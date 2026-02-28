"""
Bouncer - Telegram 命令處理模組

所有 handle_*_command 函數
"""

import logging
import time



# 從其他模組導入
from utils import response
from accounts import init_default_account, list_accounts
from telegram import _telegram_request
from constants import APPROVED_CHAT_IDS
import db as _db

logger = logging.getLogger(__name__)


def _get_table():
    """取得 DynamoDB table"""
    return _db.table


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

    # /stats [hours] - 統計資訊
    if text == '/stats' or text.startswith('/stats ') or text.startswith('/stats@'):
        # 解析小時數
        hours = 24
        parts = text.split()
        if len(parts) >= 2:
            try:
                hours = int(parts[1])
            except ValueError:
                pass
        return handle_stats_command(chat_id, hours=hours)

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
        logger.error(f"Error: {e}")
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
        logger.error(f"Error: {e}")
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


def handle_stats_command(chat_id: str, hours: int = 24) -> dict:
    """處理 /stats [hours] 指令

    Args:
        chat_id: Telegram chat ID
        hours: 查詢過去 N 小時（預設 24）
    """
    table = _get_table()
    now = int(time.time())
    since_ts = now - hours * 3600

    try:
        from boto3.dynamodb.conditions import Attr
        resp = table.scan(
            FilterExpression=Attr('created_at').gte(since_ts)
        )
        items = resp.get('Items', [])
        # 處理分頁
        while 'LastEvaluatedKey' in resp:
            resp = table.scan(
                FilterExpression=Attr('created_at').gte(since_ts),
                ExclusiveStartKey=resp['LastEvaluatedKey']
            )
            items.extend(resp.get('Items', []))
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        items = []

    total = len(items)

    # 統計狀態
    approved = sum(1 for i in items if str(i.get('status', '')).startswith('approved')
                   or i.get('status') in ('auto_approved', 'trust_approved', 'grant_approved'))
    denied = sum(1 for i in items if i.get('status') in ('denied', 'blocked', 'compliance_violation'))
    pending = sum(1 for i in items if str(i.get('status', '')).startswith('pending'))

    # 審批率
    decided = approved + denied
    if decided > 0:
        rate = round(approved / decided * 100)
        rate_str = f"{rate}%"
    else:
        rate_str = "N/A"

    # Hourly breakdown — 找尖峰時段
    import datetime
    hourly: dict = {}
    for item in items:
        created_at = item.get('created_at')
        if not created_at:
            continue
        try:
            ts = int(float(str(created_at)))
            dt = datetime.datetime.utcfromtimestamp(ts)
            hour_key = dt.strftime('%Y-%m-%dT%H')
            hourly[hour_key] = hourly.get(hour_key, 0) + 1
        except Exception:
            continue

    # 尖峰時段
    peak_line = ""
    if hourly:
        peak_hour = max(hourly, key=lambda k: hourly[k])
        peak_count = hourly[peak_hour]
        peak_line = f"\n📈 尖峰時段: {peak_hour} ({peak_count} requests)"

    text = (
        f"📊 統計資訊（過去 {hours}h）\n"
        f"\n"
        f"📋 總請求: {total}\n"
        f"✅ 批准: {approved}\n"
        f"❌ 拒絕: {denied}\n"
        f"⏳ 待審批: {pending}\n"
        f"📈 審批率: {rate_str}"
        f"{peak_line}"
    )

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})


def handle_help_command(chat_id: str) -> dict:
    """處理 /help 指令"""
    text = """🔐 Bouncer Commands

/accounts - 列出 AWS 帳號
/trust - 列出信任時段
/pending - 列出待審批請求
/stats [hours] - 統計資訊（預設 24h）
/help - 顯示此說明"""

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})
