"""
Bouncer - Telegram Callback 處理模組

所有 handle_*_callback 函數
"""

import time
import urllib.error

from botocore.exceptions import ClientError

from aws_clients import get_s3_client
from aws_lambda_powertools import Logger


# 從其他模組導入
from utils import response, format_size_human, build_info_lines
from commands import execute_command, is_dangerous
from paging import store_paged_output, get_paged_output
from trust import create_trust_session, track_command_executed
from telegram import escape_markdown, update_message, answer_callback, send_telegram_message_silent, pin_message, send_chat_action
from notifications import send_trust_auto_approve_notification
from constants import DEFAULT_ACCOUNT_ID, RESULT_TTL, TRUST_SESSION_MAX_UPLOADS, TRUST_SESSION_MAX_COMMANDS
from metrics import emit_metric
from mcp_upload import execute_upload, _verify_upload


# DynamoDB tables from db.py (no circular dependency)
import db as _db


def _is_execute_failed(output: str) -> bool:
    """判斷 execute_command 輸出是否代表失敗。
    支援：❌ prefix（Bouncer 格式）和 (exit code: N) 格式（AWS CLI 直接輸出）。
    """
    from utils import extract_exit_code
    code = extract_exit_code(output)
    return code is not None and code != 0


logger = Logger(service="bouncer")


def _get_table():
    """取得 DynamoDB table"""
    return _db.table

def _get_accounts_table():
    """取得 accounts DynamoDB table"""
    return _db.accounts_table


# ============================================================================
# Grant Session Callbacks
# ============================================================================

def handle_grant_approve(query: dict, grant_id: str, mode: str = 'all') -> dict:
    """處理 Grant 批准 callback

    Args:
        query: Telegram callback query
        grant_id: Grant session ID
        mode: 'all' 全部批准 | 'safe_only' 只批准安全命令
    """
    from grant import approve_grant, get_grant_session

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    mode_label = '全部' if mode == 'all' else '僅安全'

    try:
        # Check if grant contains dangerous commands before approving
        show_alert = False
        if mode == 'all':
            grant_check = get_grant_session(grant_id)
            if grant_check:
                commands_detail = grant_check.get('commands_detail', [])
                has_dangerous = any(d.get('category') == 'requires_individual' for d in commands_detail)
                if has_dangerous:
                    show_alert = True

        grant = approve_grant(grant_id, user_id, mode=mode)
        if not grant:
            answer_callback(callback_id, '❌ Grant 不存在或已處理')
            return response(200, {'ok': True})

        granted = grant.get('granted_commands', [])
        ttl_minutes = grant.get('ttl_minutes', 30)

        cb_suffix = '命令' if mode == 'all' else '安全命令'

        if show_alert:
            answer_callback(callback_id, f'⚠️ 高危 Grant 確認：已批准 {len(granted)} 個{cb_suffix}', show_alert=True)
        else:
            answer_callback(callback_id, f'✅ 已批准 {len(granted)} 個{cb_suffix}')
        update_message(
            message_id,
            f"✅ *Grant 已批准（{mode_label}）*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"📋 *已授權命令：* {len(granted)} 個\n"
            f"⏱ *有效時間：* {ttl_minutes} 分鐘\n"
            f"👤 *批准者：* {user_id}",
        )

        return response(200, {'ok': True})

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
        logger.error(f"[GRANT] handle_grant_approve error (mode={mode}): {e}", extra={"src_module": "grant", "operation": "handle_grant_approve", "mode": mode, "error": str(e)})
        answer_callback(callback_id, f'❌ 批准失敗: {str(e)[:50]}')
        return response(500, {'error': str(e)})


# Backward-compatible aliases
def handle_grant_approve_all(query: dict, grant_id: str) -> dict:
    """處理 Grant 全部批准 callback"""
    return handle_grant_approve(query, grant_id, mode='all')


def handle_grant_approve_safe(query: dict, grant_id: str) -> dict:
    """處理 Grant 只批准安全命令 callback"""
    return handle_grant_approve(query, grant_id, mode='safe_only')


def handle_grant_deny(query: dict, grant_id: str) -> dict:
    """處理 Grant 拒絕 callback"""
    from grant import deny_grant

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    try:
        success = deny_grant(grant_id)
        if not success:
            answer_callback(callback_id, '❌ 拒絕失敗')
            return response(200, {'ok': True})

        answer_callback(callback_id, '❌ 已拒絕')
        update_message(
            message_id,
            f"❌ *Grant 已拒絕*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"👤 *拒絕者：* {user_id}",
        )

        return response(200, {'ok': True})

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
        logger.error(f"[GRANT] handle_grant_deny error: {e}", extra={"src_module": "grant", "operation": "handle_grant_deny", "error": str(e)})
        answer_callback(callback_id, f'❌ 處理失敗: {str(e)[:50]}')
        return response(500, {'error': str(e)})


# ============================================================================
# 共用函數
# ============================================================================

def _update_request_status(table, request_id: str, status: str, approver: str, extra_attrs: dict = None) -> None:
    """更新 DynamoDB 請求狀態

    Args:
        table: DynamoDB table resource
        request_id: 請求 ID
        status: 新狀態 (approved/denied)
        approver: 審批者 user_id
        extra_attrs: 額外要更新的屬性 dict
    """
    now = int(time.time())
    update_expr = 'SET #s = :s, approved_at = :t, approver = :a, #ttl = :ttl'
    expr_names = {'#s': 'status', '#ttl': 'ttl'}
    expr_values = {
        ':s': status,
        ':t': now,
        ':a': approver,
        ':ttl': now + RESULT_TTL,
    }

    if extra_attrs:
        for key, value in extra_attrs.items():
            placeholder = f':{key}'
            # 處理保留字
            if key in ('status', 'result'):
                expr_names[f'#{key}'] = key
                update_expr += f', #{key} = {placeholder}'
            else:
                update_expr += f', {key} = {placeholder}'
            expr_values[placeholder] = value

    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _send_status_update(message_id: int, status_emoji: str, title: str, item: dict, extra_lines: str = '') -> None:
    """更新 Telegram 訊息

    Args:
        message_id: Telegram 訊息 ID
        status_emoji: 狀態 emoji (✅/❌)
        title: 標題文字
        item: 包含 request_id, source, context 等的 dict
        extra_lines: 額外要加在訊息中的行
    """
    request_id = item.get('request_id', '')
    info = build_info_lines(source=item.get('source', ''), context=item.get('context', ''))

    update_message(
        message_id,
        f"{status_emoji} *{title}*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{info}"
        f"{extra_lines}"
    )


# ============================================================================
# Command Callback
# ============================================================================

def _parse_command_callback_request(item: dict) -> dict:
    """解析 command callback 請求的資料

    Args:
        item: DynamoDB item

    Returns:
        dict: 包含 command, assume_role, source, trust_scope, reason, context, account_id, account_name
    """
    return {
        'command': item.get('command', ''),
        'assume_role': item.get('assume_role'),
        'source': item.get('source', ''),
        'trust_scope': item.get('trust_scope', ''),
        'reason': item.get('reason', ''),
        'context': item.get('context', ''),
        'account_id': item.get('account_id', DEFAULT_ACCOUNT_ID),
        'account_name': item.get('account_name', 'Default'),
    }


def _format_command_info(parsed: dict) -> dict:
    """格式化命令顯示資訊

    Args:
        parsed: _parse_command_callback_request 返回的 dict

    Returns:
        dict: 包含 source_line, account_line, safe_reason, cmd_preview
    """
    command = parsed['command']
    source = parsed['source']
    context = parsed['context']
    reason = parsed['reason']
    account_id = parsed['account_id']
    account_name = parsed['account_name']

    # build_info_lines escapes internally; pass raw values from DB
    source_line = build_info_lines(source=source, context=context)
    safe_account_name = escape_markdown(account_name) if account_name else ''
    account_line = f"🏦 *帳號：* `{account_id}` ({safe_account_name})\n"
    safe_reason = escape_markdown(reason)
    cmd_preview = command[:500] + '...' if len(command) > 500 else command

    return {
        'source_line': source_line,
        'account_line': account_line,
        'safe_reason': safe_reason,
        'cmd_preview': cmd_preview,
    }


def _execute_and_store_result(
    command: str,
    assume_role: str,
    request_id: str,
    item: dict,
    user_id: str,
    source_ip: str,
    action: str,
) -> dict:
    """執行命令並存入 DynamoDB

    Args:
        command: 要執行的命令
        assume_role: IAM role
        request_id: 請求 ID
        item: DynamoDB item (需要 created_at)
        user_id: 審批者 ID
        source_ip: 來源 IP (audit trail #74)
        action: approve 或 approve_trust

    Returns:
        dict: 包含 result, paged, decision_latency_ms
    """
    table = _get_table()

    # 執行命令
    result = execute_command(command, assume_role)
    cmd_status = 'failed' if _is_execute_failed(result) else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'manual_approve'})
    paged = store_paged_output(request_id, result)

    # 計算決策延遲
    now = int(time.time())
    created_at = int(item.get('created_at', 0))
    decision_latency_ms = (now - created_at) * 1000 if created_at else 0
    if decision_latency_ms:
        emit_metric('Bouncer', 'DecisionLatency', decision_latency_ms, unit='Milliseconds', dimensions={'Action': 'approve'})

    decision_type = 'manual_approved_trust' if action == 'approve_trust' else 'manual_approved'

    # 存入 DynamoDB（包含分頁資訊 + audit trail #74）
    update_expr = (
        'SET #s = :s, #r = :r, approved_at = :t, approver = :a, '
        'approved_by = :aby, duration_ms = :dms, source_ip = :sip, '
        'decision_type = :dt, decided_at = :da, decision_latency_ms = :dl, '
        'command_status = :cs, #ttl = :ttl'
    )
    expr_names = {'#s': 'status', '#r': 'result', '#ttl': 'ttl'}
    expr_values = {
        ':s': 'approved',
        ':r': paged['result'],
        ':t': now,
        ':a': user_id,
        ':aby': user_id,                         # audit trail: who approved (Telegram user_id)
        ':dms': decision_latency_ms,             # audit trail: approval duration in ms
        ':sip': source_ip,                       # audit trail: Telegram server IP (#74)
        ':dt': decision_type,
        ':da': now,
        ':dl': decision_latency_ms,
        ':cs': cmd_status,
        ':ttl': now + RESULT_TTL
    }

    if paged.get('paged'):
        update_expr += ', paged = :p, total_pages = :tp, output_length = :ol, next_page = :np'
        expr_values[':p'] = True
        expr_values[':tp'] = paged['total_pages']
        expr_values[':ol'] = paged['output_length']
        expr_values[':np'] = paged.get('next_page')

    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values
    )

    return {
        'result': result,
        'paged': paged,
        'decision_latency_ms': decision_latency_ms,
    }


def _handle_trust_session(
    trust_scope: str,
    account_id: str,
    user_id: str,
    source: str,
    assume_role: str,
    source_ip: str = '',
) -> str:
    """處理信任時段的建立與自動執行

    Args:
        trust_scope: 信任範圍
        account_id: 帳號 ID
        user_id: 使用者 ID
        source: 來源
        assume_role: IAM role
        source_ip: Telegram server IP (for IP binding, best-effort)

    Returns:
        str: 信任時段資訊字串 (trust_line)
    """
    trust_id = create_trust_session(
        trust_scope, account_id, user_id, source=source,
        max_uploads=TRUST_SESSION_MAX_UPLOADS,
        creator_ip=source_ip,
    )
    emit_metric('Bouncer', 'TrustSession', 1, dimensions={'Event': 'created'})
    trust_line = (
        f"\n\n🔓 信任時段已啟動：`{trust_id}`"
        f"\n📊 命令: 0/{TRUST_SESSION_MAX_COMMANDS} | 上傳: 0/{TRUST_SESSION_MAX_UPLOADS}"
    )

    # 查詢同 trust_scope 的 pending 請求（顯示 display_summary）
    # bouncer-trust-batch-flow (Approach B): show each pending request's
    # display_summary instead of just the count.
    pending_items = []
    try:
        pending_resp = _db.table.query(
            IndexName='status-created-index',
            KeyConditionExpression='#status = :status',
            FilterExpression='trust_scope = :scope AND account_id = :account',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'pending',
                ':scope': trust_scope,
                ':account': account_id,
            },
            ScanIndexForward=True,
            Limit=20,
        )
        pending_items = pending_resp.get('Items', [])
    except Exception:  # noqa: BLE001 — DDB query failure is non-fatal; logged with exc_info
        logger.warning("Failed to query pending items", extra={"src_module": "trust", "operation": "query_pending", "trust_scope": trust_scope}, exc_info=True)  # [TRUST] Failed to query pending items

    pending_count = len(pending_items)
    if pending_count > 0:
        trust_line += f"\n⚡ 自動執行 {pending_count} 個排隊請求："
        for pi in pending_items[:5]:
            summary = pi.get('display_summary') or pi.get('command', '')[:60]
            trust_line += f"\n  • {escape_markdown(str(summary))}"
        if pending_count > 5:
            trust_line += f"\n  _...及其他 {pending_count - 5} 個請求_"

    # 自動執行同 trust_scope + account 的排隊中請求
    try:
        _auto_execute_pending_requests(trust_scope, account_id, assume_role, trust_id, source)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
        logger.error(f"[TRUST] Auto-execute pending error: {e}", extra={"src_module": "trust", "operation": "auto_execute_pending", "error": str(e)})
    return trust_line


def _format_approval_response(
    action: str,
    result: str,
    paged: dict,
    trust_line: str,
    request_id: str,
    info: dict,
    message_id: int,
) -> None:
    """格式化並發送審批通過的回應訊息

    Args:
        action: approve 或 approve_trust
        result: 執行結果
        paged: 分頁資訊
        trust_line: 信任時段資訊
        request_id: 請求 ID
        info: _format_command_info 返回的 dict
        message_id: Telegram message ID
    """
    max_preview = 800 if action == 'approve_trust' else 1000
    result_preview = result[:max_preview] if len(result) > max_preview else result

    if paged.get('paged'):
        truncate_notice = (
            f"\n\n📄 *共 {paged['total_pages']} 頁* ({paged['output_length']} 字元)\n"
            f"用 `bouncer_get_page` 查看更多，或點下方按鈕"
        )
        next_page_button = {
            'inline_keyboard': [[{
                'text': f'➡️ Next Page (2/{paged["total_pages"]})',
                'callback_data': f'show_page:{request_id}:2',
            }]]
        }
    else:
        truncate_notice = ""
        next_page_button = None

    failed = _is_execute_failed(result)
    if failed:
        if action == 'approve_trust':
            title = "❌ *已批准但執行失敗* + 🔓 *信任 10 分鐘*"
        else:
            title = "❌ *已批准但執行失敗*"
    else:
        if action == 'approve_trust':
            title = "✅ *已批准並執行* + 🔓 *信任 10 分鐘*"
        else:
            title = "✅ *已批准並執行*"

    result_emoji = "❌" if failed else "✅"

    # Send result message; append inline Next Page button when paged
    send_telegram_message_silent(
        f"{title}\n\n"
        f"🆔 *ID：* `{request_id}`\n"
        f"{info['source_line']}"
        f"{info['account_line']}"
        f"📋 *命令：*\n`{info['cmd_preview']}`\n\n"
        f"💬 *原因：* {info['safe_reason']}\n\n"
        f"📤 *結果：*\n```\n{result_preview}\n```{truncate_notice}{trust_line}",
        reply_markup=next_page_button,
    )
    # Overwrite the approval message (remove buttons from it)
    update_message(message_id, f"{result_emoji} *已執行* — 見下方結果", remove_buttons=True)


def _handle_deny_callback(
    request_id: str,
    item: dict,
    callback_id: str,
    user_id: str,
    message_id: int,
    info: dict,
) -> None:
    """處理拒絕操作

    Args:
        request_id: 請求 ID
        item: DynamoDB item (需要 created_at)
        callback_id: Telegram callback ID
        user_id: 拒絕者 ID
        message_id: Telegram message ID
        info: _format_command_info 返回的 dict
    """
    table = _get_table()

    now = int(time.time())
    created_at = int(item.get('created_at', 0))
    decision_latency_ms = (now - created_at) * 1000 if created_at else 0
    if decision_latency_ms:
        emit_metric('Bouncer', 'DecisionLatency', decision_latency_ms, unit='Milliseconds', dimensions={'Action': 'deny'})

    answer_callback(callback_id, '❌ 已拒絕')
    _update_request_status(table, request_id, 'denied', user_id, extra_attrs={
        'decision_type': 'manual_denied',
        'decided_at': now,
        'decision_latency_ms': decision_latency_ms,
    })

    update_message(
        message_id,
        f"❌ *已拒絕*\n\n"
        f"🆔 *ID：* `{request_id}`\n"
        f"{info['source_line']}"
        f"{info['account_line']}"
        f"📋 *命令：*\n`{info['cmd_preview']}`\n\n"
        f"💬 *原因：* {info['safe_reason']}",
    )


def handle_command_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str, *, source_ip: str = '') -> dict:
    """處理命令執行的審批 callback

    Args:
        source_ip: Telegram server IP from API GW event (for audit trail #74).
                   This is NOT the end-user IP — Lambda runs as a webhook.
    """
    # 解析請求資料
    parsed = _parse_command_callback_request(item)
    command = parsed['command']
    assume_role = parsed['assume_role']
    source = parsed['source']
    trust_scope = parsed['trust_scope']
    account_id = parsed['account_id']

    # 格式化顯示資訊
    info = _format_command_info(parsed)

    if action in ('approve', 'approve_trust'):
        if is_dangerous(command):
            answer_callback(callback_id, '⚠️ 高危操作確認：正在執行...', show_alert=True)
        elif action == 'approve_trust':
            answer_callback(callback_id, '✅ 執行中 + 🔓 信任啟動')
        else:
            answer_callback(callback_id, '✅ 執行中...')

        # Immediate feedback: remove buttons before execute_command (best-effort)
        try:
            update_message(
                message_id,
                f"⏳ *執行中...*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{info['source_line']}"
                f"{info['account_line']}"
                f"📋 *命令：*\n`{info['cmd_preview']}`\n\n"
                f"💬 *原因：* {info['safe_reason']}",
                remove_buttons=True,
            )
        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
            logger.warning(f"[execute] Immediate feedback update_message failed (non-critical): {e}")

        # 執行命令並存入結果
        try:
            send_chat_action('typing')
        except Exception:
            pass
        exec_result = _execute_and_store_result(
            command, assume_role, request_id, item, user_id, source_ip, action
        )
        result = exec_result['result']
        paged = exec_result['paged']

        # 信任模式
        trust_line = ""
        if action == 'approve_trust':
            trust_line = _handle_trust_session(trust_scope, account_id, user_id, source, assume_role, source_ip=source_ip)

        # 格式化並發送回應訊息
        _format_approval_response(action, result, paged, trust_line, request_id, info, message_id)

    elif action == 'deny':
        _handle_deny_callback(request_id, item, callback_id, user_id, message_id, info)

    return response(200, {'ok': True})


# ============================================================================
# Account Add Callback
# ============================================================================

def handle_account_add_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理新增帳號的審批 callback"""
    table = _get_table()
    accounts_table = _get_accounts_table()

    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    role_arn = item.get('role_arn', '')
    source = item.get('source', '')
    context = item.get('context', '')

    detail_lines = (
        f"🆔 *帳號 ID：* `{account_id}`\n"
        f"📛 *名稱：* {account_name}"
    )

    if action == 'approve':
        # 寫入帳號配置
        answer_callback(callback_id, '✅ 處理中...')
        try:
            accounts_table.put_item(Item={
                'account_id': account_id,
                'name': account_name,
                'role_arn': role_arn if role_arn else None,
                'is_default': False,
                'enabled': True,
                'created_at': int(time.time()),
                'created_by': user_id
            })

            _update_request_status(table, request_id, 'approved', user_id)

            _send_status_update(
                message_id, '✅', '已新增帳號',
                {'request_id': request_id, 'source': source, 'context': context},
                extra_lines=f"{detail_lines}\n🔗 *Role：* `{role_arn}`"
            )

        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
            answer_callback(callback_id, f'❌ 新增失敗: {str(e)[:50]}')
            return response(500, {'error': str(e)})

    elif action == 'deny':
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        _send_status_update(
            message_id, '❌', '已拒絕新增帳號',
            {'request_id': request_id, 'source': source, 'context': context},
            extra_lines=detail_lines
        )

    return response(200, {'ok': True})


# ============================================================================
# Account Remove Callback
# ============================================================================

def handle_account_remove_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理移除帳號的審批 callback"""
    table = _get_table()
    accounts_table = _get_accounts_table()

    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    source = item.get('source', '')
    context = item.get('context', '')

    detail_lines = (
        f"🆔 *帳號 ID：* `{account_id}`\n"
        f"📛 *名稱：* {account_name}"
    )

    if action == 'approve':
        answer_callback(callback_id, '✅ 處理中...')
        try:
            accounts_table.delete_item(Key={'account_id': account_id})

            _update_request_status(table, request_id, 'approved', user_id)

            _send_status_update(
                message_id, '✅', '已移除帳號',
                {'request_id': request_id, 'source': source, 'context': context},
                extra_lines=detail_lines
            )

        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
            answer_callback(callback_id, f'❌ 移除失敗: {str(e)[:50]}')
            return response(500, {'error': str(e)})

    elif action == 'deny':
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        _send_status_update(
            message_id, '❌', '已拒絕移除帳號',
            {'request_id': request_id, 'source': source, 'context': context},
            extra_lines=detail_lines
        )

    return response(200, {'ok': True})


# ============================================================================
# Deploy Callback
# ============================================================================

def handle_deploy_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理部署的審批 callback"""
    from deployer import start_deploy
    table = _get_table()

    project_id = item.get('project_id', '')
    project_name = item.get('project_name', project_id)
    branch = item.get('branch', 'master')
    stack_name = item.get('stack_name', '')
    source = item.get('source', '')
    reason = item.get('reason', '')
    context = item.get('context', '')

    source_line = build_info_lines(source=source, context=context)

    if action == 'approve':
        answer_callback(callback_id, '🚀 啟動部署中...')
        _update_request_status(table, request_id, 'approved', user_id)

        # Immediate feedback: remove buttons before start_deploy (best-effort)
        try:
            update_message(
                message_id,
                f"⏳ *部署排隊中...*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{source_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}",
                remove_buttons=True,
            )
        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
            logger.warning(f"[deploy] Immediate feedback update_message failed (non-critical): {e}")

        # 啟動部署
        result = start_deploy(project_id, branch, user_id, reason)

        if 'error' in result or result.get('status') == 'conflict':
            emit_metric('Bouncer', 'Deploy', 1, dimensions={'Status': 'failed', 'Project': project_id})
            error_msg = result.get('error') or result.get('message', '啟動失敗')
            update_message(
                message_id,
                f"❌ *部署啟動失敗*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{source_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}\n\n"
                f"❗ *錯誤：* {escape_markdown(error_msg)}"
            )
        else:
            emit_metric('Bouncer', 'Deploy', 1, dimensions={'Status': 'started', 'Project': project_id})
            deploy_id = result.get('deploy_id', '')
            reason_line = f"📝 *原因：* {escape_markdown(reason)}\n" if reason else ""
            # 加入 git commit SHA（若有）
            commit_short = result.get('commit_short')
            commit_message = result.get('commit_message', '')
            commit_line = ""
            if commit_short:
                commit_display = f"`{commit_short}`"
                if commit_message:
                    commit_display += f" {escape_markdown(commit_message)}"
                commit_line = f"🔖 {commit_display}\n"
            update_message(
                message_id,
                f"🚀 *部署已啟動*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{source_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}\n"
                f"{reason_line}"
                f"📋 *Stack：* {stack_name}\n"
                f"{commit_line}"
                f"\n🆔 *部署 ID：* `{deploy_id}`\n\n"
                f"⏳ 部署進行中..."
            )

            # Pin the approval message so progress is visible (best-effort)
            try:
                pin_message(message_id)
            except Exception as pin_err:
                logger.warning(f"[deploy] Failed to pin message (ignored): {pin_err}")

            # Store telegram_message_id in deploy record for unpinning later
            if deploy_id:
                try:
                    from deployer import update_deploy_record
                    update_deploy_record(deploy_id, {'telegram_message_id': message_id})
                except ClientError as e:
                    logger.warning("Failed to store telegram_message_id (ignored): %s", e, extra={"src_module": "callbacks", "operation": "handle_deploy_callback", "error": str(e)})

    elif action == 'deny':
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ *已拒絕部署*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n"
            f"📋 *Stack：* {stack_name}\n\n"
            f"💬 *原因：* {escape_markdown(reason)}"
        )

    return response(200, {'ok': True})


# ============================================================================
# Upload Callback
# ============================================================================

def handle_upload_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理上傳的審批 callback"""
    table = _get_table()

    bucket = item.get('bucket', '')
    key = item.get('key', '')
    content_size = int(item.get('content_size', 0))
    source = item.get('source', '')
    reason = item.get('reason', '')
    context = item.get('context', '')
    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')

    s3_uri = f"s3://{bucket}/{key}"
    info_lines = build_info_lines(
        source=source, context=context,
        account_name=account_name, account_id=account_id,
    )

    size_str = format_size_human(content_size)
    safe_reason = escape_markdown(reason)

    if action == 'approve':
        # 執行上傳
        answer_callback(callback_id, '📤 上傳中...')
        result = execute_upload(request_id, user_id)

        if result.get('success'):
            emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'approved', 'Type': 'single'})
            update_message(
                message_id,
                f"✅ *已上傳*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{info_lines}"
                f"📁 *目標：* `{s3_uri}`\n"
                f"📊 *大小：* {size_str}\n"
                f"🔗 *URL：* {result.get('s3_url', '')}\n"
                f"💬 *原因：* {safe_reason}"
            )
        else:
            # 上傳失敗
            error = result.get('error', 'Unknown error')
            update_message(
                message_id,
                f"❌ *上傳失敗*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{info_lines}"
                f"📁 *目標：* `{s3_uri}`\n"
                f"📊 *大小：* {size_str}\n"
                f"❗ *錯誤：* {error}\n"
                f"💬 *原因：* {safe_reason}"
            )

    elif action == 'deny':
        emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'denied', 'Type': 'single'})
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ *已拒絕上傳*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{info_lines}"
            f"📁 *目標：* `{s3_uri}`\n"
            f"📊 *大小：* {size_str}\n"
            f"💬 *原因：* {safe_reason}"
        )

    return response(200, {'ok': True})


# ============================================================================
# Upload Batch Callback
# ============================================================================

def _parse_callback_files_manifest(item: dict, callback_id: str) -> 'list | dict':
    """Parse and validate files manifest from callback item.

    Returns:
        list: Parsed files manifest on success
        dict: Error response on failure
    """
    import json as _json
    try:
        files_manifest = _json.loads(item.get('files', '[]'))
        return files_manifest
    except _json.JSONDecodeError as e:
        logger.error("Failed to parse files manifest: %s", e, extra={"src_module": "callbacks", "operation": "parse_files_manifest", "error": str(e)}, exc_info=True)
        answer_callback(callback_id, '❌ 檔案清單解析失敗')
        return response(500, {'error': 'Failed to parse files manifest'})


def _setup_callback_s3_clients(assume_role, table, request_id: str, user_id: str, message_id: int) -> 'tuple | dict':
    """Setup dual S3 clients for batch upload callback.

    Returns:
        tuple: (s3_staging, s3_target) on success
        dict: Error response on failure
    """
    try:
        s3_staging = get_s3_client(role_arn=None, session_name='bouncer-batch-upload-staging')
        s3_target = get_s3_client(role_arn=assume_role, session_name='bouncer-batch-upload')
        return (s3_staging, s3_target)
    except ClientError as e:
        _update_request_status(table, request_id, 'error', user_id, extra_attrs={'error_message': str(e)})
        update_message(
            message_id,
            f"❌ *批量上傳失敗*（S3 連線錯誤）\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"❗ *錯誤：* {str(e)[:200]}",
        )
        return response(500, {'error': str(e)})


def _execute_callback_upload_batch(
    files_manifest: list,
    s3_staging,
    s3_target,
    bucket: str,
    message_id: int,
    request_id: str,
    file_count: int
) -> tuple:
    """Execute the batch upload loop with progress updates.

    Returns:
        tuple: (uploaded, errors, verification_failed)
    """
    import time as _time
    date_str = _time.strftime('%Y-%m-%d')
    uploaded = []
    errors = []
    verification_failed = []

    for i, fm in enumerate(files_manifest):
        fname = fm.get('filename', 'unknown')
        try:
            s3_key = fm.get('s3_key')  # new format
            content_b64_legacy = fm.get('content_b64')  # old format fallback
            from utils import generate_request_id as _gen_id
            fkey = f"{date_str}/{_gen_id('batch')}/{fname}"
            if s3_key:
                # New path: read from staging (Lambda role), write to target (assumed role).
                # Previously used copy_object with the assumed-role client which fails
                # silently when the assumed role has no read access to staging bucket (#39).
                from constants import DEFAULT_ACCOUNT_ID as _DEFAULT_ACCOUNT_ID
                staging_bucket = f"bouncer-uploads-{_DEFAULT_ACCOUNT_ID}"
                obj = s3_staging.get_object(Bucket=staging_bucket, Key=s3_key)
                body = obj['Body'].read()
                s3_target.put_object(
                    Bucket=bucket,
                    Key=fkey,
                    Body=body,
                    ContentType=fm.get('content_type', 'application/octet-stream'),
                )
                # Cleanup staging object (best effort, non-blocking)
                try:
                    s3_staging.delete_object(Bucket=staging_bucket, Key=s3_key)
                except Exception:  # noqa: BLE001 — S3 staging cleanup is best-effort
                    logger.warning("Staging cleanup failed for key=%s (non-critical)", s3_key, extra={"src_module": "callbacks", "operation": "upload_batch_cleanup", "s3_key": s3_key}, exc_info=True)  # [UPLOAD-BATCH] Staging cleanup failed
            else:
                # Legacy path: decode base64 and upload directly to target
                import base64 as _b64
                content_bytes = _b64.b64decode(content_b64_legacy or '')
                s3_target.put_object(
                    Bucket=bucket,
                    Key=fkey,
                    Body=content_bytes,
                    ContentType=fm.get('content_type', 'application/octet-stream'),
                )

            # Verify file exists after upload (non-blocking)
            vr = _verify_upload(s3_target, bucket, fkey, fname)
            if not vr.verified:
                verification_failed.append(fname)
                # Non-blocking: record in verification_failed but still count as uploaded

            uploaded.append({
                'filename': fname,
                's3_uri': vr.s3_uri,
                'size': fm.get('size', 0),
                'verified': vr.verified,
                's3_size': vr.s3_size,
            })
        except (ClientError, ValueError, OSError) as e:
            errors.append({'filename': fname, 'reason': str(e)[:120]})

        # Update progress every 5 files
        if (i + 1) % 5 == 0 or i == len(files_manifest) - 1:
            try:
                update_message(
                    message_id,
                    f"⏳ *批量上傳中...*\n\n"
                    f"📋 *請求 ID：* `{request_id}`\n"
                    f"進度: {i + 1}/{file_count}",
                )
            except Exception:  # noqa: BLE001 — progress update is best-effort
                logger.warning("Progress update failed at step %d (non-critical)", i + 1, extra={"src_module": "callbacks", "operation": "upload_batch_progress", "step": i + 1}, exc_info=True)  # [UPLOAD-BATCH] Progress update failed at step

    return (uploaded, errors, verification_failed)


def _finalize_callback_upload(
    table,
    request_id: str,
    user_id: str,
    files_manifest: list,
    uploaded: list,
    errors: list,
    verification_failed: list
) -> str:
    """Determine final upload status and update database.

    Returns:
        str: upload_status ('completed', 'failed', or 'partial')
    """
    import json as _json

    total_files = len(files_manifest)
    success_count = len(uploaded)
    fail_count = len(errors)

    if fail_count == 0:
        upload_status = 'completed'
    elif success_count == 0:
        upload_status = 'failed'
    else:
        upload_status = 'partial'

    _update_request_status(table, request_id, 'approved', user_id, extra_attrs={
        'uploaded_count': success_count,
        'error_count': fail_count,
        'upload_status': upload_status,
        'uploaded_files': _json.dumps([u['filename'] for u in uploaded]),
        'failed_files': _json.dumps([f['filename'] for f in errors]),
        'uploaded_details': _json.dumps(uploaded),
        'failed_details': _json.dumps(errors),
        'total_files': total_files,
        'verification_failed': _json.dumps(verification_failed),
    })
    emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'approved', 'Type': 'batch'})

    return upload_status


def _create_callback_trust_session(
    action: str,
    trust_scope: str,
    account_id: str,
    user_id: str,
    source: str,
    source_ip: str = '',
) -> str:
    """Create trust session if action is approve_trust.

    Returns:
        str: Trust line for message (empty if no trust session created)
    """
    trust_line = ""
    if action == 'approve_trust' and trust_scope:
        trust_id = create_trust_session(
            trust_scope, account_id, user_id, source=source,
            max_uploads=TRUST_SESSION_MAX_UPLOADS,
            creator_ip=source_ip,
        )
        emit_metric('Bouncer', 'TrustSession', 1, dimensions={'Event': 'created'})
        trust_line = (
            f"\n\n🔓 信任時段已啟動：`{trust_id}`"
            f"\n📊 命令: 0/{TRUST_SESSION_MAX_COMMANDS} | 上傳: 0/{TRUST_SESSION_MAX_UPLOADS}"
        )
    return trust_line


def handle_upload_batch_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理批量上傳的審批 callback"""
    table = _get_table()

    bucket = item.get('bucket', '')
    file_count = int(item.get('file_count', 0))
    total_size = int(item.get('total_size', 0))
    source = item.get('source', '')
    reason = item.get('reason', '')
    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    trust_scope = item.get('trust_scope', '')
    assume_role = item.get('assume_role', None)

    size_str = format_size_human(total_size)
    safe_reason = escape_markdown(reason)

    source_line = build_info_lines(
        source=source, account_name=account_name, account_id=account_id,
    )

    if action in ('approve', 'approve_trust'):
        # Parse files manifest
        files_manifest = _parse_callback_files_manifest(item, callback_id)
        if isinstance(files_manifest, dict) and 'statusCode' in files_manifest:
            return files_manifest

        # Update message to show progress
        update_message(
            message_id,
            f"⏳ *批量上傳中...*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📄 {file_count} 個檔案 ({size_str})\n"
            f"💬 *原因：* {safe_reason}\n\n"
            f"進度: 0/{file_count}",
            remove_buttons=True,
        )
        answer_callback(callback_id, '⏳ 上傳中...')

        # Get S3 clients.
        # s3_staging: Lambda execution role — reads from staging bucket (main account).
        # s3_target:  Assumed role (if set) — writes to target bucket (may be cross-account).
        # Using two separate clients avoids cross-account copy_object failures where the
        # assumed role lacks s3:GetObject on the staging bucket (#39).
        s3_clients = _setup_callback_s3_clients(assume_role, table, request_id, user_id, message_id)
        if isinstance(s3_clients, dict) and 'statusCode' in s3_clients:
            return s3_clients
        s3_staging, s3_target = s3_clients

        # Execute batch upload
        uploaded, errors, verification_failed = _execute_callback_upload_batch(
            files_manifest, s3_staging, s3_target, bucket, message_id, request_id, file_count
        )

        # Finalize upload status and update DB
        _finalize_callback_upload(
            table, request_id, user_id, files_manifest, uploaded, errors, verification_failed
        )

        # Create trust session if approve_trust
        trust_line = _create_callback_trust_session(
            action, trust_scope, account_id, user_id, source
        )

        error_line = f"\n❗ 失敗: {len(errors)} 個" if errors else ""

        update_message(
            message_id,
            f"✅ *批量上傳完成*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📄 成功: {len(uploaded)}/{file_count} 個檔案 ({size_str})"
            f"{error_line}"
            f"\n💬 *原因：* {safe_reason}"
            f"{trust_line}",
        )

    elif action == 'deny':
        emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'denied', 'Type': 'batch'})
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ *已拒絕批量上傳*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📄 {file_count} 個檔案 ({size_str})\n"
            f"💬 *原因：* {safe_reason}",
        )

    return response(200, {'ok': True})


# ============================================================================
# Deploy Frontend Callback (sprint9-003 Phase B)
# ============================================================================

def _write_frontend_deploy_history(
    request_id: str,
    project: str,
    deploy_status: str,
    user_id: str,
    file_count: int,
    success_count: int,
    fail_count: int,
    reason: str,
    source: str,
    frontend_bucket: str,
    distribution_id: str,
    cf_invalidation_failed: bool,
) -> None:
    """Write frontend deploy outcome to the deploy_history DynamoDB table.

    Uses the same table as SAM deploys (bouncer-deploy-history) so that
    bouncer_deploy_history can surface frontend deploys alongside SAM deploys.
    The project_id GSI (project-time-index) is keyed on project_id, so we
    map the frontend project name to project_id for consistent querying.
    """
    try:
        from deployer import _get_history_table
        now = int(time.time())
        # Map deploy_status -> uppercase STATUS used by SAM deploys
        status_map = {
            'deployed': 'SUCCEEDED',
            'partial_deploy': 'PARTIAL',
            'deploy_failed': 'FAILED',
        }
        history_status = status_map.get(deploy_status, deploy_status.upper())

        history_item = {
            'deploy_id': f'frontend-{request_id}',
            'project_id': project,
            'deploy_type': 'frontend',
            'status': history_status,
            'started_at': now,
            'completed_at': now,
            'triggered_by': user_id,
            'reason': reason or '',
            'source': source or '',
            'files_count': file_count,
            'files_deployed': success_count,
            'files_failed': fail_count,
            'frontend_bucket': frontend_bucket,
            'distribution_id': distribution_id,
            'cf_invalidation_failed': cf_invalidation_failed,
            'request_id': request_id,
            'ttl': now + 30 * 24 * 3600,  # 30 days
        }
        # DynamoDB does not allow None values
        history_item = {k: v for k, v in history_item.items() if v is not None}
        _get_history_table().put_item(Item=history_item)
        logger.info("deploy_history written deploy_id=frontend-%s project=%s status=%s", request_id, project, history_status, extra={"src_module": "callbacks", "operation": "write_deploy_history", "request_id": request_id, "project": project, "status": history_status})
    except ClientError as exc:
        logger.error("Failed to write deploy_history for %s: %s", request_id, exc, extra={"src_module": "callbacks", "operation": "write_deploy_history", "request_id": request_id, "error": str(exc)})


def _parse_deploy_frontend_params(item: dict) -> dict:
    """Parse and prepare parameters from deploy frontend request item.

    Returns dict with all necessary fields for deploy frontend processing.
    """
    project = item.get('project', '')
    staging_bucket = item.get('staging_bucket', '')
    frontend_bucket = item.get('frontend_bucket', '')
    distribution_id = item.get('distribution_id', '')
    source = item.get('source', '')
    reason = item.get('reason', '')
    files_json = item.get('files', '[]')
    file_count = int(item.get('file_count', 0))
    total_size = int(item.get('total_size', 0))
    deploy_role_arn = item.get('deploy_role_arn')

    safe_reason = escape_markdown(reason)
    size_str = format_size_human(total_size)
    source_line = build_info_lines(source=source)

    return {
        'project': project,
        'staging_bucket': staging_bucket,
        'frontend_bucket': frontend_bucket,
        'distribution_id': distribution_id,
        'source': source,
        'reason': reason,
        'files_json': files_json,
        'file_count': file_count,
        'total_size': total_size,
        'deploy_role_arn': deploy_role_arn,
        'safe_reason': safe_reason,
        'size_str': size_str,
        'source_line': source_line,
    }


def _handle_deploy_frontend_deny(table, request_id: str, callback_id: str, message_id: int, user_id: str, params: dict) -> dict:
    """Handle deny action for frontend deploy request.

    Updates status to rejected and sends notification message.
    Returns response dict.
    """
    answer_callback(callback_id, '❌ 已拒絕')
    _update_request_status(table, request_id, 'rejected', user_id)
    update_message(
        message_id,
        f"❌ *已拒絕前端部署*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{params['source_line']}"
        f"📦 *專案：* {escape_markdown(params['project'])}\n"
        f"📄 {params['file_count']} 個檔案 ({params['size_str']})\n"
        f"💬 *原因：* {params['safe_reason']}",
    )
    return response(200, {'ok': True})


def _assume_deploy_role(deploy_role_arn: str, request_id: str, files_manifest: list, table, message_id: int, user_id: str, params: dict, item: dict):
    """Assume deploy role for S3 operations.

    Returns:
        tuple: (s3_client, error_response_or_none)
        - If successful: (s3_client, None)
        - If failed: (None, response_dict)
    """
    import json as _json

    if not deploy_role_arn:
        return get_s3_client(), None

    try:
        s3_target = get_s3_client(role_arn=deploy_role_arn, session_name=f"bouncer-deploy-{request_id[:16]}")
        return s3_target, None
    except ClientError as e:
        logger.error("AssumeRole failed for %s: %s", deploy_role_arn, e, extra={"src_module": "callbacks", "operation": "assume_role", "deploy_role_arn": deploy_role_arn, "error": str(e)})
        failed = [
            {'filename': fm.get('filename', 'unknown'), 'reason': f'AssumeRole failed: {e}'}
            for fm in files_manifest
        ]
        deploy_status = 'deploy_failed'
        extra_attrs = {
            'deploy_status': deploy_status,
            'deployed_count': 0,
            'failed_count': len(failed),
            'deployed_files': _json.dumps([]),
            'failed_files': _json.dumps([f['filename'] for f in failed]),
            'deployed_details': _json.dumps([]),
            'failed_details': _json.dumps(failed),
            'cf_invalidation_failed': False,
        }
        _update_request_status(table, request_id, 'approved', user_id, extra_attrs=extra_attrs)
        emit_metric('Bouncer', 'DeployFrontend', 1, dimensions={'Status': deploy_status, 'Project': params['project']})
        _write_frontend_deploy_history(
            request_id=request_id,
            project=params['project'],
            deploy_status=deploy_status,
            user_id=user_id,
            file_count=params['file_count'],
            success_count=0,
            fail_count=len(failed),
            reason=item.get('reason', ''),
            source=params['source'],
            frontend_bucket=params['frontend_bucket'],
            distribution_id=params['distribution_id'],
            cf_invalidation_failed=False,
        )
        update_message(
            message_id,
            f"❌ *前端部署失敗*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{params['source_line']}"
            f"📦 *專案：* {escape_markdown(params['project'])}\n"
            f"❗ AssumeRole 失敗，全部 {params['file_count']} 個檔案無法部署\n"
            f"💬 *原因：* {params['safe_reason']}",
        )
        return None, response(200, {
            'ok': True,
            'deploy_status': deploy_status,
            'deployed_count': 0,
            'failed_count': len(failed),
            'cf_invalidation_failed': False,
        })


def _deploy_files_to_frontend(files_manifest: list, s3_staging, s3_target, request_id: str, message_id: int, params: dict, user_id: str) -> tuple:
    """Deploy files from staging bucket to frontend bucket.

    Returns:
        tuple: (deployed_list, failed_list)
    """
    deployed = []
    failed = []
    staging_bucket = params['staging_bucket']
    frontend_bucket = params['frontend_bucket']
    file_count = params['file_count']
    project = params['project']

    for i, fm in enumerate(files_manifest):
        filename = fm.get('filename', 'unknown')
        staged_key = fm.get('s3_key', '')
        content_type = fm.get('content_type', 'application/octet-stream')
        cache_control = fm.get('cache_control', 'no-cache')

        try:
            # Read from staging (Lambda role)
            obj = s3_staging.get_object(Bucket=staging_bucket, Key=staged_key)
            body = obj['Body'].read()

            # Write to frontend (assumed role or Lambda role)
            s3_target.put_object(
                Bucket=frontend_bucket,
                Key=filename,
                Body=body,
                ContentType=content_type,
                CacheControl=cache_control,
            )
            deployed.append({'filename': filename, 's3_key': filename})
            logger.info("uploaded file=%s size=%d content_type=%s request_id=%s project=%s", filename, len(body), content_type, request_id, project, extra={"src_module": "callbacks", "operation": "deploy_frontend_upload", "filename": filename, "request_id": request_id, "project": project})
        except ClientError as e:
            logger.error("upload_failed file=%s error=%s request_id=%s project=%s", filename, str(e)[:200], request_id, project, extra={"src_module": "callbacks", "operation": "deploy_frontend_upload", "filename": filename, "request_id": request_id, "project": project, "error": str(e)[:200]})
            failed.append({'filename': filename, 'reason': str(e)[:200]})

        # Progress update every 5 files
        if (i + 1) % 5 == 0 or i == len(files_manifest) - 1:
            try:
                update_message(
                    message_id,
                    f"⏳ *前端部署中...*\n\n"
                    f"📋 *請求 ID：* `{request_id}`\n"
                    f"進度: {i + 1}/{file_count}",
                )
            except Exception:  # noqa: BLE001 — progress update is best-effort
                logger.warning("Progress update failed at step %d (non-critical)", i + 1, extra={"src_module": "callbacks", "operation": "deploy_frontend_progress", "step": i + 1}, exc_info=True)  # [DEPLOY-FRONTEND] Progress update failed at step

    return deployed, failed


def _invalidate_cloudfront(success_count: int, deploy_role_arn: str, distribution_id: str, request_id: str) -> bool:
    """Invalidate CloudFront distribution if files were successfully deployed.

    Returns:
        bool: True if invalidation failed, False if succeeded or skipped (no files deployed)
    """
    if success_count == 0:
        return False

    try:
        from aws_clients import get_cloudfront_client
        cf = get_cloudfront_client(role_arn=deploy_role_arn)
        cf.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                'Paths': {'Quantity': 1, 'Items': ['/*']},
                'CallerReference': request_id,
            },
        )
        return False
    except ClientError as e:
        logger.error("CloudFront invalidation failed for dist=%s: %s", distribution_id, e, extra={"src_module": "callbacks", "operation": "cloudfront_invalidation", "distribution_id": distribution_id, "error": str(e)})
        return True


def _finalize_deploy_frontend(deployed: list, failed: list, cf_invalidation_failed: bool, table, request_id: str,
                               user_id: str, message_id: int, params: dict, item: dict) -> dict:
    """Finalize deploy frontend: update DDB, emit metrics, write history, send notifications.

    Returns:
        dict: API Gateway response
    """
    import json as _json

    success_count = len(deployed)
    fail_count = len(failed)

    if success_count == 0:
        deploy_status = 'deploy_failed'
    elif fail_count == 0:
        deploy_status = 'deployed'
    else:
        deploy_status = 'partial_deploy'

    # Update DDB
    extra_attrs = {
        'deploy_status': deploy_status,
        'deployed_count': success_count,
        'failed_count': fail_count,
        'deployed_files': _json.dumps([d['filename'] for d in deployed]),
        'failed_files': _json.dumps([f['filename'] for f in failed]),
        'deployed_details': _json.dumps(deployed),
        'failed_details': _json.dumps(failed),
        'cf_invalidation_failed': cf_invalidation_failed,
    }
    _update_request_status(table, request_id, 'approved', user_id, extra_attrs=extra_attrs)

    emit_metric('Bouncer', 'DeployFrontend', 1, dimensions={'Status': deploy_status, 'Project': params['project']})

    # Write to deploy_history table (mirrors SAM deploy format)
    _write_frontend_deploy_history(
        request_id=request_id,
        project=params['project'],
        deploy_status=deploy_status,
        user_id=user_id,
        file_count=params['file_count'],
        success_count=success_count,
        fail_count=fail_count,
        reason=item.get('reason', ''),
        source=params['source'],
        frontend_bucket=params['frontend_bucket'],
        distribution_id=params['distribution_id'],
        cf_invalidation_failed=cf_invalidation_failed,
    )

    # Build result message
    cf_warn = "\n⚠️ *CloudFront Invalidation 失敗* (S3 已完成)" if cf_invalidation_failed else ""
    fail_line = f"\n❗ 失敗: {fail_count} 個" if fail_count > 0 else ""

    if deploy_status == 'deploy_failed':
        status_emoji = '❌'
        title = '前端部署失敗'
    elif deploy_status == 'partial_deploy':
        status_emoji = '⚠️'
        title = '前端部署部分成功'
    else:
        status_emoji = '✅'
        title = '前端部署完成'

    update_message(
        message_id,
        f"{status_emoji} *{title}*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{params['source_line']}"
        f"📦 *專案：* {escape_markdown(params['project'])}\n"
        f"📄 成功: {success_count}/{params['file_count']} 個檔案 ({params['size_str']})"
        f"{fail_line}\n"
        f"🌐 *目標 Bucket：* `{escape_markdown(params['frontend_bucket'])}`\n"
        f"☁️ *CloudFront：* `{escape_markdown(params['distribution_id'])}`\n"
        f"💬 *原因：* {params['safe_reason']}"
        f"{cf_warn}",
    )

    # Send Telegram result notification (silent)
    try:
        from notifications import _send_message_silent
        if deploy_status == 'deployed':
            notif_text = (
                f"✅ 前端部署成功\n"
                f"📦 {params['project']} — {success_count} 個檔案\n"
                f"🆔 `{request_id}`"
            )
        elif deploy_status == 'partial_deploy':
            notif_text = (
                f"⚠️ 前端部署部分成功\n"
                f"📦 {params['project']} — {success_count}/{params['file_count']} 成功，{fail_count} 失敗\n"
                f"🆔 `{request_id}`"
            )
        else:
            notif_text = (
                f"❌ 前端部署失敗\n"
                f"📦 {params['project']} — 全部 {params['file_count']} 個檔案失敗\n"
                f"🆔 `{request_id}`"
            )
        if cf_invalidation_failed:
            notif_text += "\n⚠️ CloudFront Invalidation 失敗"
        _send_message_silent(notif_text)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as notif_exc:
        logger.warning("Result notification failed: %s", notif_exc, extra={"src_module": "callbacks", "operation": "deploy_frontend_notify", "request_id": request_id, "error": str(notif_exc)})

    return response(200, {
        'ok': True,
        'deploy_status': deploy_status,
        'deployed_count': success_count,
        'failed_count': fail_count,
        'cf_invalidation_failed': cf_invalidation_failed,
    })


def handle_deploy_frontend_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理前端部署的審批 callback

    action=approve: 從 DDB 讀 staged_files + target_info → S3 copy → CloudFront invalidation
    action=deny:    更新 DDB status=rejected，不執行任何部署
    """
    import json as _json

    table = _get_table()
    params = _parse_deploy_frontend_params(item)

    if action == 'deny':
        return _handle_deploy_frontend_deny(table, request_id, callback_id, message_id, user_id, params)

    # action == 'approve'
    try:
        files_manifest = _json.loads(params['files_json'])
    except _json.JSONDecodeError as e:
        logger.error("Failed to parse files manifest for deploy-frontend: %s", e, extra={"src_module": "callbacks", "operation": "handle_deploy_frontend_callback", "error": str(e)}, exc_info=True)
        answer_callback(callback_id, '❌ 檔案清單解析失敗')
        return response(500, {'error': 'Failed to parse files manifest'})

    answer_callback(callback_id, '🚀 部署中...')
    update_message(
        message_id,
        f"⏳ *前端部署中...*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{params['source_line']}"
        f"📦 *專案：* {escape_markdown(params['project'])}\n"
        f"📄 {params['file_count']} 個檔案 ({params['size_str']})\n"
        f"💬 *原因：* {params['safe_reason']}\n\n"
        f"進度: 0/{params['file_count']}",
        remove_buttons=True,
    )

    # 1. Assume deploy role
    s3_target, error_response = _assume_deploy_role(
        params['deploy_role_arn'], request_id, files_manifest, table, message_id, user_id, params, item
    )
    if error_response:
        return error_response

    # 2. Deploy files to frontend bucket
    s3_staging = get_s3_client()
    deployed, failed = _deploy_files_to_frontend(
        files_manifest, s3_staging, s3_target, request_id, message_id, params, user_id
    )

    # 3. CloudFront invalidation
    cf_invalidation_failed = _invalidate_cloudfront(
        len(deployed), params['deploy_role_arn'], params['distribution_id'], request_id
    )

    # 4. Finalize: update DDB, metrics, history, and send notifications
    return _finalize_deploy_frontend(
        deployed, failed, cf_invalidation_failed, table, request_id, user_id, message_id, params, item
    )



# ============================================================================
# Show Page Callback (sprint13-003 on-demand pagination)
# ============================================================================

def handle_show_page_callback(query: dict, request_id: str, page_num: int) -> dict:
    """處理 show_page callback — 從 DynamoDB 拉取指定頁面並發送到 Telegram

    callback_data 格式：show_page:{request_id}:{page_num}

    Args:
        query: Telegram callback query dict
        request_id: 原始命令的 request_id
        page_num: 要顯示的頁碼 (2-based)
    """
    callback_id = query.get('id', '')

    page_id = f"{request_id}:page:{page_num}"
    page_data = get_paged_output(page_id)

    if 'error' in page_data:
        answer_callback(callback_id, '❌ 頁面不存在或已過期')
        return response(200, {'ok': True})

    total_pages = page_data.get('total_pages', page_num)
    has_more = page_num < total_pages

    content_text = page_data.get('result', '')

    # Build Next Page button if more pages remain
    if has_more:
        next_page_num = page_num + 1
        next_btn = {
            'inline_keyboard': [[{
                'text': f'➡️ Next Page ({next_page_num}/{total_pages})',
                'callback_data': f'show_page:{request_id}:{next_page_num}',
            }]]
        }
    else:
        next_btn = None

    answer_callback(callback_id, f'📄 第 {page_num}/{total_pages} 頁')
    send_telegram_message_silent(
        f"📄 *第 {page_num}/{total_pages} 頁*\n\n```\n{content_text}\n```",
        reply_markup=next_btn,
    )

    return response(200, {'ok': True})



def _auto_execute_pending_requests(trust_scope: str, account_id: str, assume_role: str,
                                    trust_id: str, source: str = ''):
    """信任開啟後，自動執行同 trust_scope + account 的排隊中請求"""
    if not trust_scope:
        return

    table = _db.table

    # 查 pending 請求，用 status-created-index + filter by trust_scope + account
    response_data = table.query(
        IndexName='status-created-index',
        KeyConditionExpression='#status = :status',
        FilterExpression='trust_scope = :scope AND account_id = :account',
        ExpressionAttributeNames={
            '#status': 'status',
        },
        ExpressionAttributeValues={
            ':status': 'pending',
            ':scope': trust_scope,
            ':account': account_id,
        },
        ScanIndexForward=True,
        Limit=20,
    )

    items = response_data.get('Items', [])
    if not items:
        return

    from trust import increment_trust_command_count
    from utils import generate_request_id, log_decision

    executed = 0
    for item in items:
        req_id = item['request_id']
        cmd = item.get('command', '')
        reason = item.get('reason', '')
        item_source = item.get('source', source)
        item_assume_role = item.get('assume_role', assume_role)

        # SEC-013: 重跑 compliance check，不合規的 pending 命令拒絕執行
        try:
            from compliance_checker import check_compliance
            is_compliant, violation = check_compliance(cmd)
            if not is_compliant:
                logger.warning("Pending request failed compliance", extra={"src_module": "trust", "sec_rule": "SEC-013", "request_id": req_id, "violation_rule": violation.rule_id if violation else "unknown"})
                # 更新狀態為 compliance_rejected
                now_rej = int(time.time())
                table.update_item(
                    Key={'request_id': req_id},
                    UpdateExpression='SET #s = :s, compliance_rejected_at = :t, compliance_rule = :rule',
                    ExpressionAttributeNames={'#s': 'status'},
                    ExpressionAttributeValues={
                        ':s': 'compliance_rejected',
                        ':t': now_rej,
                        ':rule': violation.rule_id if violation else 'unknown',
                    },
                )
                continue
        except ImportError:
            pass  # compliance_checker 不存在時跳過

        # 執行命令
        try:
            send_chat_action('typing')
        except Exception:
            pass
        result = execute_command(cmd, item_assume_role)
        cmd_status = 'failed' if _is_execute_failed(result) else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'trust_callback'})
        paged = store_paged_output(req_id, result)

        # 更新 DynamoDB 狀態
        now = int(time.time())
        table.update_item(
            Key={'request_id': req_id},
            UpdateExpression='SET #s = :s, #r = :r, approved_at = :t, decision_type = :dt, decided_at = :da, command_status = :cs',
            ExpressionAttributeNames={'#s': 'status', '#r': 'result'},
            ExpressionAttributeValues={
                ':s': 'approved',
                ':r': paged['result'],
                ':t': now,
                ':dt': 'trust_auto_approved',
                ':da': now,
                ':cs': cmd_status,
            },
        )

        # Trust 計數
        new_count = increment_trust_command_count(trust_id)

        # 計算剩餘時間
        remaining = "~10:00"  # 剛建的 trust session，約 10 分鐘

        # 靜默通知
        send_trust_auto_approve_notification(
            cmd, trust_id, remaining, new_count, result,
            source=item_source,
            reason=reason,
        )

        # Track command in trust session for summary (sprint9-007-phase-a)
        is_failed = _is_execute_failed(result)
        track_command_executed(trust_id, cmd, not is_failed)

        # Audit log
        log_decision(
            table=table,
            request_id=generate_request_id(cmd),
            command=cmd,
            reason=reason,
            source=item_source,
            account_id=account_id,
            decision_type='trust_approved',
            mode='mcp',
            trust_session_id=trust_id,
        )

        executed += 1

    if executed > 0:
        logger.info("Auto-executed pending requests", extra={"src_module": "trust", "operation": "auto_execute_complete", "executed": executed, "trust_scope": trust_scope})
