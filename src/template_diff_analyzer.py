"""Template diff analyzer for auto-approve deploy (#123).

Uses GitHub API to compare template.yaml between HEAD and previous commit.
Scans added lines for high-risk security patterns.
"""
import re
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Tuple
import boto3

HIGH_RISK_PATTERNS: List[Tuple[str, str]] = [
    (r'Principal\s*:\s*["\']?\*["\']?', "Principal:* — 允許任何人呼叫"),
    (r'AuthType\s*:\s*NONE', "Lambda URL AuthType:NONE — 無驗證"),
    (r'BlockPublicAcls\s*:\s*false', "S3 BlockPublicAcls disabled"),
    (r'BlockPublicPolicy\s*:\s*false', "S3 BlockPublicPolicy disabled"),
    (r'IgnorePublicAcls\s*:\s*false', "S3 IgnorePublicAcls disabled"),
    (r'RestrictPublicBuckets\s*:\s*false', "S3 RestrictPublicBuckets disabled"),
]

@dataclass
class TemplateDiffResult:
    is_safe: bool                    # True = auto-approve ok
    has_template_changes: bool       # template.yaml 有無變動
    high_risk_findings: List[str] = field(default_factory=list)  # 高風險描述
    diff_summary: str = ''           # 人讀摘要
    error: str = ''                  # 分析失敗原因（有值時 fail-safe）


def _get_github_pat(secret_id: str) -> str:
    sm = boto3.client('secretsmanager', region_name='us-east-1')
    return sm.get_secret_value(SecretId=secret_id)['SecretString']


def _github_api(url: str, pat: str) -> dict:
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'bouncer-auto-approve/1.0',
    })
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — URL is constructed from trusted S3 presigned URL
        return json.loads(resp.read())


def _get_latest_commits(owner: str, repo: str, branch: str, pat: str) -> Tuple[str, str]:
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


def _scan_added_lines(patch: str) -> List[str]:
    """Scan added lines (+) for high-risk patterns. Return list of finding descriptions."""
    findings = []
    for line in patch.splitlines():
        if not line.startswith('+') or line.startswith('+++'):
            continue
        content = line[1:]  # strip leading +
        for pattern, description in HIGH_RISK_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                findings.append(f"{description} (line: `{content.strip()[:80]}`)")
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
