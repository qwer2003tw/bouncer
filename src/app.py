import json
import os
import hashlib
import time
import urllib.request
import urllib.parse
import subprocess
import boto3
from decimal import Decimal

# 環境變數
TELEGRAM_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
APPROVED_CHAT_ID = os.environ['APPROVED_CHAT_ID']
REQUEST_SECRET = os.environ['REQUEST_SECRET']
TABLE_NAME = os.environ['TABLE_NAME']

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

# 命令白名單（自動批准）- 只有 read-only
AUTO_APPROVE_PREFIXES = [
    'aws s3 ls',
    'aws s3api list',
    'aws ec2 describe',
    'aws rds describe',
    'aws lambda list',
    'aws lambda get',
    'aws logs describe',
    'aws logs filter-log-events',
    'aws cloudwatch describe',
    'aws cloudwatch get',
    'aws cloudwatch list',
    'aws iam list',
    'aws iam get',
    'aws sts get-caller-identity',
]

# 命令黑名單（永遠拒絕）
BLOCKED_PREFIXES = [
    'aws iam create',
    'aws iam delete',
    'aws iam attach',
    'aws iam detach',
    'aws iam put',
    'aws iam update',
    'aws sts assume-role',
    'aws organizations',
    'rm ',
    'sudo ',
    ';',
    '|',
    '&&',
    '`',
    '$(',
]


def lambda_handler(event, context):
    """主入口"""
    path = event.get('rawPath', '/')
    
    if path.endswith('/webhook'):
        # Telegram webhook callback
        return handle_telegram_webhook(event)
    else:
        # Clawdbot 請求
        return handle_clawdbot_request(event)


def handle_clawdbot_request(event):
    """處理 Clawdbot 的執行請求"""
    # 驗證 secret
    headers = event.get('headers', {})
    if headers.get('x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})
    
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        return response(400, {'error': 'Invalid JSON'})
    
    command = body.get('command', '').strip()
    reason = body.get('reason', 'No reason provided')
    
    if not command:
        return response(400, {'error': 'Missing command'})
    
    # 檢查黑名單
    if is_blocked(command):
        return response(403, {'error': 'Command blocked for security', 'command': command})
    
    # 檢查是否自動批准
    if is_auto_approve(command):
        result = execute_command(command)
        return response(200, {
            'status': 'auto_approved',
            'command': command,
            'result': result
        })
    
    # 需要人工審批
    request_id = hashlib.sha256(f"{command}{time.time()}".encode()).hexdigest()[:8]
    ttl = int(time.time()) + 300  # 5 分鐘過期
    
    # 存入 DynamoDB
    table.put_item(Item={
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'status': 'pending',
        'created_at': int(time.time()),
        'ttl': ttl
    })
    
    # 發送 Telegram 審批請求
    send_approval_request(request_id, command, reason)
    
    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': '請求已發送，等待 Telegram 確認',
        'expires_in': '5 minutes'
    })


def handle_telegram_webhook(event):
    """處理 Telegram callback"""
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        return response(400, {'error': 'Invalid JSON'})
    
    callback = body.get('callback_query')
    if not callback:
        return response(200, {'ok': True})
    
    # 驗證是授權用戶
    user_id = str(callback.get('from', {}).get('id', ''))
    if user_id != APPROVED_CHAT_ID:
        answer_callback(callback['id'], '❌ 你沒有審批權限')
        return response(403, {'error': 'Unauthorized user'})
    
    data = callback.get('data', '')
    if ':' not in data:
        return response(400, {'error': 'Invalid callback data'})
    
    action, request_id = data.split(':', 1)
    
    # 從 DynamoDB 取得請求
    try:
        item = table.get_item(Key={'request_id': request_id}).get('Item')
    except:
        item = None
    
    if not item:
        answer_callback(callback['id'], '❌ 請求已過期或不存在')
        return response(404, {'error': 'Request not found'})
    
    if item['status'] != 'pending':
        answer_callback(callback['id'], '⚠️ 此請求已處理過')
        return response(200, {'ok': True})
    
    message_id = callback.get('message', {}).get('message_id')
    
    if action == 'approve':
        # 執行命令
        command = item['command']
        result = execute_command(command)
        
        # 更新狀態
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, result = :r',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'approved', ':r': result[:1000]}
        )
        
        # 更新 Telegram 消息
        update_message(message_id, f"✅ 已批准並執行\n\n📋 命令：\n`{command}`\n\n📤 結果：\n```\n{result[:2000]}\n```")
        answer_callback(callback['id'], '✅ 已執行')
        
    elif action == 'deny':
        # 拒絕
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'denied'}
        )
        
        update_message(message_id, f"❌ 已拒絕\n\n📋 命令：\n`{item['command']}`")
        answer_callback(callback['id'], '❌ 已拒絕')
    
    return response(200, {'ok': True})


def is_blocked(command: str) -> bool:
    """檢查命令是否在黑名單"""
    cmd_lower = command.lower()
    return any(blocked in cmd_lower for blocked in BLOCKED_PREFIXES)


def is_auto_approve(command: str) -> bool:
    """檢查命令是否可自動批准"""
    cmd_lower = command.lower()
    return any(cmd_lower.startswith(prefix) for prefix in AUTO_APPROVE_PREFIXES)


def execute_command(command: str) -> str:
    """執行 AWS CLI 命令"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=25
        )
        output = result.stdout or result.stderr or '(no output)'
        return output[:4000]
    except subprocess.TimeoutExpired:
        return '❌ 命令執行超時 (25s)'
    except Exception as e:
        return f'❌ 執行錯誤: {str(e)}'


def send_approval_request(request_id: str, command: str, reason: str):
    """發送 Telegram 審批請求"""
    text = (
        f"🔐 *AWS 執行請求*\n\n"
        f"📋 *命令：*\n`{command}`\n\n"
        f"💬 *原因：* {reason}\n\n"
        f"🆔 *Request ID：* `{request_id}`\n"
        f"⏰ *5 分鐘後過期*"
    )
    
    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准執行', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }
    
    send_telegram_message(text, keyboard)


def send_telegram_message(text: str, reply_markup: dict = None):
    """發送 Telegram 消息"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        method='POST'
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


def update_message(message_id: int, text: str):
    """更新 Telegram 消息"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        method='POST'
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


def answer_callback(callback_id: str, text: str):
    """回應 Telegram callback"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    data = {
        'callback_query_id': callback_id,
        'text': text
    }
    
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        method='POST'
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


def response(status_code: int, body: dict):
    """構造 HTTP response"""
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body, default=str)
    }
