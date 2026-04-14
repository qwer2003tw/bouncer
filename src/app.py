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
import time
import unicodedata
import urllib.error

from botocore.exceptions import ClientError


# 從模組導入
from telegram import (  # noqa: F401
    escape_markdown,
    update_message, answer_callback,
    send_chat_action,
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
from caller_identity import identify_caller
from rate_limit import check_rate_limit, RateLimitExceeded, PendingLimitExceeded  # noqa: F401
from paging import store_paged_output, get_paged_output  # noqa: F401
from utils import response, generate_request_id, decimal_to_native, mcp_result, mcp_error, get_header, log_decision, generate_display_summary
# MCP tool handlers — split into sub-modules
from mcp_execute import mcp_tool_execute_native, mcp_tool_eks_get_token
from mcp_grant import (
    mcp_tool_request_grant, mcp_tool_grant_status, mcp_tool_revoke_grant,
    mcp_tool_grant_execute,
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
from mcp_deploy_frontend import (
    mcp_tool_request_frontend_presigned,
    mcp_tool_confirm_frontend_deploy,
)
from mcp_query_logs import mcp_tool_query_logs, mcp_tool_logs_allowlist
from callbacks import (
    _is_execute_failed,
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
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")


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
        logger.warning("Missing request_id in event", extra={"src_module": "cleanup", "operation": "handle_cleanup_expired"})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'missing_request_id'})

    logger.info("Processing expiry for request_id=%s", request_id, extra={"src_module": "cleanup", "operation": "handle_cleanup_expired", "request_id": request_id})

    try:
        item = table.get_item(Key={'request_id': request_id}).get('Item')
    except ClientError as e:
        logger.error("DynamoDB error for request_id=%s: %s", request_id, e, extra={"src_module": "cleanup", "operation": "get_item", "request_id": request_id, "error": str(e)})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'db_error'})

    if not item:
        # Fallback: use message_id from schedule event payload
        fallback_msg_id = event.get('telegram_message_id')
        if fallback_msg_id:
            try:
                update_message(int(fallback_msg_id), "⏰ 此請求已過期", remove_buttons=True)
            except Exception as e:  # noqa: BLE001 — fire-and-forget
                logger.warning("Fallback message update failed: %s", e, extra={"src_module": "cleanup", "operation": "fallback_update", "request_id": request_id, "error": str(e)})
        logger.info("Request %s not found — %s", request_id, "buttons cleared via fallback" if fallback_msg_id else "skipping", extra={"src_module": "cleanup", "operation": "not_found", "request_id": request_id})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'not_found'})

    current_status = item.get('status', 'pending')

    # Already actioned — no-op
    if current_status in ('approved', 'rejected', 'denied', 'timeout', 'auto_approved'):
        logger.info("Request %s already %s — no-op", request_id, current_status, extra={"src_module": "cleanup", "operation": "no_op", "request_id": request_id, "current_status": current_status})
        return response(200, {'ok': True, 'skipped': True, 'reason': f'already_{current_status}'})

    # Retrieve stored telegram_message_id
    telegram_message_id = item.get('telegram_message_id')
    if not telegram_message_id:
        logger.info("Request %s has no telegram_message_id — cannot update message", request_id, extra={"src_module": "cleanup", "operation": "no_message_id", "request_id": request_id})
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
    elif action_type == 'query_logs':
        log_group = item.get('log_group', '')
        account_id = item.get('account_id', '')
        detail_line = f"📁 *Log Group：* `{log_group}`\n🏦 *Account：* `{account_id}`\n"
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
        logger.info("Updated Telegram message %s for request %s", telegram_message_id, request_id, extra={"src_module": "cleanup", "operation": "update_message", "request_id": request_id, "message_id": telegram_message_id})
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as exc:
        logger.warning("Failed to update Telegram message %s: %s", telegram_message_id, exc, extra={"src_module": "cleanup", "operation": "update_message", "request_id": request_id, "error": str(exc)})
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
    except ClientError as exc:
        logger.error("Failed to update DynamoDB status for %s: %s", request_id, exc, extra={"src_module": "cleanup", "operation": "mark_timeout", "request_id": request_id, "error": str(exc)})


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
        logger.warning("Missing trust_id in event", extra={"src_module": "trust_expiry", "operation": "handle_trust_expiry"})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'missing_trust_id'})

    logger.info("Processing expiry notification for trust_id=%s", trust_id, extra={"src_module": "trust_expiry", "operation": "handle_trust_expiry", "trust_id": trust_id})

    # Fetch the trust session item to get source + trust_scope
    try:
        trust_item = table.get_item(Key={'request_id': trust_id}).get('Item')
    except ClientError as exc:
        logger.error("DynamoDB error fetching trust session %s: %s", trust_id, exc, extra={"src_module": "trust_expiry", "operation": "get_item", "trust_id": trust_id, "error": str(exc)})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'db_error'})

    if not trust_item:
        logger.info("Trust session %s not found (already revoked?) — skipping", trust_id, extra={"src_module": "trust_expiry", "operation": "not_found", "trust_id": trust_id})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'not_found'})

    # De-duplicate: skip if summary was already sent (e.g. session was revoked first)
    if trust_item.get('summary_sent'):
        logger.info("Summary already sent for trust_id=%s — skipping", trust_id, extra={"src_module": "trust_expiry", "operation": "already_sent", "trust_id": trust_id})
        return response(200, {'ok': True, 'skipped': True, 'reason': 'summary_already_sent'})

    source = trust_item.get('source', '') or trust_item.get('bound_source', '')
    trust_scope = trust_item.get('trust_scope', '')

    # Send execution summary (sprint9-007-phase-b)
    try:
        send_trust_session_summary(trust_item, end_reason='expiry')
        # Mark summary_sent=True to prevent duplicate on revoke (best-effort)
        try:
            table.update_item(
                Key={'request_id': trust_id},
                UpdateExpression='SET summary_sent = :t',
                ExpressionAttributeValues={':t': True},
            )
        except ClientError as _mark_exc:
            logger.warning("Failed to mark summary_sent for %s: %s", trust_id, _mark_exc, extra={"src_module": "trust_expiry", "operation": "mark_summary_sent", "trust_id": trust_id, "error": str(_mark_exc)})
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as _sum_exc:
        logger.error("send_trust_session_summary error for %s: %s", trust_id, _sum_exc, extra={"src_module": "trust_expiry", "operation": "send_summary", "trust_id": trust_id, "error": str(_sum_exc)})

    # Query pending requests that match source + trust_scope
    pending_requests = _query_pending_for_trust(source=source, trust_scope=trust_scope)
    pending_count = len(pending_requests)

    logger.info("trust_id=%s source=%r trust_scope=%r pending_count=%d", trust_id, source, trust_scope, pending_count, extra={"src_module": "trust_expiry", "operation": "query_pending", "trust_id": trust_id, "pending_count": pending_count})

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
        logger.warning("No source or trust_scope — cannot query pending requests", extra={"src_module": "trust_expiry", "operation": "query_pending"})
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
        logger.info("Found %d pending_approval items for source=%r", len(items), effective_source, extra={"src_module": "trust_expiry", "operation": "query_pending", "count": len(items)})
        return items
    except ClientError as exc:
        logger.error("Failed to query pending requests for source=%r: %s", effective_source, exc, extra={"src_module": "trust_expiry", "operation": "query_pending", "error": str(exc)})
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
    from telegram import send_telegram_message, send_telegram_message_silent

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
            cmd_preview = (
                item.get('command') or
                item.get('display_summary') or
                item.get('action', 'unknown action')
            )[:60]
            lines.append(f"  {i + 1}. `{req_id}` — `{escape_markdown(cmd_preview)}`")
        if pending_count > 5:
            lines.append(f"  _{pending_count - 5} 個更多..._ ")

        pending_details = "\n".join(lines)
        text = (
            f"⏰ *信任時段已過期*\n\n"
            f"🤖 *來源：* {safe_source}\n"
            f"🔑 `{trust_id}`\n\n"
            f"⚠️ *{pending_count} 個 pending 請求需手動審批：*\n"
            f"{pending_details}"
        )

    try:
        if pending_count > 0:
            send_telegram_message(text)   # ring: pending requests need manual approval
        else:
            send_telegram_message_silent(text)  # silent: no action needed
        logger.info("Sent expiry notification for trust_id=%s (pending=%d, ring=%s)", trust_id, pending_count, pending_count > 0, extra={"src_module": "trust_expiry", "operation": "send_notification", "trust_id": trust_id, "pending_count": pending_count})
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as exc:
        logger.error("Failed to send Telegram notification for trust %s: %s", trust_id, exc, extra={"src_module": "trust_expiry", "operation": "send_notification", "trust_id": trust_id, "error": str(exc)})


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

    # EventBridge Scheduler approval expiry warning (sprint35-003)
    if event.get('source') == 'bouncer-scheduler' and event.get('action') == 'expiry_warning':
        # s56-004: Check DDB status before sending warning (skip if already approved/denied)
        request_id = event.get('request_id', '')
        from db import table as _table
        try:
            ddb_response = _table.get_item(Key={'request_id': request_id})
            item = ddb_response.get('Item')
            if not item or item.get('status') != 'pending_approval':
                # Already approved/denied or not found, skip warning
                logger.info("Skipped expiry warning for %s (status=%s)", request_id, item.get('status') if item else 'not_found', extra={"src_module": "app", "operation": "expiry_warning", "request_id": request_id})
                return {'statusCode': 200, 'body': json.dumps({'status': 'skipped'})}
        except Exception as exc:
            logger.error("Failed to check status for expiry warning %s: %s", request_id, exc, extra={"src_module": "app", "operation": "expiry_warning", "request_id": request_id})
            # On error, skip warning (conservative approach)
            return {'statusCode': 200, 'body': json.dumps({'status': 'error'})}

        # s57-002: Update original message instead of sending new notification
        telegram_message_id = item.get('telegram_message_id') or item.get('message_id')
        if telegram_message_id:
            try:
                from telegram import update_message
                command_preview = event.get('command_preview', '')
                update_message(
                    int(telegram_message_id),
                    f"❌ *審批已過期*\n\n"
                    f"📋 *請求 ID：* `{request_id}`\n"
                    f"📋 *命令：* `{command_preview[:100]}`\n\n"
                    f"請重新發起請求。",
                    remove_buttons=True,
                )
                logger.info("Updated expired message %s", telegram_message_id, extra={"src_module": "app", "operation": "expiry_warning", "request_id": request_id})
            except Exception as exc:
                logger.warning("Failed to update expired message %s: %s", telegram_message_id, exc, extra={"src_module": "app", "operation": "expiry_warning", "request_id": request_id})
                # Fallback to sending new notification
                from notifications import send_expiry_warning_notification
                send_expiry_warning_notification(
                    request_id=request_id,
                    command_preview=event.get('command_preview', ''),
                    source=event.get('source_field', ''),
                )
        else:
            # No message_id, fallback to sending new notification
            logger.info("No telegram_message_id found for %s, sending new notification", request_id, extra={"src_module": "app", "operation": "expiry_warning", "request_id": request_id})
            from notifications import send_expiry_warning_notification
            send_expiry_warning_notification(
                request_id=request_id,
                command_preview=event.get('command_preview', ''),
                source=event.get('source_field', ''),
            )

        # Update DDB status to expired
        try:
            _table.update_item(
                Key={'request_id': request_id},
                UpdateExpression='SET #s = :s',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':s': 'expired'},
            )
        except Exception as exc:
            logger.warning("Failed to update DDB status for %s: %s", request_id, exc, extra={"src_module": "app", "operation": "expiry_warning", "request_id": request_id})

        return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}

    # EventBridge Scheduler pending approval reminder (sprint59-001)
    if event.get('source') == 'bouncer-scheduler' and event.get('action') == 'pending_reminder':
        request_id = event.get('request_id', '')
        from db import table as _table
        try:
            ddb_response = _table.get_item(Key={'request_id': request_id})
            item = ddb_response.get('Item')
            if not item or item.get('status') != 'pending_approval':
                # Already approved/denied or not found, skip reminder
                logger.info("Skipped reminder for %s (status=%s)", request_id, item.get('status') if item else 'not_found', extra={"src_module": "app", "operation": "pending_reminder", "request_id": request_id})
                return {'statusCode': 200, 'body': json.dumps({'status': 'skipped'})}
        except Exception as exc:
            logger.error("Failed to check status for reminder %s: %s", request_id, exc, extra={"src_module": "app", "operation": "pending_reminder", "request_id": request_id})
            return {'statusCode': 200, 'body': json.dumps({'status': 'error'})}

        # Send Telegram reminder
        try:
            from telegram import send_telegram_message_silent, escape_markdown
            command_preview = event.get('command_preview', '')[:100]
            source_field = event.get('source_field', '')
            expires_at = int(item.get('expires_at', 0))
            is_escalation = event.get('escalation', False)

            # Format expires_at as human-readable
            from datetime import datetime, timezone
            expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
            expires_str = expires_dt.strftime('%Y-%m-%d %H:%M:%S UTC')

            # Use different header for escalation (2nd reminder)
            header = "🔴 *第 2 次提醒 — 尚未審批的請求*" if is_escalation else "⏰ *尚未審批的請求*"

            text = (
                f"{header}\n\n"
                f"📋 *命令：* `{escape_markdown(command_preview)}`\n"
                f"🤖 *來源：* {escape_markdown(source_field)}\n"
                f"🆔 `{request_id}`\n"
                f"⌛ *到期：* {expires_str}"
            )
            send_telegram_message_silent(text)
            logger.info("Sent pending reminder for %s", request_id, extra={"src_module": "app", "operation": "pending_reminder", "request_id": request_id})
        except Exception as exc:
            logger.warning("Failed to send reminder for %s: %s", request_id, exc, extra={"src_module": "app", "operation": "pending_reminder", "request_id": request_id, "error": str(exc)})

        return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}

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

    # 驗證 secret and identify caller
    caller = identify_caller(get_header(headers, 'x-approval-secret'))
    if caller is None:
        return mcp_error(None, -32600, 'Invalid credentials')

    # Extract caller_ip from API Gateway event for trust session IP binding
    caller_ip = (
        event.get('requestContext', {}).get('identity', {}).get('sourceIp', '')
        or event.get('requestContext', {}).get('http', {}).get('sourceIp', '')
        or ''
    )

    # 解析 JSON-RPC
    try:
        body = json.loads(event.get('body', '{}'))
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("JSON parse error: %s", e, extra={"src_module": "mcp", "operation": "handle_mcp_request", "error": str(e)})
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
        # Inject caller info and override source
        arguments['_caller'] = caller
        arguments['source'] = caller.get('source', arguments.get('source', 'unknown'))
        return handle_mcp_tool_call(req_id, tool_name, arguments, caller_ip)

    else:
        return mcp_error(req_id, -32601, f'Method not found: {method}')


# ---------------------------------------------------------------------------
# Tool handler dispatch table
# ---------------------------------------------------------------------------
# Standard handlers: (req_id, arguments) -> dict
TOOL_HANDLERS = {
    'bouncer_execute_native': mcp_tool_execute_native,
    'bouncer_eks_get_token': mcp_tool_eks_get_token,
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
    'bouncer_grant_execute': mcp_tool_grant_execute,
    'bouncer_history': mcp_tool_history,
    'bouncer_stats': mcp_tool_stats,
    'bouncer_request_frontend_presigned': mcp_tool_request_frontend_presigned,
    'bouncer_confirm_frontend_deploy': mcp_tool_confirm_frontend_deploy,
    'bouncer_query_logs': mcp_tool_query_logs,
    'bouncer_logs_allowlist': mcp_tool_logs_allowlist,
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


def handle_mcp_tool_call(req_id, tool_name: str, arguments: dict, caller_ip: str = '') -> dict:
    """處理 MCP tool 呼叫"""
    emit_metric('Bouncer', 'ToolCall', 1, dimensions={'ToolName': tool_name})
    send_chat_action('typing')

    # Inject caller_ip into arguments for trust session IP binding
    if caller_ip:
        arguments = {**arguments, 'caller_ip': caller_ip}

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

    caller = identify_caller(get_header(headers, 'x-approval-secret'))
    if caller is None:
        return response(403, {'error': 'Invalid credentials'})

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

    except Exception as e:  # noqa: BLE001 — Lambda handler entry point
        logger.exception(f"[Lambda] get_request error: {e}")
        return response(500, {'error': str(e)})


def handle_clawdbot_request(event: dict) -> dict:
    """處理 REST API 的命令執行請求（向後兼容）"""
    headers = event.get('headers', {})

    caller = identify_caller(get_header(headers, 'x-approval-secret'))
    if caller is None:
        return response(403, {'error': 'Invalid credentials'})

    if ENABLE_HMAC:
        body_str = event.get('body', '')
        if not verify_hmac(headers, body_str):
            return response(403, {'error': 'Invalid HMAC signature'})

    try:
        body = json.loads(event.get('body', '{}'))
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("JSON parse error: %s", e, extra={"src_module": "rest", "operation": "handle_clawdbot_request", "error": str(e)})
        return response(400, {'error': 'Invalid JSON'})

    command = unicodedata.normalize('NFKC', body.get('command', '')).strip()
    reason = body.get('reason', 'No reason provided')
    source = caller.get('source', body.get('source', 'unknown'))  # Override with caller source
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
        logger.error("compliance_checker module import failed - failing closed")
        return response(500, {
            'error': 'Compliance checker module unavailable - request rejected for safety'
        })

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
        cmd_status = 'failed' if _is_execute_failed(result) else 'success'
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
            command_status=cmd_status,
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

def handle_telegram_webhook(event: dict) -> dict:
    """處理 Telegram callback (request validation and routing to webhook_router)"""
    from webhook_router import (
        handle_show_page,
        handle_infra_approval,
        handle_revoke_trust,
        handle_grant_callbacks,
        handle_query_logs_callbacks,
        handle_general_approval,
    )

    headers = event.get('headers', {})

    # Extract source_ip from API Gateway event for audit trail (#74)
    # Note: in webhook mode this is Telegram server IP, not the end-user IP.
    # Used to verify requests originate from Telegram, not a forged source.
    source_ip = (
        event.get('requestContext', {}).get('identity', {}).get('sourceIp', '')
        or event.get('requestContext', {}).get('http', {}).get('sourceIp', '')
        or ''
    )

    if TELEGRAM_WEBHOOK_SECRET:
        received_secret = get_header(headers, 'x-telegram-bot-api-secret-token') or ''
        if received_secret != TELEGRAM_WEBHOOK_SECRET:
            return response(403, {'error': 'Invalid webhook signature'})

    try:
        body = json.loads(event.get('body', '{}'))
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("JSON parse error in webhook: %s", e, extra={"src_module": "webhook", "operation": "handle_telegram_webhook", "error": str(e)})
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

    # Route to specialized handlers
    if action == 'show_page':
        return handle_show_page(request_id, callback)

    if action in ('infra_approve', 'infra_deny'):
        return handle_infra_approval(action, request_id, callback, user_id)

    if action == 'revoke_trust':
        return handle_revoke_trust(request_id, callback)

    if action in ('grant_approve_all', 'grant_approve_safe', 'grant_deny', 'grant_revoke'):
        return handle_grant_callbacks(action, request_id, callback)

    if action in ('approve_query_logs', 'approve_add_allowlist', 'deny_query_logs'):
        return handle_query_logs_callbacks(action, request_id, callback, user_id)

    # General approval flow
    return handle_general_approval(action, request_id, callback, user_id, source_ip)


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
    except ValueError as e:
        logger.error("HMAC timestamp parse error: %s", e, extra={"src_module": "hmac", "operation": "verify_hmac", "error": str(e)})
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
