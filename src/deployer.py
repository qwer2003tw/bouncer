"""
Bouncer Deployer Tools
MCP tools for SAM deployment
"""
import json
import os
import re
import time
import uuid
import boto3
from botocore.exceptions import ClientError
from metrics import emit_metric
from utils import mcp_result, mcp_error, decimal_to_native
import db as _db
import notifications
from aws_lambda_powertools import Logger
from telegram import unpin_message
from constants import (
    DEPLOY_MODE_MANUAL, DEPLOY_MODE_AUTO_CODE, VALID_DEPLOY_MODES,
)

# Import and re-export DB operations from deploy_db for backward compatibility
from deploy_db import (  # noqa: F401 - re-exports for backward compatibility
    _get_dynamodb,
    _get_projects_table,
    _get_history_table,
    _get_locks_table,
    get_git_commit_info,
    list_projects,
    get_project,
    add_project,
    update_project_config,
    remove_project,
    acquire_lock,
    release_lock,
    get_lock,
    create_deploy_record,
    update_deploy_record,
    get_deploy_record,
    get_deploy_history,
)

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


# ============================================================================
# Changeset Utilities
# ============================================================================



def _format_changeset_summary(resource_changes: list) -> str:
    """Format CFN changeset resource_changes into a readable summary.

    Example output: "Lambda::Function: ApprovalFunction (Modify), S3::Bucket: Bucket1 (Add)"
    Max 3 resources shown, remainder as "+N more".
    """
    if not resource_changes:
        return ''
    parts = []
    for change in resource_changes[:3]:
        rc = change.get('ResourceChange', {})
        rtype = rc.get('ResourceType', '').replace('AWS::', '')  # e.g. Lambda::Function → Lambda::Function
        logical_id = rc.get('LogicalResourceId', '')
        action = rc.get('Action', 'Modify')
        parts.append(f"{rtype}: {logical_id} ({action})")
    summary = ', '.join(parts)
    if len(resource_changes) > 3:
        summary += f" +{len(resource_changes) - 3} more"
    return summary







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
                        unpin_message(int(telegram_message_id))
                    except (OSError, TimeoutError, ConnectionError, ValueError) as e:
                        logger.warning("Failed to unpin message (ignored): %s", e, extra={"src_module": "deployer", "operation": "unpin_message", "error": str(e)})

        except ClientError as e:
            # Release lock on ClientError to prevent permanent lockout
            project_id = record.get('project_id')
            if project_id:
                release_lock(project_id)
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
# Deploy Mode Resolution
# ============================================================================

def _resolve_deploy_mode(project: dict) -> str:
    """Resolve deploy_mode from project config with backward compat.

    Priority:
    1. project['deploy_mode'] if valid
    2. auto_approve_deploy=True → 'auto_code' (backward compat)
    3. default → 'manual'
    """
    mode = project.get('deploy_mode')
    if mode in VALID_DEPLOY_MODES:
        return mode
    # Backward compat: auto_approve_deploy=True → auto_code
    if project.get('auto_approve_deploy', False):
        return DEPLOY_MODE_AUTO_CODE
    return DEPLOY_MODE_MANUAL


# ============================================================================
# Deploy Request Dedup & Rate Limit
# ============================================================================

def _find_pending_deploy(project_id: str):
    """Find existing pending (non-expired) deploy request for the same project.

    Returns the DDB item if found, None otherwise.
    """
    now = int(time.time())
    try:
        response = _db.table.query(
            IndexName='status-created-index',
            KeyConditionExpression='#st = :status',
            FilterExpression='#act = :deploy AND project_id = :pid',
            ExpressionAttributeNames={'#st': 'status', '#act': 'action'},
            ExpressionAttributeValues={
                ':status': 'pending_approval',
                ':deploy': 'deploy',
                ':pid': project_id,
            },
        )
        for item in response.get('Items', []):
            expiry = int(item.get('approval_expiry', 0))
            if expiry == 0 or now < expiry:
                return item
    except Exception:  # noqa: BLE001 — fail-open: DDB error should not block deploy
        logger.exception("_find_pending_deploy failed", extra={
            "src_module": "deployer", "operation": "_find_pending_deploy",
            "project_id": project_id,
        })
    return None


def mcp_tool_deploy(req_id: str, arguments: dict, table, send_approval_func) -> dict:
    """MCP tool: bouncer_deploy（需要審批）"""

    project_id = str(arguments.get('project', '')).strip()
    branch = str(arguments.get('branch', '')).strip() or None
    reason = str(arguments.get('reason', '')).strip()
    source = arguments.get('source', None)  # noqa: F841 — used in send_deploy_approval_request below
    context = arguments.get('context', None)  # noqa: F841 — used in approval flow below
    async_mode = arguments.get('async', True)  # noqa: F841 — used in sync/async response below

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

    # Deploy dedup: 同 project 已有 pending_approval deploy → 回傳既有 request
    existing_pending = _find_pending_deploy(project_id)
    if existing_pending:
        logger.info("Deploy dedup: returning existing request", extra={
            "src_module": "deployer", "operation": "mcp_tool_deploy",
            "project_id": project_id,
            "existing_request_id": existing_pending['request_id'],
        })
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': existing_pending['request_id'],
                'project_id': project_id,
                'message': '已有同專案的待審批部署請求',
                'duplicate': True,
            }, ensure_ascii=False)}]
        })

    # Deploy rate limit: 同專案 N 分鐘內最多 1 個 deploy request
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

    # Sprint 58 s58-005: Add git diff summary to help approver
    changed_files = _get_changed_files()
    git_diff_line = ""
    if changed_files:
        preview_files = changed_files[:3]
        file_list = ', '.join(preview_files)
        if len(changed_files) > 3:
            file_list += f' (+{len(changed_files) - 3} more)'
        git_diff_line = f"📁 *Changed files：* {escape_markdown(file_list)}\n"

    text = (
        f"🚀 *SAM 部署請求*\n\n"
        f"{info_lines}"
        f"📦 *專案：* {escape_markdown(project_name)}\n"
        f"🌿 *分支：* {branch}\n"
        f"{account_line}"
        f"📋 *Stack：* {stack_name}\n"
        f"{git_diff_line}"
        f"\n"
        f"🆔 *ID：* `{request_id}`\n"
        f"⏰ *10 分鐘後過期*"
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
            except Exception as exc:  # noqa: BLE001
                logger.error("post_notification_setup failed for %s: %s", request_id, exc, extra={"src_module": "deployer", "operation": "post_notification_setup", "request_id": request_id, "error": str(exc)})


# ============================================================================
# Backward Compatibility Re-exports (Sprint 61 s61-002)
# ============================================================================
# Pre-flight functions moved to deploy_preflight.py, re-exported for backward compatibility
from deploy_preflight import (  # noqa: F401, E402 - re-exports for backward compatibility
    _get_secretsmanager_client,
    validate_template_s3_url,
    _get_changed_files,
    preflight_check_secrets,
)
