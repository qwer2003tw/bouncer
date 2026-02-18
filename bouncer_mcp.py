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
        'description': '執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。預設異步返回 request_id，用 bouncer_status 查詢結果。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：aws ec2 describe-instances）'
                },
                'reason': {
                    'type': 'string',
                    'description': '執行原因，會顯示在審批請求中，讓審批者了解用途'
                },
                'source': {
                    'type': 'string',
                    'description': '來源標識（例如：Clawdbot、Steven 的 OpenClaw）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（12 位數字），不填則使用預設帳號'
                },
                'sync': {
                    'type': 'boolean',
                    'description': '同步模式：等待審批結果（可能超時），預設 false'
                }
            },
            'required': ['command', 'reason']
        }
    },
    {
        'name': 'bouncer_status',
        'description': '查詢審批請求狀態（用於異步模式輪詢結果）',
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
    },
    # ========== Deployer Tools ==========
    {
        'name': 'bouncer_deploy',
        'description': '部署 SAM 專案（需要 Telegram 審批）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project': {
                    'type': 'string',
                    'description': '專案 ID（例如：bouncer）'
                },
                'branch': {
                    'type': 'string',
                    'description': 'Git 分支（預設使用專案設定的分支）'
                },
                'reason': {
                    'type': 'string',
                    'description': '部署原因'
                }
            },
            'required': ['project', 'reason']
        }
    },
    {
        'name': 'bouncer_deploy_status',
        'description': '查詢部署狀態',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'deploy_id': {
                    'type': 'string',
                    'description': '部署 ID'
                }
            },
            'required': ['deploy_id']
        }
    },
    {
        'name': 'bouncer_deploy_cancel',
        'description': '取消進行中的部署',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'deploy_id': {
                    'type': 'string',
                    'description': '部署 ID'
                }
            },
            'required': ['deploy_id']
        }
    },
    {
        'name': 'bouncer_deploy_history',
        'description': '查詢專案部署歷史',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project': {
                    'type': 'string',
                    'description': '專案 ID'
                },
                'limit': {
                    'type': 'integer',
                    'description': '返回筆數（預設 10）'
                }
            },
            'required': ['project']
        }
    },
    {
        'name': 'bouncer_project_list',
        'description': '列出可部署的專案',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_list_safelist',
        'description': '列出命令分類規則：哪些命令會自動執行（safelist）、哪些會被封鎖（blocked）',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_get_page',
        'description': '取得長輸出的下一頁（當結果有 paged=true 時使用）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'page_id': {
                    'type': 'string',
                    'description': '分頁 ID（從 next_page 欄位取得）'
                }
            },
            'required': ['page_id']
        }
    },
    {
        'name': 'bouncer_list_pending',
        'description': '列出待審批的請求',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'source': {
                    'type': 'string',
                    'description': '來源標識（不填則列出所有）'
                },
                'limit': {
                    'type': 'integer',
                    'description': '最大數量（預設 20）'
                }
            }
        }
    },
    {
        'name': 'bouncer_trust_status',
        'description': '查詢當前的信任時段狀態',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'source': {
                    'type': 'string',
                    'description': '來源標識（不填則查詢所有活躍時段）'
                }
            }
        }
    },
    {
        'name': 'bouncer_trust_revoke',
        'description': '撤銷信任時段',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'trust_id': {
                    'type': 'string',
                    'description': '信任時段 ID'
                }
            },
            'required': ['trust_id']
        }
    },
    {
        'name': 'bouncer_upload',
        'description': '上傳檔案到固定 S3 桶（需要 Telegram 審批）。預設異步返回 request_id，用 bouncer_status 查詢結果。檔案大小限制 4.5 MB。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'filename': {
                    'type': 'string',
                    'description': '檔案名稱（例如 template.yaml）'
                },
                'content': {
                    'type': 'string',
                    'description': '檔案內容（base64 encoded）'
                },
                'content_type': {
                    'type': 'string',
                    'description': 'Content-Type（預設 application/octet-stream）'
                },
                'reason': {
                    'type': 'string',
                    'description': '上傳原因'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識'
                },
                'sync': {
                    'type': 'boolean',
                    'description': '同步模式：等待審批結果（可能超時），預設 false'
                }
            },
            'required': ['filename', 'content', 'reason', 'source']
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
    """執行 AWS 命令，等待審批（立即返回，不做本地輪詢）"""
    command = str(arguments.get('command', '')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', 'OpenClaw Agent')
    account = arguments.get('account', None)
    if account:
        account = str(account).strip()
    timeout = int(arguments.get('timeout', DEFAULT_TIMEOUT))

    if not command:
        return {'error': 'Missing required parameter: command'}

    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    # Lambda 預設異步返回
    mcp_args = {
        'command': command,
        'reason': reason,
        'source': source,
        'timeout': timeout,
        'async': True  # 關鍵：讓 Lambda 不等待，立即返回
    }
    if account:
        mcp_args['account'] = account

    payload = {
        'jsonrpc': '2.0',
        'id': 'execute',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_execute',
            'arguments': mcp_args
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
            except Exception:
                return result

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢，避免 block stdio）
    # Agent 需要自己用 bouncer_status 輪詢
    return inner_result


def tool_status(arguments: dict) -> dict:
    """查詢請求狀態"""
    request_id = arguments.get('request_id', '')

    if not request_id:
        return {'error': 'Missing required parameter: request_id'}

    return http_request('GET', f'/status/{request_id}')



def tool_add_account(arguments: dict) -> dict:
    """新增帳號（立即返回，不做本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'add-account',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_add_account',
            'arguments': arguments
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
            except Exception:
                return result

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢）
    return inner_result


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
    """移除帳號（立即返回，不做本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'remove-account',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_remove_account',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)

    inner_result = None
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                inner_result = json.loads(content[0]['text'])
            except Exception:
                return result

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢）
    return inner_result


# ============================================================================
# Deployer Tools
# ============================================================================

def tool_deploy(arguments: dict) -> dict:
    """部署專案（立即返回，不做本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    inner_result = parse_mcp_result(result)

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢）
    return inner_result


def tool_deploy_status(arguments: dict) -> dict:
    """查詢部署狀態"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy-status',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy_status',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_deploy_cancel(arguments: dict) -> dict:
    """取消部署"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy-cancel',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy_cancel',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_deploy_history(arguments: dict) -> dict:
    """查詢部署歷史"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy-history',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy_history',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_project_list(arguments: dict) -> dict:
    """列出專案"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'project-list',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_project_list',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_list_safelist(arguments: dict) -> dict:
    """列出 safelist 和 blocked patterns"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'list-safelist',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_list_safelist',
            'arguments': {}
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_get_page(arguments: dict) -> dict:
    """取得長輸出的下一頁"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_get_page',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_list_pending(arguments: dict) -> dict:
    """列出待審批的請求"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'list-pending',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_list_pending',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_trust_status(arguments: dict) -> dict:
    """查詢信任時段狀態"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'trust-status',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_trust_status',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_trust_revoke(arguments: dict) -> dict:
    """撤銷信任時段"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'trust-revoke',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_trust_revoke',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_upload(arguments: dict) -> dict:
    """上傳檔案到 S3"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'upload',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_upload',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def parse_mcp_result(result: dict) -> dict:
    """解析 MCP 回應"""
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                return json.loads(content[0]['text'])
            except:
                pass
    return None

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
        # Deployer tools
        elif tool_name == 'bouncer_deploy':
            result = tool_deploy(arguments)
        elif tool_name == 'bouncer_deploy_status':
            result = tool_deploy_status(arguments)
        elif tool_name == 'bouncer_deploy_cancel':
            result = tool_deploy_cancel(arguments)
        elif tool_name == 'bouncer_deploy_history':
            result = tool_deploy_history(arguments)
        elif tool_name == 'bouncer_project_list':
            result = tool_project_list(arguments)
        elif tool_name == 'bouncer_list_safelist':
            result = tool_list_safelist(arguments)
        elif tool_name == 'bouncer_get_page':
            result = tool_get_page(arguments)
        elif tool_name == 'bouncer_list_pending':
            result = tool_list_pending(arguments)
        elif tool_name == 'bouncer_trust_status':
            result = tool_trust_status(arguments)
        elif tool_name == 'bouncer_trust_revoke':
            result = tool_trust_revoke(arguments)
        elif tool_name == 'bouncer_upload':
            result = tool_upload(arguments)
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
