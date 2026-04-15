"""
Bouncer - Query Logs Callback 處理模組

處理 Telegram inline keyboard 的 query_logs 審批回調：
- approve_query_logs:{request_id}  → 一次性允許（執行查詢，不加入允許名單）
- approve_add_allowlist:{request_id} → 加入允許名單 + 執行查詢
- deny_query_logs:{request_id}     → 拒絕查詢請求
"""

import json
import time

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from db import table
from telegram import answer_callback, update_message, escape_markdown
from utils import response, format_size_human
from callbacks import _update_request_status
from mcp_query_logs import execute_log_insights, _add_to_allowlist
from metrics import emit_metric

logger = Logger(service="bouncer")


def handle_query_logs_callback(action: str, request_id: str, callback: dict, user_id: str) -> dict:
    """Handle query_logs approval callbacks.

    Actions:
        approve_query_logs:    Execute query one-time (no allowlist change)
        approve_add_allowlist: Add to allowlist + execute query
        deny_query_logs:       Deny the request
    """
    message_id = callback.get('message', {}).get('message_id')

    logger.info("Query logs callback received",
                extra={"src_module": "callbacks_query_logs", "operation": "handle_query_logs_callback",
                       "request_id": request_id, "action": action, "user_id": user_id})

    # Fetch from DDB
    try:
        item = table.get_item(Key={'request_id': request_id}).get('Item')
    except ClientError as e:
        logger.error("DynamoDB get_item error: %s", e,
                     extra={"src_module": "callbacks_query_logs", "operation": "get_item",
                            "request_id": request_id, "error": str(e)})
        item = None

    if not item:
        answer_callback(callback['id'], '❌ 請求已過期或不存在')
        return response(404, {'error': 'Request not found'})

    if item.get('status') != 'pending_approval':
        answer_callback(callback['id'], '⚠️ 此請求已處理過')
        return response(200, {'ok': True})

    # Check TTL
    item_ttl = int(item.get('ttl', 0))
    if item_ttl and int(time.time()) > item_ttl:
        answer_callback(callback['id'], '⏰ 此請求已過期')
        log_group = item.get('log_group', '')
        if message_id:
            update_message(
                message_id,
                f"⏰ *已過期*\n\n"
                f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
                f"🆔 `{request_id}`",
                remove_buttons=True,
            )
        return response(200, {'ok': True})

    # --- Deny ---
    if action == 'deny_query_logs':
        return _handle_deny(request_id, item, message_id, callback['id'], user_id)

    # --- Approve (approve_query_logs or approve_add_allowlist) ---
    return _handle_approve(action, request_id, item, message_id, callback['id'], user_id)


def _handle_deny(request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """Handle deny_query_logs callback."""
    answer_callback(callback_id, '❌ 已拒絕')
    _update_request_status(table, request_id, 'denied', user_id)

    logger.info("Approval action", extra={
        "src_module": "callbacks", "operation": "approval_action",
        "action": "deny",
        "request_id": request_id,
        "request_type": "query_logs",
        "user_id": str(user_id),
    })

    log_group = item.get('log_group', '')
    account_id = item.get('account_id', '')

    logger.info("Query logs denied: request_id=%s", request_id,
                extra={"src_module": "callbacks_query_logs", "operation": "deny",
                       "request_id": request_id, "log_group": log_group})

    if message_id:
        update_message(
            message_id,
            f"❌ *已拒絕 Log 查詢*\n\n"
            f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
            f"🏦 *Account：* `{account_id}`\n"
            f"🆔 `{request_id}`",
            remove_buttons=True,
        )

    emit_metric('Bouncer', 'QueryLogsApproval', 1, dimensions={'Action': 'deny'})
    return response(200, {'ok': True})


def _handle_approve(action: str, request_id: str, item: dict, message_id: int,
                    callback_id: str, user_id: str) -> dict:
    """Handle approve_query_logs or approve_add_allowlist callback."""
    answer_callback(callback_id, '✅ 查詢中...')

    log_group = item.get('log_group', '')
    account_id = item.get('account_id', '')
    assume_role_arn = item.get('assume_role_arn', '') or None
    query = item.get('query', '')
    start_time = int(item.get('start_time', 0))
    end_time = int(item.get('end_time', 0))
    region = item.get('region', '')

    # Add to allowlist if requested
    if action == 'approve_add_allowlist':
        _add_to_allowlist(account_id, log_group, added_by=f'telegram:{user_id}')
        logger.info("Added to allowlist via approval: %s (account=%s)", log_group, account_id,
                     extra={"src_module": "callbacks_query_logs", "operation": "add_to_allowlist",
                            "request_id": request_id, "log_group": log_group, "account_id": account_id})

    # Show progress message
    if message_id:
        update_message(
            message_id,
            f"⏳ *查詢執行中...*\n\n"
            f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
            f"🏦 *Account：* `{account_id}`\n"
            f"🆔 `{request_id}`",
            remove_buttons=True,
        )

    # Execute query
    result_data = execute_log_insights(
        log_group=log_group, query_with_limit=query,
        start_time=start_time, end_time=end_time,
        region=region, assume_role_arn=assume_role_arn, account_id=account_id,
    )

    # Handle error
    if result_data.get('status') == 'error':
        error_msg = result_data.get('error', 'Unknown error')
        _update_request_status(table, request_id, 'approved', user_id, extra_attrs={
            'result': json.dumps({'status': 'error', 'error': error_msg}, ensure_ascii=False),
            'decision_type': 'manual_approved',
        })
        if message_id:
            update_message(
                message_id,
                f"✅ *已批准但查詢失敗*\n\n"
                f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
                f"❌ *錯誤：* {escape_markdown(error_msg[:200])}\n"
                f"🆔 `{request_id}`",
            )
        logger.info("Query logs approved but query failed: request_id=%s", request_id,
                     extra={"src_module": "callbacks_query_logs", "operation": "approve_error",
                            "request_id": request_id, "error": error_msg[:200]})
        return response(200, {'ok': True})

    # Handle still running
    if result_data.get('status') == 'running':
        query_id = result_data.get('query_id', '')
        _update_request_status(table, request_id, 'approved', user_id, extra_attrs={
            'result': json.dumps({'status': 'running', 'query_id': query_id}, ensure_ascii=False),
            'decision_type': 'manual_approved',
        })
        if message_id:
            update_message(
                message_id,
                f"✅ *已批准*\n\n"
                f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
                f"⏳ 查詢仍在執行中（query\\_id: `{query_id}`）\n"
                f"🆔 `{request_id}`",
            )
        logger.info("Query logs approved, query still running: request_id=%s, query_id=%s",
                     request_id, query_id,
                     extra={"src_module": "callbacks_query_logs", "operation": "approve_running",
                            "request_id": request_id, "query_id": query_id})
        return response(200, {'ok': True})

    # Handle complete
    stats = result_data.get('statistics', {})
    records = result_data.get('records_matched', 0)
    bytes_scanned = stats.get('bytes_scanned', 0)
    bytes_str = format_size_human(int(bytes_scanned)) if bytes_scanned else '0 bytes'

    # Store result in DDB (truncated for item size)
    result_json = json.dumps(result_data, ensure_ascii=False)
    if len(result_json) > 4000:
        truncated_data = dict(result_data)
        truncated_data['results'] = truncated_data.get('results', [])[:20]
        truncated_data['truncated'] = True
        result_json = json.dumps(truncated_data, ensure_ascii=False)
        if len(result_json) > 4000:
            result_json = result_json[:4000]

    extra_attrs = {
        'result': result_json,
        'decision_type': 'manual_approved',
    }
    if action == 'approve_add_allowlist':
        extra_attrs['added_to_allowlist'] = True

    _update_request_status(table, request_id, 'approved', user_id, extra_attrs=extra_attrs)

    logger.info("Approval action", extra={
        "src_module": "callbacks", "operation": "approval_action",
        "action": "approve" if action != 'approve_add_allowlist' else "approve_add_allowlist",
        "request_id": request_id,
        "request_type": "query_logs",
        "user_id": str(user_id),
    })

    # Build approval message
    if action == 'approve_add_allowlist':
        title = '已批准並加入允許名單'
    else:
        title = '已批准 Log 查詢（一次性）'

    if message_id:
        update_message(
            message_id,
            f"✅ *{title}*\n\n"
            f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
            f"🏦 *Account：* `{account_id}`\n"
            f"📊 {records} 筆記錄 | 掃描 {bytes_str}\n"
            f"🆔 `{request_id}`",
        )

    logger.info("Query logs approved: request_id=%s, action=%s, records=%d",
                request_id, action, records,
                extra={"src_module": "callbacks_query_logs", "operation": "approve_complete",
                       "request_id": request_id, "action": action, "records": records})

    emit_metric('Bouncer', 'QueryLogsApproval', 1, dimensions={'Action': action})
    return response(200, {'ok': True})
