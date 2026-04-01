"""
Webhook callback routing logic extracted from app.py (sprint61-003)

This module contains special-case handlers for Telegram webhook callbacks,
separated from the main handle_telegram_webhook request validation flow.
"""

import time
import urllib.error
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from telegram import answer_callback, update_message, escape_markdown
from utils import response
from metrics import emit_metric
from db import table
from callbacks import (
    handle_show_page_callback,
    handle_command_callback,
    handle_account_add_callback,
    handle_account_remove_callback,
    handle_deploy_callback,
    handle_upload_callback,
    handle_upload_batch_callback,
    handle_deploy_frontend_callback,
    handle_grant_approve_all,
    handle_grant_approve_safe,
    handle_grant_deny,
)
from callbacks_query_logs import handle_query_logs_callback
from trust import revoke_trust_session
from notifications import send_trust_session_summary

logger = Logger(service="bouncer")


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
            except ClientError:
                logger.warning("[GRANT EXPIRY] Failed to update DDB status=timeout for request_id=%s", request_id, extra={"src_module": "grant_expiry", "operation": "mark_timeout", "request_id": request_id}, exc_info=True)
            return True
    except ClientError as e:
        logger.error("Error checking grant TTL: %s", e, extra={"src_module": "grant_expiry", "operation": "check_ttl", "request_id": request_id, "error": str(e)})
    return False


def handle_show_page(request_id: str, callback: dict) -> dict:
    """Handle show_page callback for on-demand pagination (sprint13-003).

    Callback data format: 'show_page:{original_request_id}:{page_num}'
    """
    parts = request_id.rsplit(':', 1)
    if len(parts) == 2:
        orig_request_id, page_num_str = parts
        try:
            page_num = int(page_num_str)
        except ValueError:
            answer_callback(callback['id'], '❌ 無效頁碼')
            return response(400, {'error': 'Invalid page number'})
        emit_metric('Bouncer', 'PageView', 1, dimensions={'Action': 'show_page'})
        return handle_show_page_callback(callback, orig_request_id, page_num)
    else:
        answer_callback(callback['id'], '❌ 無效分頁請求')
        return response(400, {'error': 'Invalid show_page data'})


def handle_infra_approval(action: str, request_id: str, callback: dict, user_id: str) -> dict:
    """Handle infra_approve/infra_deny callbacks for WaitForInfraApproval SFN state."""
    from deployer import get_deploy_record, update_deploy_record
    import boto3 as _boto3
    import json as _json
    import time as _time

    deploy_id = request_id
    deploy_record = get_deploy_record(deploy_id)
    task_token = deploy_record.get('infra_approval_token', '') if deploy_record else ''
    msg_id = callback.get('message', {}).get('message_id')

    if not task_token:
        answer_callback(callback['id'], '❌ 找不到 task token，可能已過期')
        return response(200, {'ok': True})

    # SEC: verify token TTL has not expired
    token_ttl = int(deploy_record.get('infra_approval_token_ttl', 0)) if deploy_record else 0
    if token_ttl and int(_time.time()) > token_ttl:
        logger.warning("infra_approve rejected: token expired for %s", deploy_id, extra={"src_module": "app", "operation": "infra_approve", "deploy_id": deploy_id, "token_ttl": token_ttl})
        answer_callback(callback['id'], '❌ 審批已過期，請重新發起部署')
        update_message(msg_id, '❌ *審批已過期*\n\n`' + deploy_id + '`\n\n請重新呼叫 bouncer_deploy。', remove_buttons=True)
        return response(200, {'ok': True})

    sfn = _boto3.client('stepfunctions', region_name='us-east-1')
    if action == 'infra_approve':
        sfn.send_task_success(
            taskToken=task_token,
            output=_json.dumps({'approved': True, 'deploy_id': deploy_id}),
        )
        update_deploy_record(deploy_id, {'infra_approval_status': 'APPROVED'})
        answer_callback(callback['id'], '✅ 已批准，繼續部署')
        update_message(msg_id, '✅ *Infra 變更已批准*\n\n`' + deploy_id + '`\n\n正在繼續部署...', remove_buttons=True)
    else:
        sfn.send_task_failure(
            taskToken=task_token,
            error='InfraDenied',
            cause=f'Denied by {user_id}',
        )
        update_deploy_record(deploy_id, {'infra_approval_status': 'DENIED'})
        answer_callback(callback['id'], '❌ 已拒絕部署')
        update_message(msg_id, '❌ *Infra 變更已拒絕*\n\n`' + deploy_id + '`', remove_buttons=True)
    return response(200, {'ok': True})


def handle_revoke_trust(request_id: str, callback: dict) -> dict:
    """Handle revoke_trust callback to end a trust session."""
    emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': 'revoke_trust'})

    # Fetch trust item before deletion for summary (sprint9-007-phase-a)
    trust_item_for_summary = None
    try:
        _resp = table.get_item(Key={'request_id': request_id})
        trust_item_for_summary = _resp.get('Item')
    except ClientError as _e:
        logger.warning('Failed to fetch trust item for summary: %s', _e, extra={"src_module": "webhook", "operation": "revoke_trust", "request_id": request_id, "error": str(_e)})

    success = revoke_trust_session(request_id)
    emit_metric('Bouncer', 'TrustSession', 1, dimensions={'Event': 'revoked'})
    message_id = callback.get('message', {}).get('message_id')

    if success:
        update_message(message_id, f"🛑 *信任時段已結束*\n\n`{request_id}`", remove_buttons=True)
        answer_callback(callback['id'], '🛑 信任已結束')
        # Send execution summary (sprint9-007-phase-a/b)
        # Skip if summary already sent by expiry handler (sprint9-007-phase-b)
        if trust_item_for_summary and not trust_item_for_summary.get('summary_sent'):
            try:
                send_trust_session_summary(trust_item_for_summary)
            except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as _se:
                logger.error('send_trust_session_summary error: %s', _se, extra={"src_module": "webhook", "operation": "revoke_trust_summary", "request_id": request_id, "error": str(_se)})
    else:
        answer_callback(callback['id'], '❌ 撤銷失敗')
    return response(200, {'ok': True})


def handle_grant_callbacks(action: str, request_id: str, callback: dict) -> dict:
    """Handle grant session callbacks (approve_all, approve_safe, deny, revoke)."""
    emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})

    if action == 'grant_approve_all':
        if _is_grant_expired(request_id, callback):
            return response(200, {'ok': True})
        return handle_grant_approve_all(callback, request_id)
    elif action == 'grant_approve_safe':
        if _is_grant_expired(request_id, callback):
            return response(200, {'ok': True})
        return handle_grant_approve_safe(callback, request_id)
    elif action == 'grant_deny':
        if _is_grant_expired(request_id, callback):
            return response(200, {'ok': True})
        return handle_grant_deny(callback, request_id)
    elif action == 'grant_revoke':
        from grant import revoke_grant
        success = revoke_grant(request_id)
        message_id = callback.get('message', {}).get('message_id')
        if success:
            update_message(message_id, f"🛑 *Grant 已撤銷*\n\n`{request_id}`", remove_buttons=True)
            answer_callback(callback['id'], '🛑 Grant 已撤銷')
        else:
            answer_callback(callback['id'], '❌ 撤銷失敗')
        return response(200, {'ok': True})
    else:
        # Should not reach here, but return error for safety
        return response(400, {'error': f'Unknown grant action: {action}'})


def handle_query_logs_callbacks(action: str, request_id: str, callback: dict, user_id: str) -> dict:
    """Handle query_logs approval callbacks (approve_query_logs, approve_add_allowlist, deny_query_logs)."""
    emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})
    return handle_query_logs_callback(action, request_id, callback, user_id)


def handle_general_approval(action: str, request_id: str, callback: dict, user_id: str, source_ip: str) -> dict:
    """Handle general approval flow: DynamoDB lookup, TTL check, and action dispatch."""
    try:
        db_start = time.time()
        item = table.get_item(Key={'request_id': request_id}).get('Item')
        logger.debug("DynamoDB get_item: %dms", (time.time() - db_start) * 1000, extra={"src_module": "webhook", "operation": "get_item", "request_id": request_id})
    except ClientError as e:
        logger.error("DynamoDB get_item error: %s", e, extra={"src_module": "webhook", "operation": "get_item", "request_id": request_id, "error": str(e)})
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
        return handle_command_callback(action, request_id, item, message_id, callback['id'], user_id, source_ip=source_ip)
