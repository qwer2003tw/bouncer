"""
Bouncer - Clawdbot AWS 命令審批執行系統
版本: 2.0.0 (MCP 支援)
更新: 2026-01-31

支援兩種模式：
1. REST API（向後兼容）
2. MCP JSON-RPC（新增）
"""

import json
import os
import hashlib
import hmac
import time
import urllib.request
import urllib.parse
import subprocess
import shlex
import boto3
from decimal import Decimal
from typing import Optional, Dict, Any

# ============================================================================
# 版本
# ============================================================================
VERSION = '2.0.0'

# ============================================================================
# 環境變數
# ============================================================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
APPROVED_CHAT_ID = os.environ.get('APPROVED_CHAT_ID', '999999999')
REQUEST_SECRET = os.environ.get('REQUEST_SECRET', '')
TABLE_NAME = os.environ.get('TABLE_NAME', 'clawdbot-approval-requests')
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')

# HMAC 驗證開關
ENABLE_HMAC = os.environ.get('ENABLE_HMAC', 'false').lower() == 'true'

# MCP 模式的最大等待時間（秒）- Lambda 最長 15 分鐘，保留 1 分鐘餘量
MCP_MAX_WAIT = int(os.environ.get('MCP_MAX_WAIT', '840'))  # 14 分鐘

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


# ============================================================================
# MCP Tool 定義
# ============================================================================

MCP_TOOLS = {
    'bouncer_execute': {
        'description': '執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。',
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：aws ec2 describe-instances）'
                },
                'reason': {
                    'type': 'string',
                    'description': '執行原因（用於審批記錄）',
                    'default': 'No reason provided'
                },
                'timeout': {
                    'type': 'integer',
                    'description': '最大等待時間（秒），預設 840（14分鐘）',
                    'default': 840,
                    'maximum': 840
                }
            },
            'required': ['command']
        }
    },
    'bouncer_status': {
        'description': '查詢請求狀態',
        'parameters': {
            'type': 'object',
            'properties': {
                'request_id': {
                    'type': 'string',
                    'description': '請求 ID'
                }
            },
            'required': ['request_id']
        }
    },
    'bouncer_list_safelist': {
        'description': '列出自動批准的命令前綴',
        'parameters': {
            'type': 'object',
            'properties': {}
        }
    }
}


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
    elif path.endswith('/mcp'):
        return handle_mcp_request(event)
    elif '/status/' in path:
        return handle_status_query(event, path)
    elif method == 'POST':
        return handle_clawdbot_request(event)
    else:
        return response(200, {
            'service': 'Bouncer',
            'version': VERSION,
            'endpoints': {
                'POST /': 'Submit command for approval (REST)',
                'POST /mcp': 'MCP JSON-RPC endpoint',
                'GET /status/{id}': 'Query request status',
                'POST /webhook': 'Telegram callback'
            },
            'mcp_tools': list(MCP_TOOLS.keys())
        })


# ============================================================================
# MCP JSON-RPC Handler
# ============================================================================

def handle_mcp_request(event) -> dict:
    """處理 MCP JSON-RPC 請求"""
    headers = event.get('headers', {})
    
    # 驗證 secret
    if headers.get('x-approval-secret') != REQUEST_SECRET:
        return mcp_error(None, -32600, 'Invalid secret')
    
    # 解析 JSON-RPC
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        return mcp_error(None, -32700, 'Parse error')
    
    jsonrpc = body.get('jsonrpc')
    method = body.get('method', '')
    params = body.get('params', {})
    req_id = body.get('id')
    
    if jsonrpc != '2.0':
        return mcp_error(req_id, -32600, 'Invalid Request: jsonrpc must be "2.0"')
    
    # 處理 MCP 標準方法
    if method == 'initialize':
        return mcp_result(req_id, {
            'protocolVersion': '2024-11-05',
            'serverInfo': {
                'name': 'bouncer',
                'version': VERSION
            },
            'capabilities': {
                'tools': {}
            }
        })
    
    elif method == 'tools/list':
        tools = []
        for name, spec in MCP_TOOLS.items():
            tools.append({
                'name': name,
                'description': spec['description'],
                'inputSchema': spec['parameters']
            })
        return mcp_result(req_id, {'tools': tools})
    
    elif method == 'tools/call':
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})
        return handle_mcp_tool_call(req_id, tool_name, arguments)
    
    else:
        return mcp_error(req_id, -32601, f'Method not found: {method}')


def handle_mcp_tool_call(req_id, tool_name: str, arguments: dict) -> dict:
    """處理 MCP tool 呼叫"""
    
    if tool_name == 'bouncer_execute':
        return mcp_tool_execute(req_id, arguments)
    
    elif tool_name == 'bouncer_status':
        return mcp_tool_status(req_id, arguments)
    
    elif tool_name == 'bouncer_list_safelist':
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'safelist_prefixes': AUTO_APPROVE_PREFIXES,
                    'blocked_patterns': BLOCKED_PATTERNS
                }, indent=2)
            }]
        })
    
    else:
        return mcp_error(req_id, -32602, f'Unknown tool: {tool_name}')


def mcp_tool_execute(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_execute"""
    command = arguments.get('command', '').strip()
    reason = arguments.get('reason', 'No reason provided')
    timeout = min(arguments.get('timeout', MCP_MAX_WAIT), MCP_MAX_WAIT)
    
    if not command:
        return mcp_error(req_id, -32602, 'Missing required parameter: command')
    
    # Layer 1: BLOCKED
    if is_blocked(command):
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'blocked',
                    'error': 'Command blocked for security',
                    'command': command
                })
            }],
            'isError': True
        })
    
    # Layer 2: SAFELIST (auto-approve)
    if is_auto_approve(command):
        result = execute_command(command)
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'auto_approved',
                    'command': command,
                    'result': result
                })
            }]
        })
    
    # Layer 3: APPROVAL (human review)
    request_id = generate_request_id(command)
    ttl = int(time.time()) + timeout + 60  # 過期時間 = timeout + buffer
    
    # 存入 DynamoDB
    item = {
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    table.put_item(Item=item)
    
    # 發送 Telegram 審批請求
    send_approval_request(request_id, command, reason, timeout)
    
    # 長輪詢等待結果
    result = wait_for_result_mcp(request_id, timeout=timeout)
    
    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps(result)
        }],
        'isError': result.get('status') in ['denied', 'timeout', 'error']
    })


def mcp_tool_status(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_status"""
    request_id = arguments.get('request_id', '')
    
    if not request_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: request_id')
    
    try:
        result = table.get_item(Key={'request_id': request_id})
        item = result.get('Item')
        
        if not item:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'error': 'Request not found',
                        'request_id': request_id
                    })
                }],
                'isError': True
            })
        
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(decimal_to_native(item))
            }]
        })
        
    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')


def wait_for_result_mcp(request_id: str, timeout: int = 840) -> dict:
    """MCP 模式的長輪詢，最多 timeout 秒"""
    interval = 2  # 每 2 秒查一次
    start_time = time.time()
    
    while (time.time() - start_time) < timeout:
        time.sleep(interval)
        
        try:
            result = table.get_item(Key={'request_id': request_id})
            item = result.get('Item')
            
            if item:
                status = item.get('status', '')
                if status == 'approved':
                    return {
                        'status': 'approved',
                        'request_id': request_id,
                        'command': item.get('command'),
                        'result': item.get('result', ''),
                        'approved_by': item.get('approver', 'unknown'),
                        'waited_seconds': int(time.time() - start_time)
                    }
                elif status == 'denied':
                    return {
                        'status': 'denied',
                        'request_id': request_id,
                        'command': item.get('command'),
                        'denied_by': item.get('approver', 'unknown'),
                        'waited_seconds': int(time.time() - start_time)
                    }
                # status == 'pending_approval' → 繼續等待
        except Exception as e:
            # 網路或 DynamoDB 錯誤，繼續嘗試
            print(f"Polling error: {e}")
            pass
    
    # 超時
    return {
        'status': 'timeout',
        'request_id': request_id,
        'message': f'等待 {timeout} 秒後仍未審批',
        'waited_seconds': timeout
    }


def mcp_result(req_id, result: dict) -> dict:
    """構造 MCP JSON-RPC 成功回應"""
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json',
            'X-Bouncer-Version': VERSION
        },
        'body': json.dumps({
            'jsonrpc': '2.0',
            'id': req_id,
            'result': result
        }, default=str)
    }


def mcp_error(req_id, code: int, message: str) -> dict:
    """構造 MCP JSON-RPC 錯誤回應"""
    return {
        'statusCode': 200,  # JSON-RPC 錯誤仍返回 200
        'headers': {
            'Content-Type': 'application/json',
            'X-Bouncer-Version': VERSION
        },
        'body': json.dumps({
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {
                'code': code,
                'message': message
            }
        })
    }


# ============================================================================
# REST API Handlers（向後兼容）
# ============================================================================

def handle_status_query(event, path):
    """查詢請求狀態 - GET /status/{request_id}"""
    headers = event.get('headers', {})
    
    if headers.get('x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})
    
    parts = path.split('/status/')
    if len(parts) < 2:
        return response(400, {'error': 'Missing request_id'})
    
    request_id = parts[1].strip('/')
    if not request_id:
        return response(400, {'error': 'Missing request_id'})
    
    try:
        result = table.get_item(Key={'request_id': request_id})
        item = result.get('Item')
        
        if not item:
            return response(404, {'error': 'Request not found', 'request_id': request_id})
        
        return response(200, decimal_to_native(item))
        
    except Exception as e:
        return response(500, {'error': str(e)})


def handle_clawdbot_request(event):
    """處理 REST API 的命令執行請求（向後兼容）"""
    headers = event.get('headers', {})
    
    if headers.get('x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})
    
    if ENABLE_HMAC:
        body_str = event.get('body', '')
        if not verify_hmac(headers, body_str):
            return response(403, {'error': 'Invalid HMAC signature'})
    
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        return response(400, {'error': 'Invalid JSON'})
    
    command = body.get('command', '').strip()
    reason = body.get('reason', 'No reason provided')
    wait = body.get('wait', False)
    timeout = min(body.get('timeout', 50), MCP_MAX_WAIT)
    
    if not command:
        return response(400, {'error': 'Missing command'})
    
    # Layer 1: BLOCKED
    if is_blocked(command):
        return response(403, {
            'status': 'blocked',
            'error': 'Command blocked for security',
            'command': command
        })
    
    # Layer 2: SAFELIST
    if is_auto_approve(command):
        result = execute_command(command)
        return response(200, {
            'status': 'auto_approved',
            'command': command,
            'result': result
        })
    
    # Layer 3: APPROVAL
    request_id = generate_request_id(command)
    ttl = int(time.time()) + timeout + 60
    
    item = {
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'rest'
    }
    table.put_item(Item=item)
    
    send_approval_request(request_id, command, reason, timeout)
    
    if wait:
        return wait_for_result_rest(request_id, timeout=timeout)
    
    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': '請求已發送，等待 Telegram 確認',
        'expires_in': f'{timeout} seconds',
        'check_status': f'/status/{request_id}'
    })


def wait_for_result_rest(request_id: str, timeout: int = 50) -> dict:
    """REST API 的輪詢等待"""
    interval = 2
    start_time = time.time()
    
    while (time.time() - start_time) < timeout:
        time.sleep(interval)
        
        try:
            result = table.get_item(Key={'request_id': request_id})
            item = result.get('Item')
            
            if item and item.get('status') not in ['pending_approval', 'pending']:
                return response(200, {
                    'status': item['status'],
                    'request_id': request_id,
                    'command': item.get('command'),
                    'result': item.get('result', ''),
                    'waited': True
                })
        except:
            pass
    
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
    
    user_id = str(callback.get('from', {}).get('id', ''))
    if user_id != APPROVED_CHAT_ID:
        answer_callback(callback['id'], '❌ 你沒有審批權限')
        return response(403, {'error': 'Unauthorized user'})
    
    data = callback.get('data', '')
    if ':' not in data:
        return response(400, {'error': 'Invalid callback data'})
    
    action, request_id = data.split(':', 1)
    
    try:
        item = table.get_item(Key={'request_id': request_id}).get('Item')
    except:
        item = None
    
    if not item:
        answer_callback(callback['id'], '❌ 請求已過期或不存在')
        return response(404, {'error': 'Request not found'})
    
    if item['status'] not in ['pending_approval', 'pending']:
        answer_callback(callback['id'], '⚠️ 此請求已處理過')
        return response(200, {'ok': True})
    
    message_id = callback.get('message', {}).get('message_id')
    command = item['command']
    
    if action == 'approve':
        result = execute_command(command)
        
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s, #r = :r, approved_at = :t, approver = :a',
            ExpressionAttributeNames={'#s': 'status', '#r': 'result'},
            ExpressionAttributeValues={
                ':s': 'approved',
                ':r': result[:3000],
                ':t': int(time.time()),
                ':a': user_id
            }
        )
        
        result_preview = result[:1500] if len(result) > 1500 else result
        update_message(
            message_id,
            f"✅ 已批准並執行\n\n"
            f"📋 命令：\n`{command}`\n\n"
            f"📤 結果：\n```\n{result_preview}\n```"
        )
        answer_callback(callback['id'], '✅ 已執行')
        
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
# HMAC 驗證
# ============================================================================

def verify_hmac(headers: dict, body: str) -> bool:
    """HMAC-SHA256 請求簽章驗證"""
    timestamp = headers.get('x-timestamp', '')
    nonce = headers.get('x-nonce', '')
    signature = headers.get('x-signature', '')
    
    if not all([timestamp, nonce, signature]):
        return False
    
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except:
        return False
    
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
    """執行 AWS CLI 命令"""
    try:
        args = shlex.split(command)
        
        if not args or args[0] != 'aws':
            return '❌ 只能執行 aws CLI 命令'
        
        result = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=25,
            env={**os.environ, 'AWS_PAGER': ''}
        )
        output = result.stdout or result.stderr or '(no output)'
        return output[:4000]
    except subprocess.TimeoutExpired:
        return '❌ 命令執行超時 (25s)'
    except ValueError as e:
        return f'❌ 命令格式錯誤: {str(e)}'
    except FileNotFoundError:
        return '❌ aws CLI 未安裝'
    except Exception as e:
        return f'❌ 執行錯誤: {str(e)}'


# ============================================================================
# Telegram API
# ============================================================================

def send_approval_request(request_id: str, command: str, reason: str, timeout: int = 840):
    """發送 Telegram 審批請求"""
    cmd_preview = command if len(command) <= 500 else command[:500] + '...'
    timeout_min = timeout // 60
    
    text = (
        f"🔐 *AWS 執行請求*\n\n"
        f"📋 *命令：*\n`{cmd_preview}`\n\n"
        f"💬 *原因：* {reason}\n\n"
        f"🆔 *ID：* `{request_id}`\n"
        f"⏰ *{timeout_min} 分鐘後過期*"
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
            'X-Bouncer-Version': VERSION
        },
        'body': json.dumps(body, default=str)
    }
