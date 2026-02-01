#!/usr/bin/env python3
"""
Bouncer MCP Client Wrapper

本地 MCP Server，透過 stdio 與 Clawdbot 通訊，
背後呼叫 Lambda API 並輪詢等待審批結果。

使用方式：
    python bouncer_mcp.py

環境變數：
    BOUNCER_API_URL - Bouncer Lambda API URL
    BOUNCER_SECRET - 請求認證 Secret
    BOUNCER_TIMEOUT - 審批等待超時秒數（預設 300）
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# ============================================================================
# 配置
# ============================================================================

API_URL = os.environ.get('BOUNCER_API_URL', 'https://YOUR_API_GATEWAY_URL')
SECRET = os.environ.get('BOUNCER_SECRET', '')
DEFAULT_TIMEOUT = int(os.environ.get('BOUNCER_TIMEOUT', '300'))  # 5 分鐘
POLL_INTERVAL = 2  # 輪詢間隔（秒）

VERSION = '2.0.0'

# ============================================================================
# MCP Tools 定義
# ============================================================================

TOOLS = [
    {
        'name': 'bouncer_execute',
        'description': '執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批後執行。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：aws ec2 describe-instances）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（12 位數字），不填則使用預設帳號'
                },
                'reason': {
                    'type': 'string',
                    'description': '執行原因，會顯示在審批請求中，讓審批者了解用途'
                },
                'source': {
                    'type': 'string',
                    'description': '來源標識（例如：Clawdbot、Steven 的 OpenClaw）'
                },
                'timeout': {
                    'type': 'integer',
                    'description': f'審批等待超時秒數，預設 {DEFAULT_TIMEOUT}'
                }
            },
            'required': ['command', 'reason']
        }
    },
    {
        'name': 'bouncer_status',
        'description': '查詢審批請求狀態',
        'inputSchema': {
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
    {
        'name': 'bouncer_add_account',
        'description': '新增或更新 AWS 帳號配置（需要 Telegram 審批）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（12 位數字）'
                },
                'name': {
                    'type': 'string',
                    'description': '帳號名稱（例如：Production, Staging）'
                },
                'role_arn': {
                    'type': 'string',
                    'description': 'IAM Role ARN（例如：arn:aws:iam::111111111111:role/BouncerRole）'
                },
                'source': {
                    'type': 'string',
                    'description': '來源標識'
                }
            },
            'required': ['account_id', 'name', 'role_arn']
        }
    },
    {
        'name': 'bouncer_list_accounts',
        'description': '列出已配置的 AWS 帳號',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_remove_account',
        'description': '移除 AWS 帳號配置（需要 Telegram 審批）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（12 位數字）'
                },
                'source': {
                    'type': 'string',
                    'description': '來源標識'
                }
            },
            'required': ['account_id']
        }
    }
]

# ============================================================================
# HTTP 請求
# ============================================================================

def http_request(method: str, path: str, data: dict = None) -> dict:
    """發送 HTTP 請求到 Bouncer API"""
    url = f"{API_URL.rstrip('/')}{path}"
    
    headers = {
        'Content-Type': 'application/json',
        'X-Approval-Secret': SECRET
    }
    
    body = json.dumps(data).encode() if data else None
    
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        try:
            return json.loads(error_body)
        except:
            return {'error': error_body, 'status_code': e.code}
    except Exception as e:
        return {'error': str(e)}

# ============================================================================
# Tool 實作
# ============================================================================

def tool_execute(arguments: dict) -> dict:
    """執行 AWS 命令，等待審批"""
    command = arguments.get('command', '').strip()
    reason = arguments.get('reason', 'No reason provided')
    source = arguments.get('source', 'OpenClaw Agent')
    account = arguments.get('account', None)  # 目標帳號 ID
    timeout = arguments.get('timeout', DEFAULT_TIMEOUT)
    
    if not command:
        return {'error': 'Missing required parameter: command'}
    
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}
    
    # 1. 提交請求
    payload = {
        'command': command,
        'reason': reason,
        'source': source
    }
    if account:
        payload['account'] = account
    
    result = http_request('POST', '/', payload)
    
    # 如果是自動批准或被阻擋，直接返回
    status = result.get('status', '')
    if status in ('auto_approved', 'blocked'):
        return result
    
    # 如果不是 pending，可能是錯誤
    if status != 'pending_approval':
        return result
    
    # 2. 輪詢等待結果
    request_id = result.get('request_id')
    if not request_id:
        return {'error': 'No request_id returned', 'response': result}
    
    start_time = time.time()
    log(f"Waiting for approval: {request_id} (timeout: {timeout}s)")
    
    while (time.time() - start_time) < timeout:
        time.sleep(POLL_INTERVAL)
        
        status_result = http_request('GET', f'/status/{request_id}')
        current_status = status_result.get('status', '')
        
        if current_status == 'approved':
            return {
                'status': 'approved',
                'request_id': request_id,
                'command': command,
                'result': status_result.get('result', ''),
                'approved_by': status_result.get('approver'),
                'waited_seconds': int(time.time() - start_time)
            }
        
        elif current_status == 'denied':
            return {
                'status': 'denied',
                'request_id': request_id,
                'command': command,
                'denied_by': status_result.get('approver'),
                'waited_seconds': int(time.time() - start_time)
            }
        
        elif current_status == 'timeout':
            return {
                'status': 'timeout',
                'request_id': request_id,
                'message': 'Request expired on server'
            }
        
        # pending_approval → 繼續等待
    
    # Client 端超時
    return {
        'status': 'timeout',
        'request_id': request_id,
        'message': f'No response after {timeout} seconds',
        'waited_seconds': timeout
    }


def tool_status(arguments: dict) -> dict:
    """查詢請求狀態"""
    request_id = arguments.get('request_id', '')
    
    if not request_id:
        return {'error': 'Missing required parameter: request_id'}
    
    return http_request('GET', f'/status/{request_id}')


def poll_for_result(request_id: str, timeout: int, action: str) -> dict:
    """共用的輪詢邏輯"""
    start_time = time.time()
    log(f"Waiting for approval: {request_id} ({action}, timeout: {timeout}s)")
    
    while (time.time() - start_time) < timeout:
        time.sleep(POLL_INTERVAL)
        
        status_result = http_request('GET', f'/status/{request_id}')
        current_status = status_result.get('status', '')
        
        if current_status == 'approved':
            return {
                'status': 'approved',
                'request_id': request_id,
                'action': action,
                'waited_seconds': int(time.time() - start_time)
            }
        
        elif current_status == 'denied':
            return {
                'status': 'denied',
                'request_id': request_id,
                'action': action,
                'waited_seconds': int(time.time() - start_time)
            }
        
        elif current_status == 'timeout':
            return {
                'status': 'timeout',
                'request_id': request_id,
                'message': 'Request expired on server'
            }
        
        # pending_approval → 繼續等待
    
    return {
        'status': 'timeout',
        'request_id': request_id,
        'message': f'No response after {timeout} seconds',
        'waited_seconds': timeout
    }


def tool_add_account(arguments: dict) -> dict:
    """新增帳號（使用 async 模式 + 本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}
    
    timeout = arguments.get('timeout', DEFAULT_TIMEOUT)
    
    # 加上 async=true 讓 Lambda 立即返回
    args_with_async = dict(arguments)
    args_with_async['async'] = True
    
    payload = {
        'jsonrpc': '2.0',
        'id': 'add-account',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_add_account',
            'arguments': args_with_async
        }
    }
    
    result = http_request('POST', '/mcp', payload)
    
    # 解析 MCP 回應
    inner_result = None
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                inner_result = json.loads(content[0]['text'])
            except:
                return result
    
    if not inner_result:
        return result
    
    # 如果是 error 或其他終態，直接返回
    status = inner_result.get('status', '')
    if status != 'pending_approval':
        return inner_result
    
    # 開始本地輪詢
    request_id = inner_result.get('request_id')
    if not request_id:
        return {'error': 'No request_id returned', 'response': inner_result}
    
    return poll_for_result(request_id, timeout, 'add_account')


def tool_list_accounts(arguments: dict) -> dict:
    """列出帳號（走 MCP 端點）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}
    
    payload = {
        'jsonrpc': '2.0',
        'id': 'list-accounts',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_list_accounts',
            'arguments': arguments
        }
    }
    
    result = http_request('POST', '/mcp', payload)
    
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                return json.loads(content[0]['text'])
            except:
                return content[0]
    return result


def tool_remove_account(arguments: dict) -> dict:
    """移除帳號（使用 async 模式 + 本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}
    
    timeout = arguments.get('timeout', DEFAULT_TIMEOUT)
    
    args_with_async = dict(arguments)
    args_with_async['async'] = True
    
    payload = {
        'jsonrpc': '2.0',
        'id': 'remove-account',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_remove_account',
            'arguments': args_with_async
        }
    }
    
    result = http_request('POST', '/mcp', payload)
    
    inner_result = None
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                inner_result = json.loads(content[0]['text'])
            except:
                return result
    
    if not inner_result:
        return result
    
    status = inner_result.get('status', '')
    if status != 'pending_approval':
        return inner_result
    
    request_id = inner_result.get('request_id')
    if not request_id:
        return {'error': 'No request_id returned', 'response': inner_result}
    
    return poll_for_result(request_id, timeout, 'remove_account')

# ============================================================================
# MCP Server
# ============================================================================

def log(msg: str):
    """寫 log 到 stderr（不影響 stdout 的 JSON-RPC）"""
    print(f"[Bouncer] {msg}", file=sys.stderr)


def handle_request(request: dict) -> dict:
    """處理 JSON-RPC 請求"""
    method = request.get('method', '')
    params = request.get('params', {})
    req_id = request.get('id')
    
    if request.get('jsonrpc') != '2.0':
        return error_response(req_id, -32600, 'Invalid Request')
    
    if method == 'initialize':
        return success_response(req_id, {
            'protocolVersion': '2024-11-05',
            'serverInfo': {'name': 'bouncer-client', 'version': VERSION},
            'capabilities': {'tools': {}}
        })
    
    elif method == 'notifications/initialized':
        return success_response(req_id, {})
    
    elif method == 'tools/list':
        return success_response(req_id, {'tools': TOOLS})
    
    elif method == 'tools/call':
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})
        
        if tool_name == 'bouncer_execute':
            result = tool_execute(arguments)
        elif tool_name == 'bouncer_status':
            result = tool_status(arguments)
        elif tool_name == 'bouncer_add_account':
            result = tool_add_account(arguments)
        elif tool_name == 'bouncer_list_accounts':
            result = tool_list_accounts(arguments)
        elif tool_name == 'bouncer_remove_account':
            result = tool_remove_account(arguments)
        else:
            return error_response(req_id, -32602, f'Unknown tool: {tool_name}')
        
        is_error = 'error' in result or result.get('status') in ('denied', 'timeout', 'blocked')
        
        return success_response(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(result, indent=2, ensure_ascii=False)}],
            'isError': is_error
        })
    
    else:
        return error_response(req_id, -32601, f'Method not found: {method}')


def success_response(req_id, result) -> dict:
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def error_response(req_id, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}


def main():
    log(f"MCP Client Wrapper v{VERSION} started")
    log(f"API: {API_URL}")
    log(f"Secret configured: {'Yes' if SECRET else 'No'}")
    
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        
        try:
            request = json.loads(line)
            response = handle_request(request)
            print(json.dumps(response), flush=True)
        except json.JSONDecodeError as e:
            print(json.dumps(error_response(None, -32700, f'Parse error: {e}')), flush=True)
        except Exception as e:
            log(f"Error: {e}")
            print(json.dumps(error_response(None, -32603, f'Internal error: {e}')), flush=True)


if __name__ == '__main__':
    main()
