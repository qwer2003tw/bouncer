"""
Bouncer - Clawdbot AWS 命令審批執行系統
版本: 1.1.0 (整合三份報告建議)
更新: 2026-01-31
"""

import json
import os
import hashlib
import hmac
import time
import urllib.request
import urllib.parse
import subprocess
import boto3
from decimal import Decimal

# ============================================================================
# 環境變數
# ============================================================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
APPROVED_CHAT_ID = os.environ.get('APPROVED_CHAT_ID', '999999999')
REQUEST_SECRET = os.environ.get('REQUEST_SECRET', '')
TABLE_NAME = os.environ.get('TABLE_NAME', 'clawdbot-approval-requests')
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')

# HMAC 驗證開關（Phase 2 啟用）
ENABLE_HMAC = os.environ.get('ENABLE_HMAC', 'false').lower() == 'true'

# DynamoDB
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

# ============================================================================
# 命令分類系統（四層）
# ============================================================================

# Layer 1: BLOCKED - 永遠拒絕
BLOCKED_PATTERNS = [
    # IAM 危險操作
    'iam create', 'iam delete', 'iam attach', 'iam detach', 
    'iam put', 'iam update', 'iam add', 'iam remove',
    # STS 危險操作
    'sts assume-role',
    # Organizations
    'organizations ',
    # Shell 注入
    ';', '|', '&&', '||', '`', '$(', '${',
    'rm -rf', 'sudo ', '> /dev', 'chmod 777',
    # 其他危險
    'delete-account', 'close-account',
]

# Layer 2: SAFELIST - 自動批准（Read-only）
AUTO_APPROVE_PREFIXES = [
    # EC2
    'aws ec2 describe-',
    # S3 (read-only)
    'aws s3 ls', 'aws s3api list-', 'aws s3api get-',
    # RDS
    'aws rds describe-',
    # Lambda
    'aws lambda list-', 'aws lambda get-',
    # CloudWatch
    'aws logs describe-', 'aws logs get-', 'aws logs filter-log-events',
    'aws cloudwatch describe-', 'aws cloudwatch get-', 'aws cloudwatch list-',
    # IAM (read-only)
    'aws iam list-', 'aws iam get-',
    # STS
    'aws sts get-caller-identity',
    # SSM (read-only)
    'aws ssm describe-', 'aws ssm get-', 'aws ssm list-',
    # Route53 (read-only)
    'aws route53 list-', 'aws route53 get-',
    # ECS/EKS (read-only)
    'aws ecs describe-', 'aws ecs list-',
    'aws eks describe-', 'aws eks list-',
]

# Layer 3: APPROVAL - 需要人工審批（在 SAFELIST 和 BLOCKED 之外的命令）
# Layer 4: DEFAULT DENY - 未知命令拒絕（可選，目前走 APPROVAL）


# ============================================================================
# Lambda Handler
# ============================================================================

def lambda_handler(event, context):
    """主入口 - 路由請求"""
    path = event.get('rawPath', '/')
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    
    # 路由
    if path.endswith('/webhook'):
        return handle_telegram_webhook(event)
    elif '/status/' in path:
        return handle_status_query(event, path)
    elif method == 'POST':
        return handle_clawdbot_request(event)
    else:
        return response(200, {
            'service': 'Bouncer',
            'version': '1.1.0',
            'endpoints': {
                'POST /': 'Submit command for approval',
                'GET /status/{id}': 'Query request status',
                'POST /webhook': 'Telegram callback'
            }
        })


# ============================================================================
# Status Query Endpoint（新增 - Architect 建議）
# ============================================================================

def handle_status_query(event, path):
    """查詢請求狀態 - GET /status/{request_id}"""
    headers = event.get('headers', {})
    
    # 驗證 secret
    if headers.get('x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})
    
    # 提取 request_id
    parts = path.split('/status/')
    if len(parts) < 2:
        return response(400, {'error': 'Missing request_id'})
    
    request_id = parts[1].strip('/')
    if not request_id:
        return response(400, {'error': 'Missing request_id'})
    
    # 查詢 DynamoDB
    try:
        result = table.get_item(Key={'request_id': request_id})
        item = result.get('Item')
        
        if not item:
            return response(404, {'error': 'Request not found', 'request_id': request_id})
        
        # 轉換 Decimal
        return response(200, decimal_to_native(item))
        
    except Exception as e:
        return response(500, {'error': str(e)})


# ============================================================================
# Clawdbot Request Handler
# ============================================================================

def handle_clawdbot_request(event):
    """處理 Clawdbot 的命令執行請求"""
    headers = event.get('headers', {})
    
    # 基本 Secret 驗證
    if headers.get('x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})
    
    # HMAC 驗證（Phase 2，可選啟用）
    if ENABLE_HMAC:
        body_str = event.get('body', '')
        if not verify_hmac(headers, body_str):
            return response(403, {'error': 'Invalid HMAC signature'})
    
    # 解析請求
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        return response(400, {'error': 'Invalid JSON'})
    
    command = body.get('command', '').strip()
    reason = body.get('reason', 'No reason provided')
    wait = body.get('wait', False)  # 長輪詢選項
    
    if not command:
        return response(400, {'error': 'Missing command'})
    
    # ========== 四層命令分類 ==========
    
    # Layer 1: BLOCKED
    if is_blocked(command):
        return response(403, {
            'status': 'blocked',
            'error': 'Command blocked for security',
            'command': command
        })
    
    # Layer 2: SAFELIST (auto-approve)
    if is_auto_approve(command):
        result = execute_command(command)
        return response(200, {
            'status': 'auto_approved',
            'command': command,
            'result': result
        })
    
    # Layer 3: APPROVAL (human review)
    request_id = generate_request_id(command)
    ttl = int(time.time()) + 300  # 5 分鐘過期
    
    # 存入 DynamoDB
    item = {
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'status': 'pending',
        'created_at': int(time.time()),
        'ttl': ttl
    }
    table.put_item(Item=item)
    
    # 發送 Telegram 審批請求
    send_approval_request(request_id, command, reason)
    
    # 長輪詢等待結果（Pragmatic Engineer 建議）
    if wait:
        return wait_for_result(request_id, timeout=50)
    
    # 非等待模式，立即返回
    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': '請求已發送，等待 Telegram 確認',
        'expires_in': '5 minutes',
        'check_status': f'/status/{request_id}'
    })


# ============================================================================
# 長輪詢（Pragmatic Engineer 建議）
# ============================================================================

def wait_for_result(request_id: str, timeout: int = 50) -> dict:
    """輪詢等待審批結果，最多 timeout 秒"""
    interval = 2  # 每 2 秒查一次
    iterations = timeout // interval
    
    for _ in range(iterations):
        time.sleep(interval)
        
        try:
            result = table.get_item(Key={'request_id': request_id})
            item = result.get('Item')
            
            if item and item.get('status') != 'pending':
                return response(200, {
                    'status': item['status'],
                    'request_id': request_id,
                    'command': item.get('command'),
                    'result': item.get('result', ''),
                    'waited': True
                })
        except:
            pass
    
    # 超時，返回 pending
    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': f'等待 {timeout} 秒後仍未審批',
        'check_status': f'/status/{request_id}'
    })


# ============================================================================
# Telegram Webhook Handler
# ============================================================================

def handle_telegram_webhook(event):
    """處理 Telegram callback"""
    headers = event.get('headers', {})
    
    # 驗證 Telegram webhook 簽名（防偽造）
    if TELEGRAM_WEBHOOK_SECRET:
        received_secret = headers.get('x-telegram-bot-api-secret-token', '')
        if received_secret != TELEGRAM_WEBHOOK_SECRET:
            return response(403, {'error': 'Invalid webhook signature'})
    
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
    command = item['command']
    
    if action == 'approve':
        # 執行命令
        result = execute_command(command)
        
        # 更新狀態
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, #r = :r, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status', '#r': 'result'},
            ExpressionAttributeValues={
                ':s': 'approved',
                ':r': result[:3000],  # 限制結果長度
                ':t': int(time.time()),
                ':a': user_id
            }
        )
        
        # 更新 Telegram 消息
        result_preview = result[:1500] if len(result) > 1500 else result
        update_message(
            message_id,
            f"✅ 已批准並執行\n\n"
            f"📋 命令：\n`{command}`\n\n"
            f"📤 結果：\n```\n{result_preview}\n```"
        )
        answer_callback(callback['id'], '✅ 已執行')
        
    elif action == 'deny':
        # 拒絕
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
        
        update_message(message_id, f"❌ 已拒絕\n\n📋 命令：\n`{command}`")
        answer_callback(callback['id'], '❌ 已拒絕')
    
    return response(200, {'ok': True})


# ============================================================================
# 命令分類函數
# ============================================================================

def is_blocked(command: str) -> bool:
    """Layer 1: 檢查命令是否在黑名單"""
    cmd_lower = command.lower()
    return any(pattern in cmd_lower for pattern in BLOCKED_PATTERNS)


def is_auto_approve(command: str) -> bool:
    """Layer 2: 檢查命令是否可自動批准"""
    cmd_lower = command.lower()
    return any(cmd_lower.startswith(prefix) for prefix in AUTO_APPROVE_PREFIXES)


# ============================================================================
# HMAC 驗證（Phase 2 - Security Analyst 建議）
# ============================================================================

def verify_hmac(headers: dict, body: str) -> bool:
    """
    HMAC-SHA256 請求簽章驗證
    
    Headers required:
    - X-Timestamp: Unix timestamp
    - X-Nonce: Random string (防重放)
    - X-Signature: HMAC-SHA256(timestamp.nonce.body)
    """
    timestamp = headers.get('x-timestamp', '')
    nonce = headers.get('x-nonce', '')
    signature = headers.get('x-signature', '')
    
    if not all([timestamp, nonce, signature]):
        return False
    
    # 檢查時間窗口（5 分鐘）
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except:
        return False
    
    # TODO: 檢查 nonce 是否已使用（需要額外存儲）
    
    # 驗證簽章
    payload = f"{timestamp}.{nonce}.{body}"
    expected = hmac.new(
        REQUEST_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected)


# ============================================================================
# 命令執行
# ============================================================================

def execute_command(command: str) -> str:
    """
    執行 AWS CLI 命令
    
    安全設計：
    - 使用 shlex.split() 解析命令（避免 shell injection）
    - 不使用 shell=True
    - 命令必須以 'aws' 開頭
    - 已通過 BLOCKED_PATTERNS 檢查
    """
    import shlex
    
    try:
        # 解析命令為參數列表
        args = shlex.split(command)
        
        # 額外安全檢查：必須是 aws 命令
        if not args or args[0] != 'aws':
            return '❌ 只能執行 aws CLI 命令'
        
        result = subprocess.run(
            args,
            shell=False,  # 安全：不使用 shell
            capture_output=True,
            text=True,
            timeout=25,
            env={**os.environ, 'AWS_PAGER': ''}  # 禁用 pager
        )
        output = result.stdout or result.stderr or '(no output)'
        return output[:4000]
    except subprocess.TimeoutExpired:
        return '❌ 命令執行超時 (25s)'
    except ValueError as e:
        # shlex 解析錯誤（如未閉合的引號）
        return f'❌ 命令格式錯誤: {str(e)}'
    except FileNotFoundError:
        return '❌ aws CLI 未安裝'
    except Exception as e:
        return f'❌ 執行錯誤: {str(e)}'


# ============================================================================
# Telegram API
# ============================================================================

def send_approval_request(request_id: str, command: str, reason: str):
    """發送 Telegram 審批請求"""
    # 命令預覽（過長截斷）
    cmd_preview = command if len(command) <= 500 else command[:500] + '...'
    
    text = (
        f"🔐 *AWS 執行請求*\n\n"
        f"📋 *命令：*\n`{cmd_preview}`\n\n"
        f"💬 *原因：* {reason}\n\n"
        f"🆔 *ID：* `{request_id}`\n"
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
    if not TELEGRAM_TOKEN:
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    
    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Telegram send error: {e}")


def update_message(message_id: int, text: str):
    """更新 Telegram 消息"""
    if not TELEGRAM_TOKEN:
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Telegram update error: {e}")


def answer_callback(callback_id: str, text: str):
    """回應 Telegram callback"""
    if not TELEGRAM_TOKEN:
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    data = {
        'callback_query_id': callback_id,
        'text': text
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


# ============================================================================
# Utilities
# ============================================================================

def generate_request_id(command: str) -> str:
    """產生唯一請求 ID"""
    data = f"{command}{time.time()}{os.urandom(8).hex()}"
    return hashlib.sha256(data.encode()).hexdigest()[:12]


def decimal_to_native(obj):
    """轉換 DynamoDB Decimal 為 Python native types"""
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    elif isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def response(status_code: int, body: dict) -> dict:
    """構造 HTTP response"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'X-Bouncer-Version': '1.1.0'
        },
        'body': json.dumps(body, default=str)
    }
