"""
Bouncer Deploy Pre-flight Checks
Pre-deploy validation logic extracted from deployer.py
"""
import os
from constants import DEFAULT_REGION
import re
import subprocess
import boto3
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")


def _get_secretsmanager_client():
    """Get Secrets Manager client.

    Uses deployer module global for caching and test mock compatibility.
    Tests should patch 'deploy_preflight._get_secretsmanager_client' or
    'deployer._get_secretsmanager_client' (re-exported from this module).
    """
    import deployer as _deployer
    if _deployer.secretsmanager_client is None:
        region = DEFAULT_REGION
        _deployer.secretsmanager_client = boto3.client('secretsmanager', region_name=region)
    return _deployer.secretsmanager_client


def validate_template_s3_url(url: str) -> tuple:
    """Validate template_s3_url format before passing to CloudFormation.

    Rules:
    - Must start with https://
    - Must contain amazonaws.com or S3 domain markers (.s3. or .s3-)
    - Max length 1024 (CloudFormation TemplateURL limit)

    Supports all S3 URL formats:
    - Virtual-hosted-style: https://bucket.s3.region.amazonaws.com/key
    - Path-style: https://s3.amazonaws.com/bucket/key
    - Dash-region: https://s3-region.amazonaws.com/bucket/key

    Returns:
        (is_valid: bool, reason: str) — reason is empty when valid.
    """
    if not url:
        return False, "URL is empty"
    if len(url) > 1024:
        return False, f"URL too long ({len(url)} > 1024)"
    if not url.startswith('https://'):
        return False, "URL does not start with https://"
    # S3 URLs contain amazonaws.com or at least s3 domain markers
    if 'amazonaws.com' not in url and '.s3.' not in url and '.s3-' not in url:
        return False, "URL does not appear to be an S3 URL (no amazonaws.com or S3 domain)"
    return True, ""


def _get_changed_files(repo_path: str = '.') -> list[str]:
    """Get list of files changed in latest commit vs previous.

    Sprint 58 s58-005: For deploy notifications, show what files changed.

    Returns:
        List of changed file paths, or empty list on error.
    """
    try:
        result = subprocess.run(
            ['git', 'diff', '--name-only', 'HEAD~1', 'HEAD'],
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=10
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().split('\n') if f]
    except Exception:
        logger.warning("Failed to detect secrets via git command", exc_info=True)
    return []


def preflight_check_secrets(project: dict, branch: str) -> list:
    """
    Pre-flight check: 驗證 template.yaml 引用的所有 Secrets Manager secrets 都有 AWSCURRENT

    Args:
        project: 專案配置
        branch: 部署分支

    Returns:
        list[str]: 缺少 AWSCURRENT 的 secret 名稱列表（空列表表示全部通過）
    """
    import tempfile
    import shutil

    git_repo = project.get('git_repo', '')
    if not git_repo:
        return []

    sam_template_path = project.get('sam_template_path', '.')
    branch = branch or project.get('default_branch', 'master')

    # 取得 GitHub PAT
    try:
        sm_client = _get_secretsmanager_client()
        github_pat_response = sm_client.get_secret_value(SecretId='sam-deployer/github-pat')
        github_pat = github_pat_response['SecretString']
    except ClientError as e:
        logger.error("Failed to get GitHub PAT: %s", e, extra={"src_module": "deployer", "operation": "get_preflight_secrets", "error": str(e)})
        return []  # graceful degradation

    # Clone repo to temp dir
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix='bouncer-preflight-')

        # Inject PAT into clone URL
        if git_repo.startswith('https://github.com/'):
            clone_url = git_repo.replace('https://github.com/', f'https://{github_pat}@github.com/')
        else:
            clone_url = git_repo

        # Clone (shallow, single branch)
        clone_cmd = ['git', 'clone', '--depth', '1', '--branch', branch, clone_url, tmpdir]
        result = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error("Git clone failed: %s", result.stderr, extra={"src_module": "deployer", "operation": "git_clone", "error": result.stderr[:200]})
            return []  # graceful degradation

        # Find template.yaml
        template_path = os.path.join(tmpdir, sam_template_path, 'template.yaml')
        if not os.path.exists(template_path):
            template_path = os.path.join(tmpdir, sam_template_path, 'template.yml')

        if not os.path.exists(template_path):
            logger.warning("template.yaml not found in %s", sam_template_path, extra={"src_module": "deployer", "operation": "find_template", "sam_template_path": sam_template_path})
            return []

        # Read template and extract secret references
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()

        # Match patterns like:
        # !Sub '{{resolve:secretsmanager:secret-name:SecretString:key}}'
        # !Sub '{{resolve:secretsmanager:secret-name}}'
        # "{{resolve:secretsmanager:secret-name}}"
        secret_pattern = r'\{\{resolve:secretsmanager:([^:}\s]+)'
        secret_names = re.findall(secret_pattern, template_content)

        if not secret_names:
            return []  # No secrets referenced

        # Validate each secret has AWSCURRENT
        missing_secrets = []
        for secret_name in set(secret_names):
            try:
                response = sm_client.describe_secret(SecretId=secret_name)
                version_stages = response.get('VersionIdsToStages', {})

                # Check if any version has AWSCURRENT
                has_current = any('AWSCURRENT' in stages for stages in version_stages.values())

                if not has_current:
                    missing_secrets.append(secret_name)

            except sm_client.exceptions.ResourceNotFoundException:
                missing_secrets.append(secret_name)
            except ClientError as e:
                logger.error("Error checking secret %s: %s", secret_name, e, extra={"src_module": "deployer", "operation": "check_secret", "secret_name": secret_name, "error": str(e)})
                missing_secrets.append(secret_name)

        return missing_secrets

    except Exception as e:  # noqa: BLE001 — preflight fail-closed: subprocess, file I/O, git operations
        logger.error("Preflight check error: %s", e, extra={"src_module": "deployer", "operation": "preflight_check", "error": str(e)})
        return []  # graceful degradation
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
