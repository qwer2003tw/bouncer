"""
Bouncer Deployer Tools
MCP tools for SAM deployment
"""
import json
import os
import re
import subprocess
import time
import uuid
import boto3
from botocore.exceptions import ClientError
from metrics import emit_metric
from utils import mcp_result, mcp_error, generate_request_id, decimal_to_native, generate_display_summary
import db as _db
import notifications
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

# 環境變數
PROJECTS_TABLE = os.environ.get('PROJECTS_TABLE', 'bouncer-projects')
HISTORY_TABLE = os.environ.get('HISTORY_TABLE', 'bouncer-deploy-history')
LOCKS_TABLE = os.environ.get('LOCKS_TABLE', 'bouncer-deploy-locks')
STATE_MACHINE_ARN = os.environ.get('DEPLOY_STATE_MACHINE_ARN', '')

# DynamoDB — lazy init (no boto3 call at import time)
# Tests may set these directly: deployer.history_table = moto_table
_dynamodb = None
projects_table = None
history_table = None
locks_table = None

# Step Functions — lazy init
# Tests may patch this: patch.object(deployer, 'sfn_client')
sfn_client = None

# CloudFormation — lazy init
cfn_client = None

# Secrets Manager — lazy init
secretsmanager_client = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        _dynamodb = boto3.resource('dynamodb', region_name=region)
    return _dynamodb


def _get_projects_table():
    global projects_table
    if projects_table is None:
        projects_table = _db.deployer_projects_table._get()
    return projects_table


def _get_history_table():
    global history_table
    if history_table is None:
        history_table = _db.deployer_history_table._get()
    return history_table


def _get_locks_table():
    global locks_table
    if locks_table is None:
        locks_table = _db.deployer_locks_table._get()
    return locks_table


def _get_sfn_client():
    global sfn_client
    if sfn_client is None:
        region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        sfn_client = boto3.client('stepfunctions', region_name=region)
    return sfn_client


def _get_cfn_client():
    global cfn_client
    if cfn_client is None:
        region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        cfn_client = boto3.client('cloudformation', region_name=region)
    return cfn_client


def _get_secretsmanager_client():
    global secretsmanager_client
    if secretsmanager_client is None:
        region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        secretsmanager_client = boto3.client('secretsmanager', region_name=region)
    return secretsmanager_client


# ============================================================================
# Git Utilities
# ============================================================================

def get_git_commit_info(cwd: str = None) -> dict:
    """
    取得當前 git commit 資訊

    Returns:
        dict with commit_sha (full), commit_short (7 chars), commit_message
        若 git 不可用或非 git repo，回傳全 null 的 graceful fallback
    """
    try:
        kwargs = {'capture_output': True, 'text': True, 'timeout': 5}
        if cwd:
            kwargs['cwd'] = cwd

        sha_result = subprocess.run(['git', 'rev-parse', 'HEAD'], **kwargs)
        if sha_result.returncode != 0:
            return {'commit_sha': None, 'commit_short': None, 'commit_message': None}

        full_sha = sha_result.stdout.strip()
        short_sha = full_sha[:7] if full_sha else None

        log_result = subprocess.run(
            ['git', 'log', '-1', '--format=%h %s'],
            **kwargs
        )
        commit_message = None
        if log_result.returncode == 0 and log_result.stdout.strip():
            parts = log_result.stdout.strip().split(' ', 1)
            commit_message = parts[1] if len(parts) > 1 else None

        return {
            'commit_sha': full_sha,
            'commit_short': short_sha,
            'commit_message': commit_message,
        }
    except Exception as e:  # noqa: BLE001 — git subprocess operations, fail-safe fallback
        logger.warning("get_git_commit_info failed (graceful fallback): %s", e, extra={"src_module": "deployer", "operation": "get_git_commit_info", "error": str(e)})
        return {'commit_sha': None, 'commit_short': None, 'commit_message': None}


# ============================================================================
# Pre-flight Checks
# ============================================================================

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


# ============================================================================
# Project Management
# ============================================================================

def list_projects() -> list:
    """列出所有專案"""
    try:
        result = _get_projects_table().scan()
        return result.get('Items', [])
    except ClientError:
        logger.exception("list_projects failed", extra={"src_module": "deployer", "operation": "list_projects", "error_type": "ClientError"})
        return []
    except Exception:
        logger.exception("list_projects failed", extra={"src_module": "deployer", "operation": "list_projects"})
        return []


def get_project(project_id: str) -> dict:
    """取得專案配置"""
    try:
        result = _get_projects_table().get_item(Key={'project_id': project_id})
        return result.get('Item')
    except ClientError:
        logger.exception("get_project failed", extra={"src_module": "deployer", "operation": "get_project", "project_id": project_id, "error_type": "ClientError"})
        return None

def add_project(project_id: str, config: dict) -> dict:
    """新增專案配置"""
    item = {
        'project_id': project_id,
        'name': config.get('name', project_id),
        'git_repo': config.get('git_repo', ''),
        'git_repo_owner': config.get('git_repo_owner', ''),
        'default_branch': config.get('default_branch', 'master'),
        'stack_name': config.get('stack_name', ''),
        'target_account': config.get('target_account', ''),
        'target_role_arn': config.get('target_role_arn', ''),
        'secrets_id': config.get('secrets_id', ''),
        'sam_template_path': config.get('sam_template_path', '.'),
        'allowed_deployers': config.get('allowed_deployers', []),
        'enabled': True,
        'created_at': int(time.time()),
        'auto_approve_deploy': config.get('auto_approve_deploy', False),
        'template_s3_url': config.get('template_s3_url', ''),
    }
    # Filter out empty strings for optional fields to keep DDB clean
    if not item['template_s3_url']:
        del item['template_s3_url']
    _get_projects_table().put_item(Item=item)
    return item


def update_project_config(project_id: str, updates: dict) -> dict:
    """更新 project config 的部分欄位（patch 語義）。

    支援欄位：auto_approve_deploy (bool), template_s3_url (str), 及其他 config 欄位。
    不存在的 project → raise ValueError。
    """
    project = get_project(project_id)
    if not project:
        raise ValueError(f"Project {project_id!r} not found")

    if not updates:
        return project

    # Build DDB UpdateExpression
    set_parts = []
    expr_names = {}
    expr_values = {}
    for k, v in updates.items():
        placeholder = f"#upd_{k}"
        val_placeholder = f":upd_{k}"
        set_parts.append(f"{placeholder} = {val_placeholder}")
        expr_names[placeholder] = k
        expr_values[val_placeholder] = v

    _get_projects_table().update_item(
        Key={'project_id': project_id},
        UpdateExpression='SET ' + ', '.join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
    return {**project, **updates}


def remove_project(project_id: str) -> bool:
    """移除專案配置"""
    try:
        _get_projects_table().delete_item(Key={'project_id': project_id})
        return True
    except ClientError:
        logger.exception("remove_project failed", extra={"src_module": "deployer", "operation": "remove_project", "project_id": project_id, "error_type": "ClientError"})
        return False


# ============================================================================
# Lock Management
# ============================================================================

def acquire_lock(project_id: str, deploy_id: str, locked_by: str) -> bool:
    """嘗試取得部署鎖"""
    try:
        _get_locks_table().put_item(
            Item={
                'project_id': project_id,
                'lock_id': deploy_id,
                'locked_at': int(time.time()),
                'locked_by': locked_by,
                'ttl': int(time.time()) + 3600  # 1 小時自動過期
            },
            ConditionExpression='attribute_not_exists(project_id)'
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise


def release_lock(project_id: str) -> bool:
    """釋放部署鎖"""
    try:
        _get_locks_table().delete_item(Key={'project_id': project_id})
        return True
    except ClientError:
        logger.exception("release_lock failed", extra={"src_module": "deployer", "operation": "release_lock", "project_id": project_id, "error_type": "ClientError"})
        return False

def get_lock(project_id: str) -> dict:
    """取得鎖資訊（檢查是否過期）"""
    try:
        result = _get_locks_table().get_item(Key={'project_id': project_id})
        item = result.get('Item')

        if not item:
            return None

        # 檢查 TTL 是否過期
        ttl = item.get('ttl', 0)
        if ttl and int(time.time()) > ttl:
            # Lock 已過期，自動清理
            release_lock(project_id)
            return None

        return item
    except ClientError:
        logger.exception("get_lock failed", extra={"src_module": "deployer", "operation": "get_lock", "project_id": project_id, "error_type": "ClientError"})
        return None


# ============================================================================
# Deploy History
# ============================================================================

def create_deploy_record(deploy_id: str, project_id: str, config: dict) -> dict:
    """建立部署記錄"""
    # 取得 git commit 資訊
    git_info = get_git_commit_info()

    item = {
        'deploy_id': deploy_id,
        'project_id': project_id,
        'status': 'PENDING',
        'branch': config.get('branch', 'master'),
        'started_at': int(time.time()),
        'triggered_by': config.get('triggered_by', ''),
        'reason': config.get('reason', ''),
        'commit_sha': git_info['commit_sha'],
        'commit_short': git_info['commit_short'],
        'commit_message': git_info['commit_message'],
        'ttl': int(time.time()) + 30 * 24 * 3600  # 30 天
    }
    # DynamoDB 不允許存 None，過濾掉 null 欄位
    item = {k: v for k, v in item.items() if v is not None}
    _get_history_table().put_item(Item=item)
    return item


def update_deploy_record(deploy_id: str, updates: dict):
    """更新部署記錄"""
    try:
        update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in updates.keys())
        expr_names = {f'#{k}': k for k in updates.keys()}
        expr_values = {f':{k}': v for k, v in updates.items()}

        _get_history_table().update_item(
            Key={'deploy_id': deploy_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values
        )
    except ClientError as e:
        logger.error(f"Error updating deploy record: {e}", extra={"src_module": "deployer", "operation": "get_deploy_record", "deploy_id": deploy_id})


def get_deploy_record(deploy_id: str) -> dict:
    """取得部署記錄"""
    try:
        result = _get_history_table().get_item(Key={'deploy_id': deploy_id})
        return result.get('Item')
    except ClientError:
        logger.exception("get_deploy_record failed", extra={"src_module": "deployer", "operation": "get_deploy_record", "deploy_id": deploy_id, "error_type": "ClientError"})
        return None

def get_deploy_history(project_id: str, limit: int = 10) -> list:
    """取得專案部署歷史"""
    try:
        result = _get_history_table().query(
            IndexName='project-time-index',
            KeyConditionExpression='project_id = :pid',
            ExpressionAttributeValues={':pid': project_id},
            ScanIndexForward=False,
            Limit=limit
        )
        return result.get('Items', [])
    except ClientError as e:
        logger.error("Error getting deploy history: %s", e, extra={"src_module": "deployer", "operation": "get_deploy_history", "error": str(e)})
        return []


# ============================================================================
# Deploy Trigger
# ============================================================================

def start_deploy(project_id: str, branch: str, triggered_by: str, reason: str) -> dict:
    """啟動部署"""
    # 取得專案配置
    project = get_project(project_id)
    if not project:
        return {'error': f'專案 {project_id} 不存在'}

    if not project.get('enabled', True):
        return {'error': f'專案 {project_id} 已停用'}

    # 檢查並行鎖
    existing_lock = get_lock(project_id)
    if existing_lock:
        locked_at_ts = existing_lock.get('locked_at')
        started_at_iso = None
        estimated_remaining = None
        if locked_at_ts:
            import datetime
            started_at_iso = datetime.datetime.utcfromtimestamp(int(locked_at_ts)).strftime('%Y-%m-%dT%H:%M:%SZ')
            elapsed = int(time.time()) - int(locked_at_ts)
            avg_deploy_secs = 300  # 5 分鐘
            estimated_remaining = max(0, avg_deploy_secs - elapsed)
        return {
            'status': 'conflict',
            'message': '此專案有部署正在進行中',
            'running_deploy_id': existing_lock.get('lock_id'),
            'started_at': started_at_iso,
            'estimated_remaining': estimated_remaining,
            'hint': '用 bouncer_deploy_status 查詢進度，或 bouncer_deploy_cancel 取消',
        }

    # 建立部署 ID
    deploy_id = f"deploy-{uuid.uuid4().hex[:12]}"

    # 取得鎖
    if not acquire_lock(project_id, deploy_id, triggered_by):
        return {'error': '無法取得部署鎖，可能有其他部署正在進行'}

    # 建立部署記錄（內部自動取得 git commit 資訊）
    deploy_record = create_deploy_record(deploy_id, project_id, {
        'branch': branch,
        'triggered_by': triggered_by,
        'reason': reason,
    })

    # 準備 Step Functions 輸入
    sfn_input = {
        'deploy_id': deploy_id,
        'project_id': project_id,
        'git_repo': project.get('git_repo', ''),
        'branch': branch or project.get('default_branch', 'master'),
        'stack_name': project.get('stack_name', ''),
        'sam_template_path': project.get('sam_template_path', '.'),
        'sam_params': project.get('sam_params', ''),
        'github_pat_secret': 'sam-deployer/github-pat',
        'secrets_id': project.get('secrets_id', ''),
        'target_role_arn': project.get('target_role_arn', '')
    }

    # 啟動 Step Functions
    try:
        response = _get_sfn_client().start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=deploy_id,
            input=json.dumps(sfn_input)
        )

        # 更新部署記錄
        update_deploy_record(deploy_id, {
            'status': 'RUNNING',
            'execution_arn': response['executionArn']
        })

        emit_metric('Bouncer', 'Deploy', 1, dimensions={'Status': 'started', 'Project': project_id})

        return {
            'status': 'started',
            'deploy_id': deploy_id,
            'execution_arn': response['executionArn'],
            'project_id': project_id,
            'branch': sfn_input['branch'],
            'commit_sha': deploy_record.get('commit_sha'),
            'commit_short': deploy_record.get('commit_short'),
            'commit_message': deploy_record.get('commit_message'),
        }

    except Exception as e:  # noqa: BLE001 — fail-closed deployment trigger with cleanup
        # 失敗時釋放鎖
        release_lock(project_id)
        update_deploy_record(deploy_id, {
            'status': 'FAILED',
            'error_message': str(e)
        })
        logger.exception("[DEPLOYER] start_deploy failed")
        return {'error': f'啟動部署失敗: {str(e)}'}


def cancel_deploy(deploy_id: str) -> dict:
    """取消部署"""
    record = get_deploy_record(deploy_id)
    if not record:
        return {'error': '部署記錄不存在'}

    if record.get('status') not in ['PENDING', 'RUNNING']:
        return {'error': f'部署狀態為 {record.get("status")}，無法取消'}

    execution_arn = record.get('execution_arn')
    if execution_arn:
        try:
            _get_sfn_client().stop_execution(
                executionArn=execution_arn,
                cause='User cancelled'
            )
        except ClientError as e:
            logger.error("Error stopping execution: %s", e, extra={"src_module": "deployer", "operation": "cancel_deploy", "error": str(e)})

    # 釋放鎖
    release_lock(record.get('project_id'))

    # 更新記錄
    update_deploy_record(deploy_id, {
        'status': 'CANCELLED',
        'finished_at': int(time.time())
    })

    return {'status': 'cancelled', 'deploy_id': deploy_id}


# ============================================================================
# Deploy Error Extraction
# ============================================================================

# Max output size before truncation (400 KB)
_ERROR_OUTPUT_MAX_BYTES = 400 * 1024

# Keywords that indicate an error line (case-sensitive patterns)
_ERROR_KEYWORDS = re.compile(r'FAILED|Error:|already exists', re.IGNORECASE)

# Max error lines to extract and store
_ERROR_LINES_MAX = 5


class DeployErrorExtractor:
    """Extract, deduplicate, and format key error lines from SFN execution output.

    Design goals (Approach B — Aggressive / Clean):
    - Single responsibility: extraction logic fully encapsulated in this class.
    - No global state; easy to unit-test in isolation.
    - Truncation guard prevents DynamoDB 400 KB item size limit.
    """

    MAX_LINES: int = _ERROR_LINES_MAX
    MAX_OUTPUT_BYTES: int = _ERROR_OUTPUT_MAX_BYTES

    @staticmethod
    def truncate_if_needed(text: str, max_bytes: int = _ERROR_OUTPUT_MAX_BYTES) -> str:
        """Return *text* truncated to *max_bytes* UTF-8 bytes (if necessary)."""
        encoded = text.encode('utf-8', errors='replace')
        if len(encoded) <= max_bytes:
            return text
        truncated = encoded[:max_bytes].decode('utf-8', errors='replace')
        return truncated + '\n[...truncated]'

    @classmethod
    def extract(cls, output: str, max_lines: int = _ERROR_LINES_MAX) -> list:
        """Extract up to *max_lines* unique error lines from *output*.

        A line is included if it matches any of: "FAILED", "Error:", "already exists".
        Duplicate lines (after stripping) are removed while preserving order.
        """
        safe_output = cls.truncate_if_needed(output)
        seen: set = set()
        result: list = []
        for raw_line in safe_output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _ERROR_KEYWORDS.search(line) and line not in seen:
                seen.add(line)
                result.append(line)
                if len(result) >= max_lines:
                    break
        return result

    @staticmethod
    def format_for_telegram(error_lines: list, max_top: int = 3) -> str:
        """Return a compact multi-line string of the top *max_top* error lines."""
        top = error_lines[:max_top]
        if not top:
            return ''
        return '\n'.join(f'• {line}' for line in top)

    @classmethod
    def from_sfn_history(cls, history_events: list) -> list:
        """Extract error lines from a list of SFN execution history events.

        Scans *output* and *cause* fields of each event for error keywords.
        """
        combined_parts: list = []
        for event in history_events:
            details = event.get('executionFailedEventDetails') or {}
            cause = details.get('cause') or ''
            if cause:
                combined_parts.append(cause)

            task_failed = event.get('taskFailedEventDetails') or {}
            task_cause = task_failed.get('cause') or ''
            if task_cause:
                combined_parts.append(task_cause)

            # Also check lambdaFunctionFailedEventDetails
            lambda_failed = event.get('lambdaFunctionFailedEventDetails') or {}
            lambda_cause = lambda_failed.get('cause') or ''
            if lambda_cause:
                combined_parts.append(lambda_cause)

        combined = '\n'.join(combined_parts)
        return cls.extract(combined)


def send_deploy_failure_notification(deploy_id: str, project_id: str, error_lines: list):
    """Send a Telegram notification with deploy failure details and top 3 error lines."""
    try:
        from telegram import send_telegram_message_silent, escape_markdown
        error_text = DeployErrorExtractor.format_for_telegram(error_lines)
        error_block = f"\n\n📋 *錯誤摘要：*\n{escape_markdown(error_text)}" if error_text else ''
        text = (
            f"❌ *部署失敗*\n\n"
            f"🆔 *部署 ID：* `{deploy_id}`\n"
            f"📦 *專案：* `{project_id}`"
            f"{error_block}"
        )
        send_telegram_message_silent(text)
    except (OSError, TimeoutError, ConnectionError) as exc:
        logger.warning("send_deploy_failure_notification failed: %s", exc, extra={"src_module": "deployer", "operation": "send_deploy_failure_notification", "error": str(exc)})


def _get_progress_hint(elapsed: int) -> str:
    """Sprint11-009 (#53): 根據 elapsed_seconds 回傳人類可讀的進度提示。

    由於 DDB record 沒有 granular phase 欄位，用時間估算目前大致在哪個階段。
    這只是 hint，不保證準確，但比永遠顯示 INITIALIZING 更有用。
    """
    if elapsed < 30:
        return "🔄 正在初始化部署環境（通常需要 30-60 秒）"
    elif elapsed < 120:
        return "正在 build（SAM + Lambda layer）"
    else:
        return "正在部署 CloudFormation stack"


def get_deploy_status(deploy_id: str) -> dict:
    """取得部署狀態"""
    record = get_deploy_record(deploy_id)
    if not record:
        return {
            'status': 'not_found',
            'deploy_id': deploy_id,
            'message': 'Deploy record not found. The deploy may not have started yet, or the record has been cleaned up.',
            'hint': 'If the deploy was just approved, retry in a few seconds. If the request expired, re-issue bouncer_deploy.',
        }

    # 如果有 execution_arn，查詢 Step Functions 狀態
    execution_arn = record.get('execution_arn')
    # Sprint11-009 (#56): track SFN raw status separately from build/deploy status
    _sfn_raw_status: str | None = None
    if execution_arn and record.get('status') == 'RUNNING':
        try:
            response = _get_sfn_client().describe_execution(executionArn=execution_arn)
            _sfn_raw_status = response.get('status')
            sfn_status = _sfn_raw_status

            # 同步狀態
            if sfn_status in ['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED']:
                new_status = 'SUCCESS' if sfn_status == 'SUCCEEDED' else 'FAILED'
                finished_at = int(time.time())
                ddb_update: dict = {
                    'status': new_status,
                    'finished_at': finished_at,
                }

                # --- Sprint8-002: extract error lines on failure ---
                error_lines: list = []
                if sfn_status == 'FAILED':
                    try:
                        history_resp = _get_sfn_client().get_execution_history(
                            executionArn=execution_arn,
                            maxResults=100,
                            reverseOrder=True,
                        )
                        history_events = history_resp.get('events', [])
                        error_lines = DeployErrorExtractor.from_sfn_history(history_events)
                    except ClientError as hist_exc:
                        logger.warning("get_execution_history failed: %s", hist_exc, extra={"src_module": "deployer", "operation": "get_execution_history", "error": str(hist_exc)})

                    ddb_update['error_lines'] = error_lines

                # --- Sprint17-#55: CloudFormation failed resources ---
                stack_name = record.get('stack_name', '')
                if sfn_status in ('FAILED', 'TIMED_OUT', 'ABORTED') and stack_name:
                    try:
                        events = _get_cfn_client().describe_stack_events(
                            StackName=stack_name
                        )['StackEvents']
                        failed_events = [
                            e for e in events
                            if 'FAILED' in e.get('ResourceStatus', '')
                        ][:5]
                        ddb_update['failed_resources'] = [
                            {
                                'resource': e['LogicalResourceId'],
                                'status': e['ResourceStatus'],
                                'reason': e.get('ResourceStatusReason', '')[:200],
                            }
                            for e in failed_events
                        ]
                        # #87: cfn_failure_events with richer format (logical_resource_id, timestamp)
                        ddb_update['cfn_failure_events'] = [
                            {
                                'logical_resource_id': e['LogicalResourceId'],
                                'resource_status': e['ResourceStatus'],
                                'reason': e.get('ResourceStatusReason', '')[:300],
                                'timestamp': (
                                    e['Timestamp'].strftime('%Y-%m-%dT%H:%M:%SZ')
                                    if hasattr(e.get('Timestamp'), 'strftime')
                                    else str(e.get('Timestamp', ''))
                                ),
                            }
                            for e in failed_events
                        ]
                        ddb_update['error_summary'] = (
                            failed_events[0].get('ResourceStatusReason', '')[:300]
                            if failed_events else ''
                        )
                    except ClientError as exc:
                        logger.warning('describe_stack_events failed: %s', exc, extra={"src_module": "deployer", "operation": "describe_stack_events", "stack_name": stack_name, "error": str(exc)})

                update_deploy_record(deploy_id, ddb_update)
                record['status'] = new_status
                if error_lines:
                    record['error_lines'] = error_lines
                if 'failed_resources' in ddb_update:
                    record['failed_resources'] = ddb_update['failed_resources']
                if 'cfn_failure_events' in ddb_update:
                    record['cfn_failure_events'] = ddb_update['cfn_failure_events']
                if 'error_summary' in ddb_update:
                    record['error_summary'] = ddb_update['error_summary']

                project_id = record.get('project_id', '')
                deploy_status = 'success' if new_status == 'SUCCESS' else 'failed'
                emit_metric('Bouncer', 'Deploy', 1, dimensions={'Status': deploy_status, 'Project': project_id})

                # 計算部署耗時
                started_at = int(record.get('started_at', 0))
                if started_at:
                    duration_seconds = finished_at - started_at
                    emit_metric('Bouncer', 'DeployDuration', duration_seconds, unit='Seconds', dimensions={'Project': project_id})

                # 釋放鎖
                release_lock(record.get('project_id'))

                # --- 通知 Telegram (failure only) ---
                if sfn_status == 'FAILED':
                    send_deploy_failure_notification(deploy_id, project_id, error_lines)

                # --- Unpin the deploy message (best-effort) ---
                telegram_message_id = record.get('telegram_message_id')
                if telegram_message_id:
                    try:
                        from telegram import unpin_message
                        unpin_message(int(telegram_message_id))
                    except (OSError, TimeoutError, ConnectionError, ValueError) as e:
                        logger.warning("Failed to unpin message (ignored): %s", e, extra={"src_module": "deployer", "operation": "unpin_message", "error": str(e)})

        except ClientError as e:
            logger.error("Error getting execution status: %s", e, extra={"src_module": "deployer", "operation": "get_execution_status", "deploy_id": deploy_id, "error": str(e)})

    # Add timing fields to response
    status = record.get('status', '')
    started_at = record.get('started_at')
    finished_at = record.get('finished_at')
    if started_at:
        started_at_int = int(started_at)
        if status == 'RUNNING':
            elapsed = int(time.time()) - started_at_int
            record['elapsed_seconds'] = elapsed
            # Sprint11-009 (#53): add progress_hint instead of static 'phase'
            record['progress_hint'] = _get_progress_hint(elapsed)
        elif status in ('SUCCESS', 'FAILED') and finished_at:
            record['duration_seconds'] = int(finished_at) - started_at_int

    # Sprint11-009 (#56): clarify SFN vs build status when they diverge.
    # SFN SUCCEEDED only means the workflow state machine completed — the actual
    # build (CodeBuild/SAM) may still have failed.  Expose both so agents do not
    # mistake SFN completion for a successful deploy.
    if _sfn_raw_status is not None:
        record['sfn_status'] = _sfn_raw_status
        # build_status mirrors the canonical DDB status field
        record['build_status'] = record.get('status', status)
        if _sfn_raw_status == 'SUCCEEDED' and record.get('status') == 'FAILED':
            record['note'] = (
                'SFN workflow completed but build failed. '
                'Check error_summary for details.'
            )

    # Sprint14 (#53): deprecate unreliable 'phase' field.
    # The DDB item may carry a 'phase' attribute written by the state machine but
    # it is always 'INITIALIZING' because the SFN task never updates it.
    # Agents must use status + elapsed_seconds + progress_hint instead.
    if 'phase' in record:
        record['phase_deprecated'] = record.pop('phase')
        record['phase_note'] = (
            "phase field is unreliable (always INITIALIZING). "
            "Use status + elapsed_seconds + progress_hint instead."
        )

    return record


# ============================================================================
# MCP Tool Handlers
# ============================================================================

def mcp_tool_deploy(req_id: str, arguments: dict, table, send_approval_func) -> dict:
    """MCP tool: bouncer_deploy（需要審批）"""

    project_id = str(arguments.get('project', '')).strip()
    branch = str(arguments.get('branch', '')).strip() or None
    reason = str(arguments.get('reason', '')).strip()
    source = arguments.get('source', None)
    context = arguments.get('context', None)
    async_mode = arguments.get('async', True)

    if not project_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: project')

    if not reason:
        return mcp_error(req_id, -32602, 'Missing required parameter: reason')

    # 取得專案配置
    project = get_project(project_id)
    if not project:
        available = [p['project_id'] for p in list_projects()]
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'專案 {project_id} 不存在',
                'available_projects': available
            })}],
            'isError': True
        })

    # Pre-flight check: 驗證 external secrets 有 AWSCURRENT
    deploy_branch = branch or project.get('default_branch', 'master')
    missing_secrets = preflight_check_secrets(project, deploy_branch)
    if missing_secrets:
        error_msg = "❌ Deploy 前檢查失敗：以下 secrets 缺少 AWSCURRENT staging label\n\n"
        for secret_name in missing_secrets:
            error_msg += f"  • {secret_name}\n"
        error_msg += "\n請先執行以下指令設定 secret 值：\n"
        error_msg += "  aws secretsmanager put-secret-value \\\n"
        error_msg += "    --secret-id <secret-name> \\\n"
        error_msg += "    --secret-string \"<value>\" \\\n"
        error_msg += "    --region us-east-1\n"
        error_msg += "\nDeploy 已中止，請補值後重試。"
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': error_msg,
                'missing_secrets': missing_secrets
            }, ensure_ascii=False)}],
            'isError': True
        })

    # 檢查並行鎖
    existing_lock = get_lock(project_id)
    if existing_lock:
        locked_at_ts = existing_lock.get('locked_at')
        started_at_iso = None
        estimated_remaining = None
        if locked_at_ts:
            import datetime
            started_at_iso = datetime.datetime.utcfromtimestamp(int(locked_at_ts)).strftime('%Y-%m-%dT%H:%M:%SZ')
            elapsed = int(time.time()) - int(locked_at_ts)
            avg_deploy_secs = 300  # 5 分鐘
            estimated_remaining = max(0, avg_deploy_secs - elapsed)
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'conflict',
                'message': '此專案有部署正在進行中',
                'running_deploy_id': existing_lock.get('lock_id'),
                'started_at': started_at_iso,
                'estimated_remaining': estimated_remaining,
                'hint': '用 bouncer_deploy_status 查詢進度，或 bouncer_deploy_cancel 取消',
            }, ensure_ascii=False)}],
            'isError': True
        })

    # Auto-approve 分析（若 project 啟用）
    auto_approve_enabled = project.get('auto_approve_deploy', False)
    template_s3_url = project.get('template_s3_url', '').strip()
    stack_name = project.get('stack_name', '')

    if auto_approve_enabled and template_s3_url and stack_name:
        from changeset_analyzer import (
            create_dry_run_changeset, analyze_changeset,
            cleanup_changeset, is_code_only_change,
        )
        changeset_name = None
        cfn = _get_cfn_client()
        try:
            changeset_name = create_dry_run_changeset(cfn, stack_name, template_s3_url)
            analysis = analyze_changeset(cfn, stack_name, changeset_name)

            if is_code_only_change(analysis):
                # Code-only → auto-approve，直接 start_deploy
                deploy_result = start_deploy(
                    project_id,
                    branch or project.get('default_branch', 'master'),
                    source or 'auto-approve',
                    reason,
                )
                # 發靜默通知
                from notifications import send_auto_approve_deploy_notification
                send_auto_approve_deploy_notification(
                    project_id=project_id,
                    deploy_id=deploy_result.get('deploy_id', ''),
                    source=source,
                    reason=reason,
                )
                return mcp_result(req_id, {
                    'content': [{'type': 'text', 'text': json.dumps({
                        'status': 'started',
                        'deploy_id': deploy_result.get('deploy_id', ''),
                        'project_id': project_id,
                        'auto_approved': True,
                        'message': '純 code 變更，已自動批准並啟動部署',
                    }, ensure_ascii=False)}]
                })
            else:
                # Infra change or error → 繼續走審批，附加 changeset summary
                if analysis.error:
                    context = f"[changeset 分析失敗: {analysis.error}] {context or ''}"
                else:
                    changed = [c.get('ResourceChange', {}).get('LogicalResourceId', '') for c in analysis.resource_changes]
                    context = f"[需審批：infra 變更 {changed}] {context or ''}"
        except Exception as e:  # noqa: BLE001 — fail-safe: any error → human approval
            logger.warning(
                "auto-approve changeset analysis failed, fallback to human approval",
                extra={"src_module": "deployer", "operation": "auto_approve_analysis", "error": str(e)},
            )
        finally:
            if changeset_name:
                try:
                    cleanup_changeset(cfn, stack_name, changeset_name)
                except Exception:  # noqa: BLE001 — cleanup is best-effort
                    pass

    # 建立審批請求
    request_id = generate_request_id(f"deploy:{project_id}")
    ttl = int(time.time()) + 300 + 60

    item = {
        'request_id': request_id,
        'action': 'deploy',
        'project_id': project_id,
        'project_name': project.get('name', project_id),
        'branch': branch or project.get('default_branch', 'master'),
        'stack_name': project.get('stack_name', ''),
        'reason': reason,
        'source': source or 'mcp',  # GSI 不允許 NULL，用預設值
        'context': context or '',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp',
        'display_summary': generate_display_summary('deploy', project_id=project_id),
    }
    _db.table.put_item(Item=item)

    # 發送 Telegram 審批請求
    send_deploy_approval_request(request_id, project, branch, reason, source, context=context, expires_at=ttl)

    if async_mode:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': request_id,
                'project_id': project_id,
                'message': '部署請求已發送，等待 Telegram 確認',
                'expires_in': '300 seconds'
            })}]
        })

    # 同步模式需要等待，但這裡不實作
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'status': 'pending_approval',
            'request_id': request_id
        })}]
    })


def mcp_tool_deploy_status(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_status"""

    deploy_id = str(arguments.get('deploy_id', '')).strip()

    if not deploy_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: deploy_id')

    record = get_deploy_status(deploy_id)

    # TTL expiry check: if the approval record still exists but TTL has passed → expired
    if record.get('status') == 'pending_approval':
        ttl = int(record.get('ttl', 0))
        if ttl and int(time.time()) > ttl:
            record = {
                'status': 'expired',
                'deploy_id': deploy_id,
                'message': '部署請求已過期，未在時限內批准',
                'hint': 'Re-issue bouncer_deploy to create a new deploy request.',
            }

    # Only set isError for actual errors (not for status values that are informational)
    # not_found / expired = informational (agent can decide what to do); never set isError
    is_error = (
        'error' in record
        and record.get('status') not in ('pending', 'pending_approval', 'PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'not_found', 'expired')
    )

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(decimal_to_native(record), indent=2, ensure_ascii=False)}],
        'isError': is_error,
    })


def mcp_tool_deploy_cancel(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_cancel"""

    deploy_id = str(arguments.get('deploy_id', '')).strip()

    if not deploy_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: deploy_id')

    result = cancel_deploy(deploy_id)
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': 'error' in result
    })


def mcp_tool_deploy_history(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_history"""

    project_id = str(arguments.get('project', '')).strip()
    limit = int(arguments.get('limit', 10))

    if not project_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: project')

    history = get_deploy_history(project_id, limit)
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'project_id': project_id,
            'history': [decimal_to_native(h) for h in history]
        }, indent=2, ensure_ascii=False)}]
    })


def mcp_tool_project_list(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_project_list"""

    projects = list_projects()
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'projects': [decimal_to_native(p) for p in projects]
        }, indent=2, ensure_ascii=False)}]
    })


# ============================================================================
# Telegram Notifications
# ============================================================================

def send_deploy_approval_request(request_id: str, project: dict, branch: str, reason: str, source: str, context: str = None, expires_at: int = None):
    """發送部署審批請求到 Telegram

    Args:
        request_id:  Bouncer request ID.
        project:     Project config dict.
        branch:      Git branch to deploy.
        reason:      Human-readable reason for the deploy.
        source:      Requester identifier.
        context:     Optional extra context string.
        expires_at:  Unix timestamp when the request expires.  When provided,
                     post_notification_setup() is called to store the
                     telegram_message_id in DynamoDB and schedule EventBridge
                     cleanup — fixing the "buttons never cleared" bug (#75).
    """
    from telegram import send_telegram_message, escape_markdown
    from utils import build_info_lines

    project_id = project.get('project_id', '')
    project_name = project.get('name', project_id)
    stack_name = project.get('stack_name', '')
    target_account = project.get('target_account', '')
    # Fallback: extract account ID from target_role_arn if target_account is empty
    if not target_account:
        target_role_arn = project.get('target_role_arn', '')
        if target_role_arn and ':iam::' in target_role_arn:
            try:
                target_account = target_role_arn.split(':iam::')[1].split(':')[0]
            except (IndexError, AttributeError):
                pass

    branch = branch or project.get('default_branch', 'master')
    # build_info_lines escapes internally; pass raw values
    info_lines = build_info_lines(source=source, context=context, reason=reason)
    account_line = f"🏦 *帳號：* `{target_account}`\n" if target_account else ""

    text = (
        f"🚀 *SAM 部署請求*\n\n"
        f"{info_lines}"
        f"📦 *專案：* {escape_markdown(project_name)}\n"
        f"🌿 *分支：* {branch}\n"
        f"{account_line}"
        f"📋 *Stack：* {stack_name}\n\n"
        f"🆔 *ID：* `{request_id}`\n"
        f"⏰ *5 分鐘後過期*"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准部署', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }

    response = send_telegram_message(text, reply_markup=keyboard)

    # #75: schedule expiry cleanup so buttons are cleared when the request times out.
    # post_notification_setup() stores telegram_message_id in DDB and creates an
    # EventBridge one-time schedule that fires at expires_at to remove the keyboard.
    if expires_at is not None:
        telegram_message_id = (response or {}).get('result', {}).get('message_id')
        if telegram_message_id:
            try:
                notifications.post_notification_setup(
                    request_id=request_id,
                    telegram_message_id=telegram_message_id,
                    expires_at=expires_at,
                )
            except ClientError as exc:
                logger.error("post_notification_setup failed for %s: %s", request_id, exc, extra={"src_module": "deployer", "operation": "post_notification_setup", "request_id": request_id, "error": str(exc)})
