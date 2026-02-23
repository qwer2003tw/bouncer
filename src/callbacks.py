"""
Bouncer - Telegram Callback 處理模組

所有 handle_*_callback 函數
"""

import time


# 從其他模組導入
from utils import response
from commands import execute_command
from paging import store_paged_output, send_remaining_pages
from trust import create_trust_session
from telegram import escape_markdown, update_message, answer_callback, update_and_answer
from constants import DEFAULT_ACCOUNT_ID
from metrics import emit_metric


# DynamoDB tables from db.py (no circular dependency)
import db as _db


def _get_app_module():
    """延遲取得 app module — 只用於 execute_upload"""
    import app as app_module
    return app_module

def _get_table():
    """取得 DynamoDB table"""
    return _db.table

def _get_accounts_table():
    """取得 accounts DynamoDB table"""
    return _db.accounts_table


# ============================================================================
# Grant Session Callbacks
# ============================================================================

def handle_grant_approve_all(query: dict, grant_id: str) -> dict:
    """處理 Grant 全部批准 callback"""
    from grant import approve_grant, get_grant_session
    from notifications import send_grant_complete_notification
    from telegram import update_and_answer, escape_markdown

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    try:
        grant = approve_grant(grant_id, user_id, mode='all')
        if not grant:
            answer_callback(callback_id, '❌ Grant 不存在或已處理')
            return response(200, {'ok': True})

        granted = grant.get('granted_commands', [])
        ttl_minutes = grant.get('ttl_minutes', 30)

        update_and_answer(
            message_id,
            f"✅ *Grant 已批准（全部）*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"📋 *已授權命令：* {len(granted)} 個\n"
            f"⏱ *有效時間：* {ttl_minutes} 分鐘\n"
            f"👤 *批准者：* {user_id}",
            callback_id,
            f'✅ 已批准 {len(granted)} 個命令'
        )

        return response(200, {'ok': True})

    except Exception as e:
        print(f"[GRANT] handle_grant_approve_all error: {e}")
        answer_callback(callback_id, f'❌ 批准失敗: {str(e)[:50]}')
        return response(500, {'error': str(e)})


def handle_grant_approve_safe(query: dict, grant_id: str) -> dict:
    """處理 Grant 只批准安全命令 callback"""
    from grant import approve_grant, get_grant_session
    from notifications import send_grant_complete_notification
    from telegram import update_and_answer, escape_markdown

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    try:
        grant = approve_grant(grant_id, user_id, mode='safe_only')
        if not grant:
            answer_callback(callback_id, '❌ Grant 不存在或已處理')
            return response(200, {'ok': True})

        granted = grant.get('granted_commands', [])
        ttl_minutes = grant.get('ttl_minutes', 30)

        update_and_answer(
            message_id,
            f"✅ *Grant 已批准（僅安全）*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"📋 *已授權命令：* {len(granted)} 個\n"
            f"⏱ *有效時間：* {ttl_minutes} 分鐘\n"
            f"👤 *批准者：* {user_id}",
            callback_id,
            f'✅ 已批准 {len(granted)} 個安全命令'
        )

        return response(200, {'ok': True})

    except Exception as e:
        print(f"[GRANT] handle_grant_approve_safe error: {e}")
        answer_callback(callback_id, f'❌ 批准失敗: {str(e)[:50]}')
        return response(500, {'error': str(e)})


def handle_grant_deny(query: dict, grant_id: str) -> dict:
    """處理 Grant 拒絕 callback"""
    from grant import deny_grant
    from telegram import update_and_answer

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    try:
        success = deny_grant(grant_id)
        if not success:
            answer_callback(callback_id, '❌ 拒絕失敗')
            return response(200, {'ok': True})

        update_and_answer(
            message_id,
            f"❌ *Grant 已拒絕*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"👤 *拒絕者：* {user_id}",
            callback_id,
            '❌ 已拒絕'
        )

        return response(200, {'ok': True})

    except Exception as e:
        print(f"[GRANT] handle_grant_deny error: {e}")
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
    update_expr = 'SET #s = :s, approved_at = :t, approver = :a'
    expr_names = {'#s': 'status'}
    expr_values = {
        ':s': status,
        ':t': now,
        ':a': approver,
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
    source = item.get('source', '')
    context = item.get('context', '')

    source_line = f"🤖 *來源：* {source}\n" if source else ""
    context_line = f"📝 *任務：* {context}\n" if context else ""

    update_message(
        message_id,
        f"{status_emoji} *{title}*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{source_line}"
        f"{context_line}"
        f"{extra_lines}"
    )


# ============================================================================
# Command Callback
# ============================================================================

def handle_command_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理命令執行的審批 callback"""
    table = _get_table()

    command = item.get('command', '')
    assume_role = item.get('assume_role')
    source = item.get('source', '')
    reason = item.get('reason', '')
    context = item.get('context', '')
    account_id = item.get('account_id', DEFAULT_ACCOUNT_ID)
    account_name = item.get('account_name', 'Default')

    source_line = f"🤖 *來源：* {source}\n" if source else ""
    context_line = f"📝 *任務：* {context}\n" if context else ""
    account_line = f"🏢 *帳號：* `{account_id}` ({account_name})\n"

    if action in ('approve', 'approve_trust'):
        result = execute_command(command, assume_role)
        cmd_status = 'failed' if result.startswith('❌') else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status})
        paged = store_paged_output(request_id, result)

        now = int(time.time())
        created_at = int(item.get('created_at', 0))
        decision_latency_ms = (now - created_at) * 1000 if created_at else 0

        decision_type = 'manual_approved_trust' if action == 'approve_trust' else 'manual_approved'

        # 存入 DynamoDB（包含分頁資訊）
        update_expr = 'SET #s = :s, #r = :r, approved_at = :t, approver = :a, decision_type = :dt, decided_at = :da, decision_latency_ms = :dl'
        expr_names = {'#s': 'status', '#r': 'result'}
        expr_values = {
            ':s': 'approved',
            ':r': paged['result'],
            ':t': now,
            ':a': user_id,
            ':dt': decision_type,
            ':da': now,
            ':dl': decision_latency_ms
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

        # 信任模式
        trust_line = ""
        if action == 'approve_trust':
            trust_id = create_trust_session(source, account_id, user_id)
            trust_line = f"\n\n🔓 信任時段已啟動：`{trust_id}`"

        max_preview = 800 if action == 'approve_trust' else 1000
        result_preview = result[:max_preview] if len(result) > max_preview else result
        if paged.get('paged'):
            truncate_notice = f"\n\n⚠️ 輸出較長 ({paged['output_length']} 字元，共 {paged['total_pages']} 頁)"
        else:
            truncate_notice = ""

        title = "✅ *已批准並執行* + 🔓 *信任 10 分鐘*" if action == 'approve_trust' else "✅ *已批准並執行*"
        cb_text = '✅ 已執行 + 🔓 信任啟動' if action == 'approve_trust' else '✅ 已執行'

        update_and_answer(
            message_id,
            f"{title}\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{source_line}"
            f"{context_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"📤 *結果：*\n```\n{result_preview}\n```{truncate_notice}{trust_line}",
            callback_id,
            cb_text
        )
        # 自動發送剩餘頁面
        if paged.get('paged'):
            send_remaining_pages(request_id, paged['total_pages'])

    elif action == 'deny':
        now = int(time.time())
        created_at = int(item.get('created_at', 0))
        decision_latency_ms = (now - created_at) * 1000 if created_at else 0

        _update_request_status(table, request_id, 'denied', user_id, extra_attrs={
            'decision_type': 'manual_denied',
            'decided_at': now,
            'decision_latency_ms': decision_latency_ms,
        })

        update_and_answer(
            message_id,
            f"❌ *已拒絕*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{source_line}"
            f"{context_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}",
            callback_id,
            '❌ 已拒絕'
        )

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
            answer_callback(callback_id, '✅ 帳號已新增')

        except Exception as e:
            answer_callback(callback_id, f'❌ 新增失敗: {str(e)[:50]}')
            return response(500, {'error': str(e)})

    elif action == 'deny':
        _update_request_status(table, request_id, 'denied', user_id)

        _send_status_update(
            message_id, '❌', '已拒絕新增帳號',
            {'request_id': request_id, 'source': source, 'context': context},
            extra_lines=detail_lines
        )
        answer_callback(callback_id, '❌ 已拒絕')

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
        try:
            accounts_table.delete_item(Key={'account_id': account_id})

            _update_request_status(table, request_id, 'approved', user_id)

            _send_status_update(
                message_id, '✅', '已移除帳號',
                {'request_id': request_id, 'source': source, 'context': context},
                extra_lines=detail_lines
            )
            answer_callback(callback_id, '✅ 帳號已移除')

        except Exception as e:
            answer_callback(callback_id, f'❌ 移除失敗: {str(e)[:50]}')
            return response(500, {'error': str(e)})

    elif action == 'deny':
        _update_request_status(table, request_id, 'denied', user_id)

        _send_status_update(
            message_id, '❌', '已拒絕移除帳號',
            {'request_id': request_id, 'source': source, 'context': context},
            extra_lines=detail_lines
        )
        answer_callback(callback_id, '❌ 已拒絕')

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

    source_line = f"🤖 *來源：* {source}\n" if source else ""
    context_line = f"📝 *任務：* {context}\n" if context else ""

    if action == 'approve':
        _update_request_status(table, request_id, 'approved', user_id)

        # 啟動部署
        result = start_deploy(project_id, branch, user_id, reason)

        if 'error' in result:
            update_message(
                message_id,
                f"❌ *部署啟動失敗*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{source_line}"
                f"{context_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}\n\n"
                f"❗ *錯誤：* {result['error']}"
            )
            answer_callback(callback_id, '❌ 部署啟動失敗')
        else:
            deploy_id = result.get('deploy_id', '')
            reason_line = f"📝 *原因：* {escape_markdown(reason)}\n" if reason else ""
            update_message(
                message_id,
                f"🚀 *部署已啟動*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{source_line}"
                f"{context_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}\n"
                f"{reason_line}"
                f"📋 *Stack：* {stack_name}\n\n"
                f"🆔 *部署 ID：* `{deploy_id}`\n\n"
                f"⏳ 部署進行中..."
            )
            answer_callback(callback_id, '🚀 部署已啟動')

    elif action == 'deny':
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ *已拒絕部署*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"{context_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n"
            f"📋 *Stack：* {stack_name}\n\n"
            f"💬 *原因：* {reason}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})


# ============================================================================
# Upload Callback
# ============================================================================

def handle_upload_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理上傳的審批 callback"""
    app = _get_app_module()
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
    source_line = f"🤖 來源： {source}\n" if source else ""
    context_line = f"📝 任務： {context}\n" if context else ""
    account_line = f"🏦 帳號： {account_id} ({account_name})\n" if account_id else ""

    # 格式化大小
    if content_size >= 1024 * 1024:
        size_str = f"{content_size / 1024 / 1024:.2f} MB"
    elif content_size >= 1024:
        size_str = f"{content_size / 1024:.2f} KB"
    else:
        size_str = f"{content_size} bytes"

    if action == 'approve':
        # 執行上傳
        result = app.execute_upload(request_id, user_id)

        if result.get('success'):
            update_message(
                message_id,
                f"✅ 已上傳\n\n"
                f"📋 請求 ID： `{request_id}`\n"
                f"{source_line}"
                f"{context_line}"
                f"{account_line}"
                f"📁 目標： {s3_uri}\n"
                f"📊 大小： {size_str}\n"
                f"🔗 URL： {result.get('s3_url', '')}\n"
                f"💬 原因： {reason}"
            )
            answer_callback(callback_id, '✅ 已上傳')
        else:
            # 上傳失敗
            error = result.get('error', 'Unknown error')
            update_message(
                message_id,
                f"❌ 上傳失敗\n\n"
                f"📋 請求 ID： `{request_id}`\n"
                f"{source_line}"
                f"{context_line}"
                f"{account_line}"
                f"📁 目標： {s3_uri}\n"
                f"📊 大小： {size_str}\n"
                f"❗ 錯誤： {error}\n"
                f"💬 原因： {reason}"
            )
            answer_callback(callback_id, '❌ 上傳失敗')

    elif action == 'deny':
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ 已拒絕上傳\n\n"
            f"📋 請求 ID： `{request_id}`\n"
            f"{source_line}"
            f"{context_line}"
            f"{account_line}"
            f"📁 目標： {s3_uri}\n"
            f"📊 大小： {size_str}\n"
            f"💬 原因： {reason}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})
