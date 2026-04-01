"""
Bouncer - Telegram 命令處理模組

所有 handle_*_command 函數
"""

import time

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

# 從其他模組導入
from utils import response
from accounts import init_default_account, list_accounts
from telegram import send_telegram_message_to
from constants import APPROVED_CHAT_IDS
import db as _db

logger = Logger(service="bouncer")


def _get_table():
    """取得 DynamoDB table"""
    return _db.table


def handle_telegram_command(message: dict) -> dict:
    """處理 Telegram 文字指令"""
    user_id = str(message.get('from', {}).get('id', ''))
    chat_id = str(message.get('chat', {}).get('id', ''))
    text = message.get('text', '').strip()

    logger.info(
        "Telegram command received",
        extra={
            "src_module": "telegram_commands",
            "operation": "handle_telegram_command",
            "user_id": user_id,
            "command_type": text.split()[0] if text else "",
        }
    )

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

    # /otp {code} - OTP 驗證
    if text.startswith('/otp ') or text.startswith('/otp@'):
        parts = text.split()
        if len(parts) >= 2:
            code = parts[1].strip()
            return handle_otp_command(chat_id, user_id, code)
        else:
            send_telegram_message_to(chat_id, "❌ 用法：/otp {驗證碼}\n範例：/otp 123456")
            return response(200, {'ok': True})

    # /logs [account] - 列出允許的 log groups
    if text == '/logs' or text.startswith('/logs ') or text.startswith('/logs@'):
        parts = text.split()
        account = parts[1] if len(parts) >= 2 else ''
        return handle_logs_command(chat_id, account)

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
        from boto3.dynamodb.conditions import Key
        # Use type-expires-at-index GSI: PK=type, SK=expires_at
        resp = table.query(
            IndexName='type-expires-at-index',
            KeyConditionExpression=Key('type').eq('trust_session') & Key('expires_at').gt(now)
        )
        items = resp.get('Items', [])
    except ClientError as e:
        logger.error("Query trust sessions error: %s", e, extra={"src_module": "telegram_commands", "operation": "handle_trust_command", "error": str(e)})
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
        from boto3.dynamodb.conditions import Key
        # Use status-created-index GSI: PK=status, SK=created_at — ALL projection
        resp = table.query(
            IndexName='status-created-index',
            KeyConditionExpression=Key('status').eq('pending'),
            ScanIndexForward=False,
        )
        items = resp.get('Items', [])
    except ClientError as e:
        logger.error("Query pending requests error: %s", e, extra={"src_module": "telegram_commands", "operation": "handle_pending_command", "error": str(e)})
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

    # Collect items via GSI Query for each known status value, then merge.
    # This avoids a full table Scan by leveraging status-created-index (ALL projection).
    KNOWN_STATUSES = [
        'approved', 'auto_approved', 'trust_approved', 'grant_approved',
        'denied', 'blocked', 'compliance_violation',
        'pending', 'pending_approval',
        'error',
    ]

    try:
        from boto3.dynamodb.conditions import Key
        items = []
        for status_val in KNOWN_STATUSES:
            resp = table.query(
                IndexName='status-created-index',
                KeyConditionExpression=(
                    Key('status').eq(status_val) & Key('created_at').gte(since_ts)
                ),
                ScanIndexForward=False,
            )
            items.extend(resp.get('Items', []))
            # Handle pagination within each status
            while 'LastEvaluatedKey' in resp:
                resp = table.query(
                    IndexName='status-created-index',
                    KeyConditionExpression=(
                        Key('status').eq(status_val) & Key('created_at').gte(since_ts)
                    ),
                    ScanIndexForward=False,
                    ExclusiveStartKey=resp['LastEvaluatedKey'],
                )
                items.extend(resp.get('Items', []))
    except ClientError as e:
        logger.error("Stats query error: %s", e, extra={"src_module": "telegram_commands", "operation": "handle_stats", "error": str(e)})
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
        except (ValueError, OSError):
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


def handle_otp_command(chat_id: str, user_id: str, provided_code: str) -> dict:
    """處理 /otp {code} 指令 — OTP 二次驗證"""
    from otp import get_pending_otp, validate_otp

    logger.info(
        "OTP command started",
        extra={
            "src_module": "telegram_commands",
            "operation": "handle_otp_command",
            "user_id": user_id,
        }
    )

    # Find pending OTP for this user
    otp_record = get_pending_otp(user_id)
    if not otp_record:
        send_telegram_message_to(chat_id, "❌ 沒有待驗證的 OTP，或 OTP 已過期")
        return response(200, {'ok': True})

    # Extract original request_id (strip 'otp#' prefix)
    full_key = otp_record['request_id']
    original_request_id = otp_record.get('original_request_id') or full_key[len('otp#'):]

    success, msg = validate_otp(original_request_id, provided_code)

    if not success:
        logger.info(
            "OTP validation failed",
            extra={
                "src_module": "telegram_commands",
                "operation": "handle_otp_command",
                "user_id": user_id,
                "result": "fail",
            }
        )
        send_telegram_message_to(chat_id, f"❌ {msg}")
        return response(200, {'ok': True})

    logger.info(
        "OTP validation successful",
        extra={
            "src_module": "telegram_commands",
            "operation": "handle_otp_command",
            "user_id": user_id,
            "result": "success",
            "request_id": original_request_id,
        }
    )

    # OTP valid - retrieve original request and execute
    send_telegram_message_to(chat_id, f"✅ {msg}，正在執行命令...")

    # Get the original pending item from DDB
    table = _get_table()
    try:
        item = table.get_item(Key={'request_id': original_request_id}).get('Item')
    except Exception as e:
        logger.error("Failed to get original request: %s", e, extra={"src_module": "telegram_commands", "operation": "handle_otp", "request_id": original_request_id})
        send_telegram_message_to(chat_id, f"❌ 找不到原始請求：{original_request_id}")
        return response(200, {'ok': True})

    if not item:
        send_telegram_message_to(chat_id, "❌ 原始審批請求已過期")
        return response(200, {'ok': True})

    # Mark item as OTP verified and re-invoke handle_command_callback
    item['otp_verified'] = True

    from callbacks_command import handle_command_callback
    message_id = int(otp_record.get('message_id', 0))

    # Execute the command by calling handle_command_callback with otp_verified=True
    handle_command_callback(
        action='approve',
        request_id=original_request_id,
        item=item,
        message_id=message_id,
        callback_id='',  # No callback_id for /otp command path
        user_id=user_id,
    )

    return response(200, {'ok': True})


def handle_logs_command(chat_id: str, account: str = '') -> dict:
    """處理 /logs [account] 指令 — 列出允許查詢的 log groups"""
    from mcp_query_logs import _list_allowlist
    from constants import DEFAULT_ACCOUNT_ID

    account_id = account.strip() or DEFAULT_ACCOUNT_ID
    if not account_id:
        send_telegram_message_to(chat_id, "❌ 無法確定帳號 ID")
        return response(200, {'ok': True})

    items = _list_allowlist(account_id)

    if not items:
        text = f"📁 Log 允許名單（{account_id}）\n\n尚無任何 log group"
    else:
        lines = [f"📁 Log 允許名單（{account_id}）\n"]
        for item in items:
            lg = item.get('log_group', '')
            added_by = item.get('added_by', '')
            source = f" — {added_by}" if added_by else ""
            lines.append(f"• `{lg}`{source}")
        lines.append(f"\n共 {len(items)} 筆")
        text = "\n".join(lines)

    send_telegram_message_to(chat_id, text)
    return response(200, {'ok': True})


def handle_help_command(chat_id: str) -> dict:
    """處理 /help 指令"""
    text = """🔐 Bouncer Commands

/accounts - 列出 AWS 帳號
/trust - 列出信任時段
/pending - 列出待審批請求
/stats [hours] - 統計資訊（預設 24h）
/otp {code} - OTP 二次驗證（用於高風險命令）
/logs [account] - 列出允許查詢的 log groups
/help - 顯示此說明"""

    send_telegram_message_to(chat_id, text, parse_mode=None)
    return response(200, {'ok': True})
