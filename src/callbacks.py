"""
Bouncer - Telegram Callback 處理模組

所有 handle_*_callback 函數
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

# 從其他模組導入
from utils import response
from commands import execute_command
from paging import store_paged_output, send_remaining_pages
from trust import create_trust_session
from telegram import escape_markdown, update_message, answer_callback, update_and_answer
from constants import DEFAULT_ACCOUNT_ID


# 延遲 import 避免循環依賴
def _get_app_module():
    """延遲取得 app module 避免循環 import"""
    import app as app_module
    return app_module

def _get_table():
    """取得 DynamoDB table"""
    app = _get_app_module()
    return app.table

def _get_accounts_table():
    """取得 accounts DynamoDB table"""
    app = _get_app_module()
    return app.accounts_table


def handle_command_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str):
    """處理命令執行的審批 callback"""
    table = _get_table()

    command = item.get('command', '')
    assume_role = item.get('assume_role')
    source = item.get('source', '')
    reason = item.get('reason', '')
    account_id = item.get('account_id', DEFAULT_ACCOUNT_ID)
    account_name = item.get('account_name', 'Default')

    source_line = f"🤖 *來源：* {source}\n" if source else ""
    account_line = f"🏢 *帳號：* `{account_id}` ({account_name})\n"

    if action == 'approve':
        result = execute_command(command, assume_role)
        paged = store_paged_output(request_id, result)

        # 存入 DynamoDB（包含分頁資訊）
        update_expr = 'SET #s = :s, #r = :r, approved_at = :t, approver = :a'
        expr_names = {'#s': 'status', '#r': 'result'}
        expr_values = {
            ':s': 'approved',
            ':r': paged['result'],
            ':t': int(time.time()),
            ':a': user_id
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

        result_preview = result[:1000] if len(result) > 1000 else result
        if paged.get('paged'):
            truncate_notice = f"\n\n⚠️ 輸出較長 ({paged['output_length']} 字元，共 {paged['total_pages']} 頁)"
        else:
            truncate_notice = ""
        update_and_answer(
            message_id,
            f"✅ *已批准並執行*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"📤 *結果：*\n```\n{result_preview}\n```{truncate_notice}",
            callback_id,
            '✅ 已執行'
        )
        # 自動發送剩餘頁面
        if paged.get('paged'):
            send_remaining_pages(request_id, paged['total_pages'])

    elif action == 'approve_trust':
        # 批准並建立信任時段
        result = execute_command(command, assume_role)
        paged = store_paged_output(request_id, result)

        # 存入 DynamoDB（包含分頁資訊）
        update_expr = 'SET #s = :s, #r = :r, approved_at = :t, approver = :a'
        expr_names = {'#s': 'status', '#r': 'result'}
        expr_values = {
            ':s': 'approved',
            ':r': paged['result'],
            ':t': int(time.time()),
            ':a': user_id
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

        # 建立信任時段
        trust_id = create_trust_session(source, account_id, user_id)

        result_preview = result[:800] if len(result) > 800 else result
        if paged.get('paged'):
            truncate_notice = f"\n\n⚠️ 輸出較長 ({paged['output_length']} 字元，共 {paged['total_pages']} 頁)"
        else:
            truncate_notice = ""
        update_and_answer(
            message_id,
            f"✅ *已批准並執行* + 🔓 *信任 10 分鐘*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"📤 *結果：*\n```\n{result_preview}\n```{truncate_notice}\n\n"
            f"🔓 信任時段已啟動：`{trust_id}`",
            callback_id,
            '✅ 已執行 + 🔓 信任啟動'
        )
        # 自動發送剩餘頁面
        if paged.get('paged'):
            send_remaining_pages(request_id, paged['total_pages'])

    elif action == 'deny':
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'denied',
                ':t': int(time.time()),
                ':a': user_id
            }
        )

        update_and_answer(
            message_id,
            f"❌ *已拒絕*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}",
            callback_id,
            '❌ 已拒絕'
        )

    return response(200, {'ok': True})


def handle_account_add_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str):
    """處理新增帳號的審批 callback"""
    table = _get_table()
    accounts_table = _get_accounts_table()

    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    role_arn = item.get('role_arn', '')
    source = item.get('source', '')

    source_line = f"🤖 *來源：* {source}\n" if source else ""

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

            table.update_item(
                Key={'request_id': request_id},
                UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={
                    ':s': 'approved',
                    ':t': int(time.time()),
                    ':a': user_id
                }
            )

            update_message(
                message_id,
                f"✅ *已新增帳號*\n\n"
                f"{source_line}"
                f"🆔 *帳號 ID：* `{account_id}`\n"
                f"📛 *名稱：* {account_name}\n"
                f"🔗 *Role：* `{role_arn}`"
            )
            answer_callback(callback_id, '✅ 帳號已新增')

        except Exception as e:
            answer_callback(callback_id, f'❌ 新增失敗: {str(e)[:50]}')
            return response(500, {'error': str(e)})

    elif action == 'deny':
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'denied',
                ':t': int(time.time()),
                ':a': user_id
            }
        )

        update_message(
            message_id,
            f"❌ *已拒絕新增帳號*\n\n"
            f"{source_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {account_name}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})


def handle_account_remove_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str):
    """處理移除帳號的審批 callback"""
    table = _get_table()
    accounts_table = _get_accounts_table()

    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    source = item.get('source', '')

    source_line = f"🤖 *來源：* {source}\n" if source else ""

    if action == 'approve':
        try:
            accounts_table.delete_item(Key={'account_id': account_id})

            table.update_item(
                Key={'request_id': request_id},
                UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={
                    ':s': 'approved',
                    ':t': int(time.time()),
                    ':a': user_id
                }
            )

            update_message(
                message_id,
                f"✅ *已移除帳號*\n\n"
                f"{source_line}"
                f"🆔 *帳號 ID：* `{account_id}`\n"
                f"📛 *名稱：* {account_name}"
            )
            answer_callback(callback_id, '✅ 帳號已移除')

        except Exception as e:
            answer_callback(callback_id, f'❌ 移除失敗: {str(e)[:50]}')
            return response(500, {'error': str(e)})

    elif action == 'deny':
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'denied',
                ':t': int(time.time()),
                ':a': user_id
            }
        )

        update_message(
            message_id,
            f"❌ *已拒絕移除帳號*\n\n"
            f"{source_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {account_name}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})


def handle_deploy_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str):
    """處理部署的審批 callback"""
    from deployer import start_deploy
    table = _get_table()

    project_id = item.get('project_id', '')
    project_name = item.get('project_name', project_id)
    branch = item.get('branch', 'master')
    stack_name = item.get('stack_name', '')
    source = item.get('source', '')
    reason = item.get('reason', '')

    source_line = f"🤖 *來源：* {source}\n" if source else ""

    if action == 'approve':
        # 更新審批狀態
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'approved',
                ':t': int(time.time()),
                ':a': user_id
            }
        )

        # 啟動部署
        result = start_deploy(project_id, branch, user_id, reason)

        if 'error' in result:
            update_message(
                message_id,
                f"❌ *部署啟動失敗*\n\n"
                f"{source_line}"
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
                f"{source_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}\n"
                f"{reason_line}"
                f"📋 *Stack：* {stack_name}\n\n"
                f"🆔 *部署 ID：* `{deploy_id}`\n\n"
                f"⏳ 部署進行中..."
            )
            answer_callback(callback_id, '🚀 部署已啟動')

    elif action == 'deny':
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'denied',
                ':t': int(time.time()),
                ':a': user_id
            }
        )

        update_message(
            message_id,
            f"❌ *已拒絕部署*\n\n"
            f"{source_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n"
            f"📋 *Stack：* {stack_name}\n\n"
            f"💬 *原因：* {reason}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})


def handle_upload_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str):
    """處理上傳的審批 callback"""
    app = _get_app_module()
    table = _get_table()

    bucket = item.get('bucket', '')
    key = item.get('key', '')
    content_size = int(item.get('content_size', 0))
    source = item.get('source', '')
    reason = item.get('reason', '')
    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')

    s3_uri = f"s3://{bucket}/{key}"
    source_line = f"🤖 來源： {source}\n" if source else ""
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
                f"{source_line}"
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
                f"{source_line}"
                f"{account_line}"
                f"📁 目標： {s3_uri}\n"
                f"📊 大小： {size_str}\n"
                f"❗ 錯誤： {error}\n"
                f"💬 原因： {reason}"
            )
            answer_callback(callback_id, '❌ 上傳失敗')

    elif action == 'deny':
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'denied',
                ':t': int(time.time()),
                ':a': user_id
            }
        )

        update_message(
            message_id,
            f"❌ 已拒絕上傳\n\n"
            f"{source_line}"
            f"{account_line}"
            f"📁 目標： {s3_uri}\n"
            f"📊 大小： {size_str}\n"
            f"💬 原因： {reason}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})
