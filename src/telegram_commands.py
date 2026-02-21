"""
Bouncer - Telegram 命令處理模組

所有 handle_*_command 函數
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

# 從其他模組導入
from utils import response
from accounts import init_default_account, list_accounts
from telegram import _telegram_request
from constants import APPROVED_CHAT_IDS


# 延遲 import 避免循環依賴
def _get_app_module():
    """延遲取得 app module 避免循環 import"""
    import app as app_module
    return app_module

def _get_table():
    """取得 DynamoDB table"""
    app = _get_app_module()
    return app.table


def send_telegram_message_to(chat_id: str, text: str, parse_mode: str = None):
    """發送訊息到指定 chat"""
    data = {
        'chat_id': chat_id,
        'text': text
    }
    if parse_mode:
        data['parse_mode'] = parse_mode
    _telegram_request('sendMessage', data, timeout=10, json_body=True)


def handle_telegram_command(message: dict) -> dict:
    """處理 Telegram 文字指令"""
    user_id = str(message.get('from', {}).get('id', ''))
    chat_id = str(message.get('chat', {}).get('id', ''))
    text = message.get('text', '').strip()

    # 權限檢查
    if user_id not in APPROVED_CHAT_IDS:
        return response(200, {'ok': True})  # 忽略非授權用戶

    # /accounts - 列出帳號
    if text == '/accounts' or text.startswith('/accounts@'):
        return handle_accounts_command(chat_id)

    # /trust - 列出信任時段
    if text == '/trust' or text.startswith('/trust@'):
        return handle_trust_command(chat_id)

    # /pending - 列出待審批
    if text == '/pending' or text.startswith('/pending@'):
        return handle_pending_command(chat_id)

    # /help - 顯示指令列表
    if text == '/help' or text.startswith('/help@') or text == '/start' or text.startswith('/start@'):
        return handle_help_command(chat_id)

    return response(200, {'ok': True})


def handle_accounts_command(chat_id: str) -> dict:
    """處理 /accounts 指令"""
    init_default_account()
    accounts = list_accounts()

    if not accounts:
        text = "📋 AWS 帳號\n\n尚未配置任何帳號"
    else:
        lines = ["📋 AWS 帳號\n"]
        for acc in accounts:
            status = "✅" if acc.get('enabled', True) else "❌"
            default = " (預設)" if acc.get('is_default') else ""
            lines.append(f"{status} {acc['account_id']} - {acc.get('name', 'N/A')}{default}")
        text = "\n".join(lines)

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})


def handle_trust_command(chat_id: str) -> dict:
    """處理 /trust 指令"""
    table = _get_table()
    now = int(time.time())

    try:
        resp = table.scan(
            FilterExpression='#type = :type AND expires_at > :now',
            ExpressionAttributeNames={'#type': 'type'},
            ExpressionAttributeValues={
                ':type': 'trust_session',
                ':now': now
            }
        )
        items = resp.get('Items', [])
    except Exception as e:
        print(f"Error: {e}")
        items = []

    if not items:
        text = "🔓 信任時段\n\n目前沒有活躍的信任時段"
    else:
        lines = ["🔓 信任時段\n"]
        for item in items:
            remaining = int(item.get('expires_at', 0)) - now
            mins, secs = divmod(remaining, 60)
            count = int(item.get('command_count', 0))
            source = item.get('source', 'N/A')
            lines.append(f"• {source}\n  ⏱️ {mins}:{secs:02d} 剩餘 | 📊 {count}/20 命令")
        text = "\n".join(lines)

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})


def handle_pending_command(chat_id: str) -> dict:
    """處理 /pending 指令"""
    table = _get_table()

    try:
        resp = table.scan(
            FilterExpression='#status = :status',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':status': 'pending'}
        )
        items = resp.get('Items', [])
    except Exception as e:
        print(f"Error: {e}")
        items = []

    if not items:
        text = "⏳ 待審批請求\n\n目前沒有待審批的請求"
    else:
        lines = ["⏳ 待審批請求\n"]
        now = int(time.time())
        for item in items:
            age = now - int(item.get('created_at', now))
            mins, secs = divmod(age, 60)
            cmd = item.get('command', '')[:50]
            source = item.get('source', 'N/A')
            lines.append(f"• {cmd}\n  👤 {source} | ⏱️ {mins}m{secs}s ago")
        text = "\n".join(lines)

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})


def handle_help_command(chat_id: str) -> dict:
    """處理 /help 指令"""
    text = """🔐 Bouncer Commands

/accounts - 列出 AWS 帳號
/trust - 列出信任時段
/pending - 列出待審批請求
/help - 顯示此說明"""

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})
