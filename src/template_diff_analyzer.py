"""Template diff analyzer for auto-approve deploy (#123).

Uses GitHub API to compare template.yaml between HEAD and previous commit.
Scans added lines for high-risk security patterns.
"""
import re
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from constants import DEFAULT_REGION
import boto3

HIGH_RISK_PATTERNS: list[tuple[str, str]] = [
    (r'Principal\s*:\s*["\']?\*["\']?', "Principal:* — 允許任何人呼叫"),
    (r'AuthType\s*:\s*NONE', "Lambda URL AuthType:NONE — 無驗證"),
    (r'BlockPublicAcls\s*:\s*false', "S3 BlockPublicAcls disabled"),
    (r'BlockPublicPolicy\s*:\s*false', "S3 BlockPublicPolicy disabled"),
    (r'IgnorePublicAcls\s*:\s*false', "S3 IgnorePublicAcls disabled"),
    (r'RestrictPublicBuckets\s*:\s*false', "S3 RestrictPublicBuckets disabled"),

    # s51-002: ANY IAM change triggers human review
    (r'AWS::IAM::(Role|Policy|ManagedPolicy|Group|User|InstanceProfile)', "IAM 資源變更 — 需人工審查"),
    (r'AssumeRolePolicyDocument\s*:', "IAM Trust relationship 變更 — 需人工審查"),

    # s51-003: Security Group 0.0.0.0/0
    (r'CidrIp\s*:\s*["\']?0\.0\.0\.0/0["\']?', "Security Group 開放 0.0.0.0/0 — 允許所有 IPv4"),
    (r'CidrIpv6\s*:\s*["\']?::/0["\']?', "Security Group 開放 ::/0 — 允許所有 IPv6"),

    # s51-005: KMS key policy
    (r'AWS::KMS::Key', "KMS Key 新增/修改 — 影響加密資料存取"),
    (r'KeyPolicy\s*:', "KMS Key Policy 變更 — 影響加密金鑰授權"),

    # s51-006: Lambda env var secrets (common secret patterns)
    (r'(?i)(password|secret|api_key|apikey|token|credential)\s*:\s*\S+', "Lambda 環境變數疑似明文 secret"),

    # s51-007: VPC public IP
    (r'AssociatePublicIpAddress\s*:\s*true', "EC2 分配公開 IP — 增加曝露面"),
    (r'MapPublicIpOnLaunch\s*:\s*true', "Subnet 自動分配公開 IP"),
]

# Sprint 75 #241: patterns for removed lines (- lines) — dangerous deletions
REMOVAL_RISK_PATTERNS: list[tuple[str, str]] = [
    (r'BucketName\s*:', "BucketName 被刪除 — CloudFormation 可能重建 bucket 導致資料遺失"),
    (r'DeletionPolicy\s*:\s*Retain', "DeletionPolicy: Retain 被刪除 — 失去刪除保護"),
    (r'AWS::DynamoDB::Table', "DynamoDB Table 資源被刪除"),
    (r'AWS::S3::Bucket', "S3 Bucket 資源被刪除"),
    (r'AWS::RDS::', "RDS 資源被刪除"),
]

@dataclass
class TemplateDiffResult:
    is_safe: bool                    # True = auto-approve ok
    has_template_changes: bool       # template.yaml 有無變動
    high_risk_findings: list[str] = field(default_factory=list)  # 高風險描述
    diff_summary: str = ''           # 人讀摘要
    error: str = ''                  # 分析失敗原因（有值時 fail-safe）


def _get_github_pat(secret_id: str) -> str:
    sm = boto3.client('secretsmanager', region_name=DEFAULT_REGION)
    return sm.get_secret_value(SecretId=secret_id)['SecretString']


def _github_api(url: str, pat: str) -> dict:
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'bouncer-auto-approve/1.0',
    })
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — URL is constructed from trusted S3 presigned URL
        return json.loads(resp.read())


def _get_latest_commits(owner: str, repo: str, branch: str, pat: str) -> tuple[str, str]:
    """Return (head_sha, base_sha) — head and its parent."""
    data = _github_api(
        f'https://api.github.com/repos/{owner}/{repo}/commits/{branch}',
        pat
    )
    head_sha = data['sha']
    parents = data.get('parents', [])
    base_sha = parents[0]['sha'] if parents else head_sha
    return head_sha, base_sha


def _get_template_diff(owner: str, repo: str, base_sha: str, head_sha: str, pat: str) -> str:
    """Return diff patch for template.yaml (added lines only), empty string if no changes."""
    data = _github_api(
        f'https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}',
        pat
    )
    for f in data.get('files', []):
        # Match template.yaml at repo root or in subdirectory
        if f.get('filename', '').endswith('template.yaml'):
            return f.get('patch', '')
    return ''


def _scan_added_lines(patch: str) -> list[str]:
    """Scan added (+) and removed (-) lines for high-risk patterns.

    Added lines are checked against HIGH_RISK_PATTERNS (dangerous additions).
    Removed lines are checked against REMOVAL_RISK_PATTERNS (dangerous deletions).
    """
    findings = []
    for line in patch.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            content = line[1:]  # strip leading +
            for pattern, description in HIGH_RISK_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    findings.append(f"{description} (line: `{content.strip()[:80]}`)")
        elif line.startswith('-') and not line.startswith('---'):
            content = line[1:]  # strip leading -
            for pattern, description in REMOVAL_RISK_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    findings.append(f"{description} (removed: `{content.strip()[:80]}`)")
    return findings


def analyze_template_diff(
    git_repo: str,
    branch: str,
    github_pat_secret: str,
) -> TemplateDiffResult:
    """Main entry point. Returns TemplateDiffResult.

    On any error → returns is_safe=False, error set (fail-safe: route to human approval).
    """
    try:
        # Parse owner/repo
        parts = git_repo.split('/')
        if len(parts) < 2:
            return TemplateDiffResult(is_safe=False, has_template_changes=False,
                                      error=f'Cannot parse git_repo: {git_repo!r}')
        owner, repo = parts[-2], parts[-1].replace('.git', '')

        pat = _get_github_pat(github_pat_secret)
        head_sha, base_sha = _get_latest_commits(owner, repo, branch, pat)

        if head_sha == base_sha:
            # First commit or single commit repo → no base to compare → fail-safe
            return TemplateDiffResult(is_safe=False, has_template_changes=False,
                                      error='Cannot determine base commit (first commit?)')

        patch = _get_template_diff(owner, repo, base_sha, head_sha, pat)

        if not patch:
            # No template.yaml changes
            return TemplateDiffResult(is_safe=True, has_template_changes=False,
                                      diff_summary='template.yaml 無變動 → code-only')

        findings = _scan_added_lines(patch)
        if findings:
            summary = f"template.yaml 有變動，發現 {len(findings)} 個高風險項目：" + "; ".join(findings[:3])
            return TemplateDiffResult(is_safe=False, has_template_changes=True,
                                      high_risk_findings=findings, diff_summary=summary)

        return TemplateDiffResult(is_safe=True, has_template_changes=True,
                                  diff_summary='template.yaml 有變動但無高風險項目 → auto-approve')

    except urllib.error.HTTPError as e:
        return TemplateDiffResult(is_safe=False, has_template_changes=False,
                                  error=f'GitHub API error: {e.code} {e.reason}')
    except Exception as e:  # noqa: BLE001 — fail-safe
        return TemplateDiffResult(is_safe=False, has_template_changes=False,
                                  error=f'Analysis failed: {str(e)[:200]}')
