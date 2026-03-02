"""
Bouncer - Clawdbot AWS 命令審批執行系統
版本: 3.0.0 (MCP 支援)
更新: 2026-02-03

支援兩種模式：
1. REST API（向後兼容）
2. MCP JSON-RPC（新增）
"""

import json
import hashlib
import hmac
import logging
import time
import unicodedata


# 從模組導入
from telegram import (  # noqa: F401
    escape_markdown,
    update_message, answer_callback,
)
from trust import (  # noqa: F401
    revoke_trust_session, create_trust_session,
    increment_trust_command_count, is_trust_excluded, should_trust_approve,
)
from commands import (  # noqa: F401
    get_block_reason, is_auto_approve, execute_command,
    is_blocked, is_dangerous, aws_cli_split,
)
from accounts import (  # noqa: F401
    init_bot_commands, validate_account_id,
    init_default_account, list_accounts, validate_role_arn,
)
from rate_limit import check_rate_limit, RateLimitExceeded, PendingLimitExceeded  # noqa: F401
from paging import store_paged_output, get_paged_output  # noqa: F401
from utils import response, generate_request_id, decimal_to_native, mcp_result, mcp_error, get_header, log_decision, generate_display_summary
# MCP tool handlers — split into sub-modules
from mcp_execute import (
    mcp_tool_execute, mcp_tool_request_grant, mcp_tool_grant_status, mcp_tool_revoke_grant,
)
from mcp_upload import mcp_tool_upload, mcp_tool_upload_batch, execute_upload  # noqa: F401
from mcp_presigned import mcp_tool_request_presigned, mcp_tool_request_presigned_batch
from mcp_confirm import handle_confirm_upload
from mcp_admin import (
    mcp_tool_status, mcp_tool_help, mcp_tool_trust_status, mcp_tool_trust_revoke,
    mcp_tool_add_account, mcp_tool_list_accounts, mcp_tool_get_page,
    mcp_tool_list_pending, mcp_tool_remove_account, mcp_tool_list_safelist,
)
from mcp_history import mcp_tool_history, mcp_tool_stats
from mcp_deploy_frontend import mcp_tool_deploy_frontend
from callbacks import (
    handle_command_callback, handle_account_add_callback, handle_account_remove_callback,
    handle_deploy_callback, handle_upload_callback, handle_upload_batch_callback,
    handle_grant_approve_all, handle_grant_approve_safe, handle_grant_deny,
    handle_deploy_frontend_callback,
)
from telegram_commands import (  # noqa: F401
    handle_telegram_command, handle_accounts_command,
    handle_help_command, handle_pending_command, handle_trust_command,
)
from tool_schema import MCP_TOOLS  # noqa: F401
from metrics import emit_metric

# 從 constants.py 導入所有常數
from constants import (  # noqa: F401
    VERSION,
    TELEGRAM_WEBHOOK_SECRET,
    APPROVED_CHAT_IDS,
    REQUEST_SECRET, ENABLE_HMAC,
    MCP_MAX_WAIT,
    BLOCKED_PATTERNS, AUTO_APPROVE_PREFIXES,
    APPROVAL_TIMEOUT_DEFAULT, APPROVAL_TTL_BUFFER,
    TELEGRAM_TIMESTAMP_MAX_AGE,
)


# DynamoDB — canonical references in db.py; re-exported for backward compat
from db import table, accounts_table  # noqa: F401

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
logger = logging.getLogger(__name__)


# ============================================================================
# Cleanup Expired Handler (sprint7-002)
# ============================================================================

def handle_cleanup_expired(event: dict) -> dict:
    """Handle EventBridge Scheduler trigger to clean up expired request buttons.

    Triggered by EventBridge Scheduler when a request's TTL is reached.

    Scenarios:
    - Request has status 'pending' → update message text to "⏰ 已過期" and remove buttons
    - Request already approved/rejected/denied/timeout/auto_approved → no-op
    - Request not found or missing message_id → log and return gracefully
    """
    request_id = event.get('request_id')
    if not request_id:
        logger.warning("[CLEANUP] Missing request_id in event")
        return response(200, {'ok': True, 'skipped': True, 'reason': 'missing_request_id'})

    logger.info("[CLEANUP] Processing expiry for request_id=%s", request_id)

    try:
        item = table.get_item(Key={'request_id': request_id}).get('Item')
    except Exception as e:
        logger.error(f"[CLEANUP] DynamoDB error for {request_id}: {e}")
        return response(200, {'ok': True, 'skipped': True, 'reason': 'db_error'})

    if not item:
        logger.info(f"[CLEANUP] Request {request_id} not found — skipping")
        return response(200, {'ok': True, 'skipped': True, 'reason': 'not_found'})

    current_status = item.get('status', 'pending')

    # Already actioned — no-op
    if current_status in ('approved', 'rejected', 'denied', 'timeout', 'auto_approved'):
        logger.info(f"[CLEANUP] Request {request_id} already {current_status} — no-op")
        return response(200, {'ok': True, 'skipped': True, 'reason': f'already_{current_status}'})

    # Retrieve stored telegram_message_id
    telegram_message_id = item.get('telegram_message_id')
    if not telegram_message_id:
        logger.info(f"[CLEANUP] Request {request_id} has no telegram_message_id — cannot update message")
        # Still mark as timed out
        _mark_request_timeout(request_id)
        return response(200, {'ok': True, 'skipped': True, 'reason': 'no_message_id'})

    # Build expiry message preserving original context
    source = item.get('source', '')
    command = item.get('command', '')
    reason = item.get('reason', '')
    context_val = item.get('context', '')

    source_line = f"🤖 *來源：* {escape_markdown(source)}\n" if source else ""
    context_line = f"📝 *任務：* {escape_markdown(context_val)}\n" if context_val else ""

    # Action-specific summary line
    action_type = item.get('action', 'execute')
    if action_type in ('upload', 'upload_batch'):
        summary = item.get('display_summary', '上傳請求')
        detail_line = f"📁 *請求：* {escape_markdown(summary)}\n"
    elif action_type == 'deploy':
        project_id = item.get('project_id', '')
        detail_line = f"🚀 *部署：* `{project_id}`\n"
    else:
        cmd_preview = command[:200] + '...' if len(command) > 200 else command
        detail_line = f"📋 *命令：*\n`{cmd_preview}`\n"

    reason_line = f"\n💬 *原因：* {escape_markdown(reason)}" if reason else ""

    expiry_text = (
        f"⏰ *已過期*\n\n"
        f"{source_line}"
        f"{context_line}"
        f"{detail_line}"
        f"{reason_line}\n"
        f"\n🆔 `{request_id}`"
    )

    # Update Telegram message (remove buttons)
    try:
        update_message(int(telegram_message_id), expiry_text, remove_buttons=True)
        logger.info(
            "[CLEANUP] Updated Telegram message %s for request %s",
            telegram_message_id, request_id,
        )
    except Exception as exc:
        logger.warning(
            "[CLEANUP] Failed to update Telegram message %s: %s",
            telegram_message_id, exc,
        )
        # Continue to update DynamoDB even if Telegram update fails

    # Mark as timeout in DynamoDB
    _mark_request_timeout(request_id)

    emit_metric('Bouncer', 'RequestExpired', 1, dimensions={'Action': action_type})
    return response(200, {'ok': True, 'cleaned': True, 'request_id': request_id})


def _mark_request_timeout(request_id: str) -> None:
    """Set request status to 'timeout' in DynamoDB."""
    try:
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'timeout'},
        )
    except Exception as exc:
        logger.error("[CLEANUP] Failed to update DynamoDB status for %s: %s", request_id, exc)


# ============================================================================
# Trust Expiry Handler (sprint8-007)
# ============================================================================

def handle_trust_expiry(event: dict) -> dict:
    """Handle EventBridge Scheduler trigger fired when a trust session expires.

    Triggered by TrustExpiryNotifier.schedule() at trust session expires_at.

    Acceptance scenarios:
    1. Queries pending requests matching the expired trust session's source +
       trust_scope (stored on the trust session item).
    2. Sends a Telegram notification: "trust 已過期，N 個 pending 請求需手動審批".
    3. Returns gracefully when no pending requests exist.
    4. Handles DynamoDB / missing-trust-id errors without raising.

    Args:
        event: EventBridge Scheduler event containing ``trust_id``.

    Returns:
        Response dict (always 200 — handler is best-effort).
    """
    trust_id = event.get('trust_id')
    if not trust_id:
        logger.warning("[TRUST-EXPIRY] Missing trust_id in event")
        return response(200, {'ok': True, 'skipped': True, 'reason': 'missing_trust_id'})

    logger.info("[TRUST-EXPIRY] Processing expiry notification for trust_id=%s", trust_id)

    # Fetch the trust session item to get source + trust_scope
    try:
        trust_item = table.get_item(Key={'request_id': trust_id}).get('Item')
    except Exception as exc:
        logger.error("[TRUST-EXPIRY] DynamoDB error fetching trust session %s: %s", trust_id, exc)
        return response(200, {'ok': True, 'skipped': True, 'reason': 'db_error'})

    if not trust_item:
        logger.info("[TRUST-EXPIRY] Trust session %s not found (already revoked?) — skipping", trust_id)
        return response(200, {'ok': True, 'skipped': True, 'reason': 'not_found'})

    source = trust_item.get('source', '') or trust_item.get('bound_source', '')
    trust_scope = trust_item.get('trust_scope', '')

    # Query pending requests that match source + trust_scope
    pending_requests = _query_pending_for_trust(source=source, trust_scope=trust_scope)
    pending_count = len(pending_requests)

    logger.info(
        "[TRUST-EXPIRY] trust_id=%s source=%r trust_scope=%r pending_count=%d",
        trust_id, source, trust_scope, pending_count,
    )

    # Send Telegram notification
    _send_trust_expiry_notification(
        trust_id=trust_id,
        source=source,
        trust_scope=trust_scope,
        pending_count=pending_count,
        pending_requests=pending_requests,
    )

    emit_metric('Bouncer', 'TrustExpired', 1)
    return response(200, {
        'ok': True,
        'trust_id': trust_id,
        'source': source,
        'trust_scope': trust_scope,
        'pending_count': pending_count,
    })


def _query_pending_for_trust(source: str, trust_scope: str) -> list:
    """Query pending requests that match the expired trust session's source.

    Uses the ``source-created-index`` GSI to efficiently find matching
    pending requests without a full table scan.

    Args:
        source:      Source string bound to the trust session.
        trust_scope: Trust scope identifier (used as fallback when source is empty).

    Returns:
        List of pending request items (may be empty).
    """
    effective_source = source or trust_scope
    if not effective_source:
        logger.warning("[TRUST-EXPIRY] No source or trust_scope — cannot query pending requests")
        return []

    try:
        resp = table.query(
            IndexName='source-created-index',
            KeyConditionExpression='#src = :source',
            FilterExpression='#status = :status',
            ExpressionAttributeNames={'#src': 'source', '#status': 'status'},
            ExpressionAttributeValues={
                ':source': effective_source,
                ':status': 'pending_approval',
            },
            ScanIndexForward=False,
            Limit=100,
        )
        items = resp.get('Items', [])
        logger.info(
            "[TRUST-EXPIRY] Found %d pending_approval items for source=%r",
            len(items), effective_source,
        )
        return items
    except Exception as exc:
        logger.error("[TRUST-EXPIRY] Failed to query pending requests for source=%r: %s", effective_source, exc)
        return []


def _send_trust_expiry_notification(
    trust_id: str,
    source: str,
    trust_scope: str,
    pending_count: int,
    pending_requests: list,
) -> None:
    """Send Telegram notification that a trust session has expired.

    Args:
        trust_id:        Trust session ID.
        source:          Source string from the expired trust session.
        trust_scope:     Trust scope identifier.
        pending_count:   Number of pending requests needing manual approval.
        pending_requests: List of pending request items (for display).
    """
    from telegram import send_telegram_message_silent

    safe_source = escape_markdown(source or trust_scope or trust_id[:20])

    if pending_count == 0:
        text = (
            f"⏰ *信任時段已過期*\n\n"
            f"🤖 *來源：* {safe_source}\n"
            f"🔑 `{trust_id}`\n\n"
            f"✅ 目前無 pending 請求需要審批。"
        )
    else:
        # Build a short summary of the pending requests
        lines = []
        for i, item in enumerate(pending_requests[:5]):
            req_id = item.get('request_id', '')[:20]
            cmd_preview = item.get('command', item.get('display_summary', ''))[:60]
            lines.append(f"  {i + 1}\\. `{req_id}` — `{escape_markdown(cmd_preview)}`")
        if pending_count > 5:
            lines.append(f"  _{pending_count - 5} 個更多\\.\\.\\._ ")

        pending_details = "\n".join(lines)
        text = (
            f"⏰ *信任時段已過期*\n\n"
            f"🤖 *來源：* {safe_source}\n"
            f"🔑 `{trust_id}`\n\n"
            f"⚠️ *{pending_count} 個 pending 請求需手動審批：*\n"
            f"{pending_details}"
        )

    try:
        send_telegram_message_silent(text)
        logger.info(
            "[TRUST-EXPIRY] Sent expiry notification for trust_id=%s (pending=%d)",
            trust_id, pending_count,
        )
    except Exception as exc:
        logger.error(
            "[TRUST-EXPIRY] Failed to send Telegram notification for trust %s: %s",
            trust_id, exc,
        )


# ============================================================================
# Lambda Handler
# ============================================================================

def lambda_handler(event: dict, context) -> dict:
    """主入口 - 路由請求"""
    # 初始化 Bot commands（cold start 時執行一次）
    init_bot_commands()

    # EventBridge Scheduler cleanup trigger (sprint7-002)
    if event.get('source') == 'bouncer-scheduler' and event.get('action') == 'cleanup_expired':
        return handle_cleanup_expired(event)

    # EventBridge Scheduler trust expiry notification trigger (sprint8-007)
    if event.get('source') == 'bouncer-scheduler' and event.get('action') == 'trust_expiry':
        return handle_trust_expiry(event)

    # 支援 Function URL (rawPath) 和 API Gateway (path)
    path = event.get('rawPath') or event.get('path') or '/'

    # 支援 Function URL 和 API Gateway 的 method 格式
    method = (
        event.get('requestContext', {}).get('http', {}).get('method') or
        event.get('requestContext', {}).get('httpMethod') or
        event.get('httpMethod') or
        'GET'
    )

    # 路由
    if path.endswith('/webhook'):
        return handle_telegram_webhook(event)
    elif path.endswith('/mcp'):
        return handle_mcp_request(event)
    elif '/status/' in path:
        return handle_status_query(event, path)
    elif method == 'POST':
        return handle_clawdbot_request(event)
    else:
        return response(200, {
            'service': 'Bouncer',
            'version': VERSION,
            'endpoints': {
                'POST /': 'Submit command for approval (REST)',
                'POST /mcp': 'MCP JSON-RPC endpoint',
                'GET /status/{id}': 'Query request status',
                'POST /webhook': 'Telegram callback'
            },
            'mcp_tools': list(MCP_TOOLS.keys())
        })


# ============================================================================
# MCP JSON-RPC Handler
# ============================================================================

def handle_mcp_request(event) -> dict:
    """處理 MCP JSON-RPC 請求"""
    headers = event.get('headers', {})

    # 驗證 secret
    if get_header(headers, 'x-approval-secret') != REQUEST_SECRET:
        return mcp_error(None, -32600, 'Invalid secret')

    # 解析 JSON-RPC
    try:
        body = json.loads(event.get('body', '{}'))
    except Exception as e:
        logger.error(f"Error: {e}")
        return mcp_error(None, -32700, 'Parse error')

    jsonrpc = body.get('jsonrpc')
    method = body.get('method', '')
    params = body.get('params', {})
    req_id = body.get('id')

    if jsonrpc != '2.0':
        return mcp_error(req_id, -32600, 'Invalid Request: jsonrpc must be "2.0"')

    # 處理 MCP 標準方法
    if method == 'initialize':
        return mcp_result(req_id, {
            'protocolVersion': '2024-11-05',
            'serverInfo': {
                'name': 'bouncer',
                'version': VERSION
            },
            'capabilities': {
                'tools': {}
            }
        })

    elif method == 'tools/list':
        tools = []
        for name, spec in MCP_TOOLS.items():
            tools.append({
                'name': name,
                'description': spec['description'],
                'inputSchema': spec['parameters']
            })
        return mcp_result(req_id, {'tools': tools})

    elif method == 'tools/call':
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})
        return handle_mcp_tool_call(req_id, tool_name, arguments)

    else:
        return mcp_error(req_id, -32601, f'Method not found: {method}')


# ---------------------------------------------------------------------------
# Tool handler dispatch table
# ---------------------------------------------------------------------------
# Standard handlers: (req_id, arguments) -> dict
TOOL_HANDLERS = {
    'bouncer_execute': mcp_tool_execute,
    'bouncer_status': mcp_tool_status,
    'bouncer_help': mcp_tool_help,
    'bouncer_list_safelist': mcp_tool_list_safelist,
    'bouncer_trust_status': mcp_tool_trust_status,
    'bouncer_trust_revoke': mcp_tool_trust_revoke,
    'bouncer_add_account': mcp_tool_add_account,
    'bouncer_list_accounts': mcp_tool_list_accounts,
    'bouncer_get_page': mcp_tool_get_page,
    'bouncer_list_pending': mcp_tool_list_pending,
    'bouncer_remove_account': mcp_tool_remove_account,
    'bouncer_upload': mcp_tool_upload,
    'bouncer_upload_batch': mcp_tool_upload_batch,
    'bouncer_request_presigned': mcp_tool_request_presigned,
    'bouncer_request_presigned_batch': mcp_tool_request_presigned_batch,
    'bouncer_confirm_upload': lambda req_id, arguments: handle_confirm_upload({**arguments, '_req_id': req_id}),
    'bouncer_request_grant': mcp_tool_request_grant,
    'bouncer_grant_status': mcp_tool_grant_status,
    'bouncer_revoke_grant': mcp_tool_revoke_grant,
    'bouncer_history': mcp_tool_history,
    'bouncer_stats': mcp_tool_stats,
    'bouncer_deploy_frontend': mcp_tool_deploy_frontend,
}

# Deployer handlers are lazy-imported to avoid cold-start cost
_DEPLOYER_TOOLS = {
    'bouncer_deploy', 'bouncer_deploy_status', 'bouncer_deploy_cancel',
    'bouncer_deploy_history', 'bouncer_project_list',
}


def _get_deployer_handler(tool_name: str):
    """Lazy-import deployer handlers (only when needed)."""
    from deployer import (  # noqa: F811
        mcp_tool_deploy, mcp_tool_deploy_status, mcp_tool_deploy_cancel,
        mcp_tool_deploy_history, mcp_tool_project_list,
    )
    return {
        'bouncer_deploy': mcp_tool_deploy,
        'bouncer_deploy_status': mcp_tool_deploy_status,
        'bouncer_deploy_cancel': mcp_tool_deploy_cancel,
        'bouncer_deploy_history': mcp_tool_deploy_history,
        'bouncer_project_list': mcp_tool_project_list,
    }[tool_name]


def handle_mcp_tool_call(req_id, tool_name: str, arguments: dict) -> dict:
    """處理 MCP tool 呼叫"""
    emit_metric('Bouncer', 'ToolCall', 1, dimensions={'ToolName': tool_name})

    # Standard tool handlers
    handler = TOOL_HANDLERS.get(tool_name)
    if handler:
        return handler(req_id, arguments)

    # Deployer tools (lazy-imported, bouncer_deploy has extra args)
    if tool_name in _DEPLOYER_TOOLS:
        deployer_handler = _get_deployer_handler(tool_name)
        if tool_name == 'bouncer_deploy':
            return deployer_handler(req_id, arguments, table, send_approval_request)
        return deployer_handler(req_id, arguments)

    return mcp_error(req_id, -32602, f'Unknown tool: {tool_name}')


# ============================================================================
# Upload 相關函數（被 callbacks 呼叫）
# ============================================================================



# execute_upload moved to mcp_upload.py; re-exported via import above


# ============================================================================
# REST API Handlers（向後兼容）
# ============================================================================

def handle_status_query(event, path):
    """查詢請求狀態 - GET /status/{request_id}"""
    headers = event.get('headers', {})

    if get_header(headers, 'x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})

    parts = path.split('/status/')
    if len(parts) < 2:
        return response(400, {'error': 'Missing request_id'})

    request_id = parts[1].strip('/')
    if not request_id:
        return response(400, {'error': 'Missing request_id'})

    try:
        result = table.get_item(Key={'request_id': request_id})
        item = result.get('Item')

        if not item:
            return response(404, {'error': 'Request not found', 'request_id': request_id})

        return response(200, decimal_to_native(item))

    except Exception as e:
        return response(500, {'error': str(e)})


def handle_clawdbot_request(event: dict) -> dict:
    """處理 REST API 的命令執行請求（向後兼容）"""
    headers = event.get('headers', {})

    if get_header(headers, 'x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})

    if ENABLE_HMAC:
        body_str = event.get('body', '')
        if not verify_hmac(headers, body_str):
            return response(403, {'error': 'Invalid HMAC signature'})

    try:
        body = json.loads(event.get('body', '{}'))
    except Exception as e:
        logger.error(f"Error: {e}")
        return response(400, {'error': 'Invalid JSON'})

    command = unicodedata.normalize('NFKC', body.get('command', '')).strip()
    reason = body.get('reason', 'No reason provided')
    source = body.get('source', None)  # 來源（哪個 agent/系統）
    assume_role = body.get('assume_role', None)  # 目標帳號 role ARN
    timeout = min(body.get('timeout', APPROVAL_TIMEOUT_DEFAULT), MCP_MAX_WAIT)

    if not command:
        return response(400, {'error': 'Missing command'})

    # SEC-011: Compliance check for REST endpoint
    try:
        from compliance_checker import check_compliance
        is_compliant, violation = check_compliance(command)
        if not is_compliant:
            emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'compliance'})
            log_decision(
                table=table,
                request_id=generate_request_id(command),
                command=command,
                reason=reason,
                source=source,
                account_id=None,
                decision_type='compliance_violation',
            )
            return response(400, {
                'error': 'Compliance violation',
                'violations': [{
                    'rule_id': violation.rule_id,
                    'rule_name': violation.rule_name,
                    'description': violation.description,
                    'remediation': violation.remediation,
                }]
            })
    except ImportError:
        pass  # compliance_checker 不存在時跳過

    # Layer 1: BLOCKED
    block_reason = get_block_reason(command)
    if block_reason:
        emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'blocked'})
        log_decision(
            table=table,
            request_id=generate_request_id(command),
            command=command,
            reason=reason,
            source=source,
            account_id=None,
            decision_type='blocked',
        )
        return response(403, {
            'status': 'blocked',
            'error': '命令被安全規則封鎖',
            'block_reason': block_reason,
            'command': command[:200]
        })

    # Layer 2: SAFELIST
    if is_auto_approve(command):
        result = execute_command(command, assume_role)
        cmd_status = 'error' if result.startswith('❌') else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'auto_approve'})
        log_decision(
            table=table,
            request_id=generate_request_id(command),
            command=command,
            reason=reason,
            source=source,
            account_id=None,
            decision_type='auto_approved',
            mode='rest',
        )
        return response(200, {
            'status': 'auto_approved',
            'command': command,
            'result': result
        })

    # Layer 3: APPROVAL
    request_id = generate_request_id(command)
    ttl = int(time.time()) + timeout + APPROVAL_TTL_BUFFER

    item = {
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'source': source or '__anonymous__',
        'assume_role': assume_role,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'rest',
        'decision_type': 'pending',
        'display_summary': generate_display_summary('execute', command=command),
    }
    table.put_item(Item=item)

    send_approval_request(request_id, command, reason, timeout, source, assume_role)

    # Sync long-polling 已移除（wait=True 不再生效）。
    # 一律返回 pending，讓 client 用 /status/{id} 輪詢。
    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': '請求已發送，等待 Telegram 確認',
        'expires_in': f'{timeout} seconds',
        'check_status': f'/status/{request_id}'
    })




# ============================================================================
# Telegram Webhook Handler
# ============================================================================

def _is_grant_expired(request_id: str, callback: dict) -> bool:
    """Check if a grant request has expired; if so, send expiry callback and update message.

    Returns True if the grant has expired (caller should return early),
    False if not expired or item not found (let callback handler decide).
    """
    try:
        grant_item = table.get_item(Key={'request_id': request_id}).get('Item')
        if not grant_item:
            return False
        grant_ttl = int(grant_item.get('ttl', 0))
        if grant_ttl and int(time.time()) > grant_ttl:
            answer_callback(callback['id'], '⏰ 此請求已過期')
            message_id = callback.get('message', {}).get('message_id')
            if message_id:
                update_message(
                    message_id,
                    f"⏰ *Grant 審批已過期*\n\n🆔 `{request_id}`",
                    remove_buttons=True,
                )
            try:
                table.update_item(
                    Key={'request_id': request_id},
                    UpdateExpression='SET #s = :s',
                    ExpressionAttributeNames={'#s': 'status'},
                    ExpressionAttributeValues={':s': 'timeout'},
                )
            except Exception:
                pass
            return True
    except Exception as e:
        print(f"[GRANT EXPIRY] Error checking grant TTL: {e}")
    return False


def handle_telegram_webhook(event: dict) -> dict:
    """處理 Telegram callback"""
    headers = event.get('headers', {})

    if TELEGRAM_WEBHOOK_SECRET:
        received_secret = get_header(headers, 'x-telegram-bot-api-secret-token') or ''
        if received_secret != TELEGRAM_WEBHOOK_SECRET:
            return response(403, {'error': 'Invalid webhook signature'})

    try:
        body = json.loads(event.get('body', '{}'))
    except Exception as e:
        logger.error(f"Error: {e}")
        return response(400, {'error': 'Invalid JSON'})

    # 處理文字訊息（指令）
    message = body.get('message')
    if message:
        return handle_telegram_command(message)

    callback = body.get('callback_query')
    if not callback:
        return response(200, {'ok': True})

    user_id = str(callback.get('from', {}).get('id', ''))
    if user_id not in APPROVED_CHAT_IDS:
        answer_callback(callback['id'], '❌ 你沒有審批權限')
        return response(403, {'error': 'Unauthorized user'})

    data = callback.get('data', '')
    if ':' not in data:
        return response(400, {'error': 'Invalid callback data'})

    action, request_id = data.split(':', 1)

    # 特殊處理：撤銷信任時段
    if action == 'revoke_trust':
        emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
        # Fetch trust item before deletion for summary (sprint9-007-phase-a)
        trust_item_for_summary = None
        try:
            _resp = table.get_item(Key={'request_id': request_id})
            trust_item_for_summary = _resp.get('Item')
        except Exception as _e:
            logger.warning('Failed to fetch trust item for summary: %s', _e)
        success = revoke_trust_session(request_id)
        emit_metric('Bouncer', 'TrustSession', 1, dimensions={'Event': 'revoked'})
        message_id = callback.get('message', {}).get('message_id')
        if success:
            update_message(message_id, f"🛑 *信任時段已結束*\n\n`{request_id}`", remove_buttons=True)
            answer_callback(callback['id'], '🛑 信任已結束')
            # Send execution summary (sprint9-007-phase-a)
            if trust_item_for_summary:
                try:
                    send_trust_session_summary(trust_item_for_summary)
                except Exception as _se:
                    logger.error('send_trust_session_summary error: %s', _se)
        else:
            answer_callback(callback['id'], '❌ 撤銷失敗')
        return response(200, {'ok': True})

    # 特殊處理：Grant Session callbacks
    if action == 'grant_approve_all':
        emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
        if _is_grant_expired(request_id, callback):
            return response(200, {'ok': True})
        return handle_grant_approve_all(callback, request_id)
    elif action == 'grant_approve_safe':
        emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
        if _is_grant_expired(request_id, callback):
            return response(200, {'ok': True})
        return handle_grant_approve_safe(callback, request_id)
    elif action == 'grant_deny':
        emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
        if _is_grant_expired(request_id, callback):
            return response(200, {'ok': True})
        return handle_grant_deny(callback, request_id)
    elif action == 'grant_revoke':
        emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
        from grant import revoke_grant
        success = revoke_grant(request_id)
        message_id = callback.get('message', {}).get('message_id')
        if success:
            update_message(message_id, f"🛑 *Grant 已撤銷*\n\n`{request_id}`", remove_buttons=True)
            answer_callback(callback['id'], '🛑 Grant 已撤銷')
        else:
            answer_callback(callback['id'], '❌ 撤銷失敗')
        return response(200, {'ok': True})

    try:
        db_start = time.time()
        item = table.get_item(Key={'request_id': request_id}).get('Item')
        logger.debug(f"[TIMING] DynamoDB get_item: {(time.time() - db_start) * 1000:.0f}ms")
    except Exception as e:
        logger.error(f"Error: {e}")
        item = None

    if not item:
        emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
        answer_callback(callback['id'], '❌ 請求已過期或不存在')
        return response(404, {'error': 'Request not found'})

    # 取得 message_id（用於更新訊息）
    message_id = callback.get('message', {}).get('message_id')
    account_id = item.get('account_id', 'default')
    emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action, 'Account': account_id})

    if item['status'] not in ['pending_approval', 'pending']:
        # 已處理 fallback：只彈 toast，不覆蓋原本的完整訊息
        answer_callback(callback['id'], '⚠️ 此請求已處理過')
        return response(200, {'ok': True})

    # 檢查是否過期
    ttl = item.get('ttl', 0)
    if ttl and int(time.time()) > ttl:
        answer_callback(callback['id'], '⏰ 此請求已過期')
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'timeout'}
        )
        # 更新 Telegram 訊息，移除按鈕
        if message_id:
            source = item.get('source', '')
            command = item.get('command', '')
            reason = item.get('reason', '')
            context = item.get('context', '')
            source_line = f"🤖 *來源：* {escape_markdown(source)}\n" if source else ""
            context_line = f"📝 *任務：* {escape_markdown(context)}\n" if context else ""
            cmd_preview = command[:200] + '...' if len(command) > 200 else command
            update_message(
                message_id,
                f"⏰ *已過期*\n\n"
                f"{source_line}"
                f"{context_line}"
                f"📋 *命令：*\n`{cmd_preview}`\n\n"
                f"💬 *原因：* {escape_markdown(reason)}",
                remove_buttons=True
            )
        return response(200, {'ok': True, 'expired': True})

    # 根據請求類型處理
    request_action = item.get('action', 'execute')  # 預設是命令執行

    if request_action == 'add_account':
        return handle_account_add_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'remove_account':
        return handle_account_remove_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'deploy':
        return handle_deploy_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'upload':
        return handle_upload_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'upload_batch':
        return handle_upload_batch_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'deploy_frontend':
        return handle_deploy_frontend_callback(action, request_id, item, message_id, callback['id'], user_id)
    else:
        return handle_command_callback(action, request_id, item, message_id, callback['id'], user_id)


# ============================================================================
# HMAC 驗證
# ============================================================================

def verify_hmac(headers: dict, body: str) -> bool:
    """HMAC-SHA256 請求簽章驗證"""
    timestamp = headers.get('x-timestamp', '')
    nonce = headers.get('x-nonce', '')
    signature = headers.get('x-signature', '')

    if not all([timestamp, nonce, signature]):
        return False

    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > TELEGRAM_TIMESTAMP_MAX_AGE:
            return False
    except Exception as e:
        logger.error(f"Error: {e}")
        return False

    payload = f"{timestamp}.{nonce}.{body}"
    expected = hmac.new(
        REQUEST_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


# Notification functions moved to notifications.py — re-exported for backward compat
from notifications import send_approval_request, send_trust_session_summary  # noqa: F401, E402
