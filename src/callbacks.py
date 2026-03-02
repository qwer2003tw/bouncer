"""
Bouncer - Telegram Callback 處理模組

所有 handle_*_callback 函數
"""

import time
import logging


# 從其他模組導入
from utils import response, format_size_human, build_info_lines
from commands import execute_command
from paging import store_paged_output, send_remaining_pages
from trust import create_trust_session, track_command_executed
from telegram import escape_markdown, update_message, answer_callback
from notifications import send_trust_auto_approve_notification
from constants import DEFAULT_ACCOUNT_ID, RESULT_TTL, TRUST_SESSION_MAX_UPLOADS, TRUST_SESSION_MAX_COMMANDS
from metrics import emit_metric
from mcp_upload import execute_upload, _verify_upload


# DynamoDB tables from db.py (no circular dependency)
import db as _db

logger = logging.getLogger(__name__)


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
    from grant import approve_grant

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    mode_label = '全部' if mode == 'all' else '僅安全'

    try:
        grant = approve_grant(grant_id, user_id, mode=mode)
        if not grant:
            answer_callback(callback_id, '❌ Grant 不存在或已處理')
            return response(200, {'ok': True})

        granted = grant.get('granted_commands', [])
        ttl_minutes = grant.get('ttl_minutes', 30)

        cb_suffix = '命令' if mode == 'all' else '安全命令'

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

    except Exception as e:
        logger.error(f"[GRANT] handle_grant_approve error (mode={mode}): {e}")
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

    except Exception as e:
        logger.error(f"[GRANT] handle_grant_deny error: {e}")
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

def handle_command_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理命令執行的審批 callback"""
    table = _get_table()

    command = item.get('command', '')
    assume_role = item.get('assume_role')
    source = item.get('source', '')
    trust_scope = item.get('trust_scope', '')
    reason = item.get('reason', '')
    context = item.get('context', '')
    account_id = item.get('account_id', DEFAULT_ACCOUNT_ID)
    account_name = item.get('account_name', 'Default')

    # build_info_lines escapes internally; pass raw values from DB
    source_line = build_info_lines(source=source, context=context)
    safe_account_name = escape_markdown(account_name) if account_name else ''
    account_line = f"🏦 *帳號：* `{account_id}` ({safe_account_name})\n"
    safe_reason = escape_markdown(reason)
    cmd_preview = command[:500] + '...' if len(command) > 500 else command

    if action in ('approve', 'approve_trust'):
        cb_text = '✅ 執行中 + 🔓 信任啟動' if action == 'approve_trust' else '✅ 執行中...'
        answer_callback(callback_id, cb_text)
        result = execute_command(command, assume_role)
        cmd_status = 'failed' if result.startswith('❌') else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'manual_approve'})
        paged = store_paged_output(request_id, result)

        now = int(time.time())
        created_at = int(item.get('created_at', 0))
        decision_latency_ms = (now - created_at) * 1000 if created_at else 0
        if decision_latency_ms:
            emit_metric('Bouncer', 'DecisionLatency', decision_latency_ms, unit='Milliseconds', dimensions={'Action': 'approve'})

        decision_type = 'manual_approved_trust' if action == 'approve_trust' else 'manual_approved'

        # 存入 DynamoDB（包含分頁資訊）
        update_expr = 'SET #s = :s, #r = :r, approved_at = :t, approver = :a, decision_type = :dt, decided_at = :da, decision_latency_ms = :dl, #ttl = :ttl'
        expr_names = {'#s': 'status', '#r': 'result', '#ttl': 'ttl'}
        expr_values = {
            ':s': 'approved',
            ':r': paged['result'],
            ':t': now,
            ':a': user_id,
            ':dt': decision_type,
            ':da': now,
            ':dl': decision_latency_ms,
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

        # 信任模式
        trust_line = ""
        if action == 'approve_trust':
            trust_id = create_trust_session(
                trust_scope, account_id, user_id, source=source,
                max_uploads=TRUST_SESSION_MAX_UPLOADS,
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
            except Exception:
                pass

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
            except Exception as e:
                logger.error(f"[TRUST] Auto-execute pending error: {e}")

        max_preview = 800 if action == 'approve_trust' else 1000
        result_preview = result[:max_preview] if len(result) > max_preview else result
        if paged.get('paged'):
            truncate_notice = f"\n\n⚠️ 輸出較長 ({paged['output_length']} 字元，共 {paged['total_pages']} 頁)"
        else:
            truncate_notice = ""

        title = "✅ *已批准並執行* + 🔓 *信任 10 分鐘*" if action == 'approve_trust' else "✅ *已批准並執行*"
        cb_text = '✅ 已執行 + 🔓 信任啟動' if action == 'approve_trust' else '✅ 已執行'

        update_message(
            message_id,
            f"{title}\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {safe_reason}\n\n"
            f"📤 *結果：*\n```\n{result_preview}\n```{truncate_notice}{trust_line}",
        )
        # 自動發送剩餘頁面
        if paged.get('paged'):
            send_remaining_pages(request_id, paged['total_pages'])

    elif action == 'deny':
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
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {safe_reason}",
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

        except Exception as e:
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

        except Exception as e:
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

def handle_upload_batch_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理批量上傳的審批 callback"""
    import json as _json
    import boto3

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
        try:
            files_manifest = _json.loads(item.get('files', '[]'))
        except Exception:
            answer_callback(callback_id, '❌ 檔案清單解析失敗')
            return response(500, {'error': 'Failed to parse files manifest'})

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

        # Get S3 client
        try:
            if assume_role:
                sts = boto3.client('sts')
                creds = sts.assume_role(
                    RoleArn=assume_role,
                    RoleSessionName='bouncer-batch-upload',
                )['Credentials']
                s3 = boto3.client(
                    's3',
                    aws_access_key_id=creds['AccessKeyId'],
                    aws_secret_access_key=creds['SecretAccessKey'],
                    aws_session_token=creds['SessionToken'],
                )
            else:
                s3 = boto3.client('s3')
        except Exception as e:
            _update_request_status(table, request_id, 'error', user_id, extra_attrs={'error_message': str(e)})
            update_message(
                message_id,
                f"❌ *批量上傳失敗*（S3 連線錯誤）\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"❗ *錯誤：* {str(e)[:200]}",
            )
            return response(500, {'error': str(e)})

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
                    # New path: S3-to-S3 copy from staging bucket (主帳號) to target bucket
                    from constants import DEFAULT_ACCOUNT_ID as _DEFAULT_ACCOUNT_ID
                    staging_bucket = f"bouncer-uploads-{_DEFAULT_ACCOUNT_ID}"
                    s3.copy_object(
                        CopySource={'Bucket': staging_bucket, 'Key': s3_key},
                        Bucket=bucket,
                        Key=fkey,
                        ContentType=fm.get('content_type', 'application/octet-stream'),
                        MetadataDirective='REPLACE',
                    )
                    # Cleanup staging object
                    try:
                        s3.delete_object(Bucket=staging_bucket, Key=s3_key)
                    except Exception:
                        pass  # Non-critical
                else:
                    # Legacy path: decode base64 and upload
                    import base64 as _b64
                    content_bytes = _b64.b64decode(content_b64_legacy or '')
                    s3.put_object(
                        Bucket=bucket,
                        Key=fkey,
                        Body=content_bytes,
                        ContentType=fm.get('content_type', 'application/octet-stream'),
                    )

                # Verify file exists after upload (non-blocking)
                vr = _verify_upload(s3, bucket, fkey, fname)
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
            except Exception as e:
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
                except Exception:
                    pass  # Progress update failure is non-critical

        # Determine final status
        total_files = len(files_manifest)
        success_count = len(uploaded)
        fail_count = len(errors)
        if fail_count == 0:
            upload_status = 'completed'
        elif success_count == 0:
            upload_status = 'failed'
        else:
            upload_status = 'partial'

        # Update DB
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

        # Build trust session if approve_trust
        trust_line = ""
        if action == 'approve_trust' and trust_scope:
            trust_id = create_trust_session(
                trust_scope, account_id, user_id, source=source,
                max_uploads=TRUST_SESSION_MAX_UPLOADS,
            )
            emit_metric('Bouncer', 'TrustSession', 1, dimensions={'Event': 'created'})
            trust_line = (
                f"\n\n🔓 信任時段已啟動：`{trust_id}`"
                f"\n📊 命令: 0/{TRUST_SESSION_MAX_COMMANDS} | 上傳: 0/{TRUST_SESSION_MAX_UPLOADS}"
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
                logger.warning(f"[TRUST][SEC-013] Pending request {req_id} failed compliance: {violation.rule_id if violation else 'unknown'}")
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
        result = execute_command(cmd, item_assume_role)
        cmd_status = 'error' if result.startswith('❌') else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'trust_callback'})
        paged = store_paged_output(req_id, result)

        # 更新 DynamoDB 狀態
        now = int(time.time())
        table.update_item(
            Key={'request_id': req_id},
            UpdateExpression='SET #s = :s, #r = :r, approved_at = :t, decision_type = :dt, decided_at = :da',
            ExpressionAttributeNames={'#s': 'status', '#r': 'result'},
            ExpressionAttributeValues={
                ':s': 'approved',
                ':r': paged['result'],
                ':t': now,
                ':dt': 'trust_auto_approved',
                ':da': now,
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
        is_failed = result.startswith('❌')
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
        logger.info(f"[TRUST] Auto-executed {executed} pending requests for trust_scope={trust_scope}")
