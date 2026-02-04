"""
Bouncer - Clawdbot AWS 命令審批執行系統
版本: 3.0.0 (MCP 支援)
更新: 2026-02-03

支援兩種模式：
1. REST API（向後兼容）
2. MCP JSON-RPC（新增）
"""

import json
import os
import hashlib
import hmac
import time
import boto3
from decimal import Decimal
from typing import Optional

# 從模組導入（逐步遷移中）
try:
    from telegram import (
        escape_markdown, send_telegram_message, send_telegram_message_silent,
        send_telegram_message_to, update_message, answer_callback, update_and_answer,
        _telegram_request,
    )
    from paging import store_paged_output, get_paged_output, send_remaining_pages
    from trust import (
        create_trust_session, revoke_trust_session,
        increment_trust_command_count, is_trust_excluded, should_trust_approve,  # noqa: F401
    )
    from commands import is_blocked, is_dangerous, is_auto_approve, execute_command, fix_json_args  # noqa: F401
    from accounts import (
        init_bot_commands, init_default_account, get_account, list_accounts,
        validate_account_id, validate_role_arn,
    )
except ImportError:
    from src.telegram import (
        escape_markdown, send_telegram_message, send_telegram_message_silent,
        send_telegram_message_to, update_message, answer_callback, update_and_answer,
        _telegram_request,
    )
    from src.paging import store_paged_output, get_paged_output, send_remaining_pages
    from src.trust import (
        create_trust_session, revoke_trust_session,
        increment_trust_command_count, should_trust_approve,
    )
    from src.commands import is_blocked, is_dangerous, is_auto_approve, execute_command
    from src.accounts import (
        init_bot_commands, init_default_account, get_account, list_accounts,
        validate_account_id, validate_role_arn,
    )

# 從 constants.py 導入所有常數
try:
    # Lambda 環境
    from constants import (
        VERSION,
        TELEGRAM_TOKEN, TELEGRAM_WEBHOOK_SECRET,  # noqa: F401
        APPROVED_CHAT_IDS,
        TABLE_NAME, ACCOUNTS_TABLE_NAME,
        DEFAULT_ACCOUNT_ID,
        REQUEST_SECRET, ENABLE_HMAC,
        MCP_MAX_WAIT,
        RATE_LIMIT_WINDOW, RATE_LIMIT_MAX_REQUESTS, MAX_PENDING_PER_SOURCE, RATE_LIMIT_ENABLED,
        TRUST_SESSION_MAX_COMMANDS,
        BLOCKED_PATTERNS, AUTO_APPROVE_PREFIXES,
    )
except ImportError:
    # 本地測試環境
    from src.constants import (
        VERSION,
        TELEGRAM_WEBHOOK_SECRET,
        APPROVED_CHAT_IDS,
        TABLE_NAME, ACCOUNTS_TABLE_NAME,
        DEFAULT_ACCOUNT_ID,
        REQUEST_SECRET, ENABLE_HMAC,
        MCP_MAX_WAIT,
        RATE_LIMIT_WINDOW, RATE_LIMIT_MAX_REQUESTS, MAX_PENDING_PER_SOURCE, RATE_LIMIT_ENABLED,
        TRUST_SESSION_MAX_COMMANDS,
        BLOCKED_PATTERNS, AUTO_APPROVE_PREFIXES,
    )


def get_header(headers: dict, key: str) -> Optional[str]:
    """Case-insensitive header lookup for API Gateway compatibility"""
    # Try exact match first
    if key in headers:
        return headers[key]
    # Try lowercase
    lower_key = key.lower()
    if lower_key in headers:
        return headers[lower_key]
    # Try case-insensitive search
    for k, v in headers.items():
        if k.lower() == lower_key:
            return v
    return None


# DynamoDB
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)
accounts_table = dynamodb.Table(ACCOUNTS_TABLE_NAME)

# ============================================================================
# Rate Limiting
# ============================================================================

class RateLimitExceeded(Exception):
    """Rate limit 超出例外"""
    pass

class PendingLimitExceeded(Exception):
    """Pending limit 超出例外"""
    pass

def check_rate_limit(source: str) -> None:
    """
    檢查 source 的請求頻率

    Args:
        source: 請求來源識別

    Raises:
        RateLimitExceeded: 如果超出頻率限制
        PendingLimitExceeded: 如果 pending 請求過多
    """
    if not RATE_LIMIT_ENABLED:
        return

    if not source:
        source = "__anonymous__"

    now = int(time.time())
    window_start = now - RATE_LIMIT_WINDOW

    try:
        # 查詢此 source 在時間視窗內的審批請求數
        response = table.query(
            IndexName='source-created-index',
            KeyConditionExpression='#src = :source AND created_at >= :window_start',
            FilterExpression='#st IN (:pending, :approved, :denied)',
            ExpressionAttributeNames={
                '#src': 'source',
                '#st': 'status'
            },
            ExpressionAttributeValues={
                ':source': source,
                ':window_start': window_start,
                ':pending': 'pending_approval',
                ':approved': 'approved',
                ':denied': 'denied'
            },
            Select='COUNT'
        )

        recent_count = response.get('Count', 0)

        if recent_count >= RATE_LIMIT_MAX_REQUESTS:
            raise RateLimitExceeded(
                f"Rate limit exceeded: {recent_count}/{RATE_LIMIT_MAX_REQUESTS} "
                f"requests in last {RATE_LIMIT_WINDOW}s"
            )

        # 查詢 pending 請求數
        pending_response = table.query(
            IndexName='source-created-index',
            KeyConditionExpression='#src = :source',
            FilterExpression='#st = :pending',
            ExpressionAttributeNames={
                '#src': 'source',
                '#st': 'status'
            },
            ExpressionAttributeValues={
                ':source': source,
                ':pending': 'pending_approval'
            },
            Select='COUNT'
        )

        pending_count = pending_response.get('Count', 0)

        if pending_count >= MAX_PENDING_PER_SOURCE:
            raise PendingLimitExceeded(
                f"Pending limit exceeded: {pending_count}/{MAX_PENDING_PER_SOURCE} "
                f"pending requests"
            )

    except (RateLimitExceeded, PendingLimitExceeded):
        raise
    except Exception as e:
        # GSI 不存在或其他錯誤，記錄但不阻擋（fail-open）
        print(f"Rate limit check error (allowing): {e}")


# ============================================================================
# MCP Tool 定義
# ============================================================================

MCP_TOOLS = {
    'bouncer_execute': {
        'description': '執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。預設異步返回 request_id，用 bouncer_status 查詢結果。',
        'parameters': {
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
                    'description': '執行原因（用於審批記錄）',
                    'default': 'No reason provided'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識（哪個 agent/系統發的）'
                },
                'sync': {
                    'type': 'boolean',
                    'description': '同步模式：等待審批結果（可能超時），預設 false',
                    'default': False
                }
            },
            'required': ['command']
        }
    },
    'bouncer_status': {
        'description': '查詢請求狀態（用於異步模式輪詢結果）',
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
    },
    'bouncer_trust_status': {
        'description': '查詢當前的信任時段狀態',
        'parameters': {
            'type': 'object',
            'properties': {
                'source': {
                    'type': 'string',
                    'description': '來源標識（不填則查詢所有活躍時段）'
                }
            }
        }
    },
    'bouncer_trust_revoke': {
        'description': '撤銷信任時段',
        'parameters': {
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
    'bouncer_add_account': {
        'description': '新增或更新 AWS 帳號配置（需要 Telegram 審批）',
        'parameters': {
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
                }
            },
            'required': ['account_id', 'name', 'role_arn']
        }
    },
    'bouncer_list_accounts': {
        'description': '列出已配置的 AWS 帳號',
        'parameters': {
            'type': 'object',
            'properties': {}
        }
    },
    'bouncer_get_page': {
        'description': '取得長輸出的下一頁（當結果有 paged=true 時使用）',
        'parameters': {
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
    'bouncer_list_pending': {
        'description': '列出待審批的請求',
        'parameters': {
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
    'bouncer_remove_account': {
        'description': '移除 AWS 帳號配置（需要 Telegram 審批）',
        'parameters': {
            'type': 'object',
            'properties': {
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（12 位數字）'
                }
            },
            'required': ['account_id']
        }
    },
    # ========== Deployer Tools ==========
    'bouncer_deploy': {
        'description': '部署 SAM 專案（需要 Telegram 審批）',
        'parameters': {
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
    'bouncer_deploy_status': {
        'description': '查詢部署狀態',
        'parameters': {
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
    'bouncer_deploy_cancel': {
        'description': '取消進行中的部署',
        'parameters': {
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
    'bouncer_deploy_history': {
        'description': '查詢專案部署歷史',
        'parameters': {
            'type': 'object',
            'properties': {
                'project': {
                    'type': 'string',
                    'description': '專案 ID'
                },
                'limit': {
                    'type': 'integer',
                    'description': '返回筆數（預設 10）',
                    'default': 10
                }
            },
            'required': ['project']
        }
    },
    'bouncer_project_list': {
        'description': '列出可部署的專案',
        'parameters': {
            'type': 'object',
            'properties': {}
        }
    },
    # ========== Upload Tool ==========
    'bouncer_upload': {
        'description': '上傳檔案到固定 S3 桶（需要 Telegram 審批）。用於 CloudFormation template 等場景。檔案會上傳到 bouncer-uploads 桶，30 天後自動刪除。',
        'parameters': {
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
                    'description': 'Content-Type（預設 application/octet-stream）',
                    'default': 'application/octet-stream'
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
                    'description': '同步模式：等待審批結果（可能超時），預設 false',
                    'default': False
                }
            },
            'required': ['filename', 'content', 'reason', 'source']
        }
    }
}


# ============================================================================
# Lambda Handler
# ============================================================================

def lambda_handler(event, context):
    """主入口 - 路由請求"""
    # 初始化 Bot commands（cold start 時執行一次）
    init_bot_commands()

    # 支援 Function URL (rawPath) 和 API Gateway (path)
    path = event.get('rawPath') or event.get('path') or '/'

    # 支援 Function URL 和 API Gateway 的 method 格式
    method = (
        event.get('requestContext', {}).get('http', {}).get('method') or
        event.get('requestContext', {}).get('httpMethod') or
        event.get('httpMethod') or
        'GET'
    )

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
    if get_header(headers, 'x-approval-secret') != REQUEST_SECRET:
        return mcp_error(None, -32600, 'Invalid secret')

    # 解析 JSON-RPC
    try:
        body = json.loads(event.get('body', '{}'))
    except Exception as e:
        print(f"Error: {e}")
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

    elif tool_name == 'bouncer_trust_status':
        return mcp_tool_trust_status(req_id, arguments)

    elif tool_name == 'bouncer_trust_revoke':
        return mcp_tool_trust_revoke(req_id, arguments)

    elif tool_name == 'bouncer_add_account':
        return mcp_tool_add_account(req_id, arguments)

    elif tool_name == 'bouncer_list_accounts':
        return mcp_tool_list_accounts(req_id, arguments)

    elif tool_name == 'bouncer_get_page':
        return mcp_tool_get_page(req_id, arguments)

    elif tool_name == 'bouncer_list_pending':
        return mcp_tool_list_pending(req_id, arguments)

    elif tool_name == 'bouncer_remove_account':
        return mcp_tool_remove_account(req_id, arguments)

    # Deployer tools
    elif tool_name == 'bouncer_deploy':
        from deployer import mcp_tool_deploy
        return mcp_tool_deploy(req_id, arguments, table, send_approval_request)

    elif tool_name == 'bouncer_deploy_status':
        from deployer import mcp_tool_deploy_status
        return mcp_tool_deploy_status(req_id, arguments)

    elif tool_name == 'bouncer_deploy_cancel':
        from deployer import mcp_tool_deploy_cancel
        return mcp_tool_deploy_cancel(req_id, arguments)

    elif tool_name == 'bouncer_deploy_history':
        from deployer import mcp_tool_deploy_history
        return mcp_tool_deploy_history(req_id, arguments)

    elif tool_name == 'bouncer_project_list':
        from deployer import mcp_tool_project_list
        return mcp_tool_project_list(req_id, arguments)

    elif tool_name == 'bouncer_upload':
        return mcp_tool_upload(req_id, arguments)

    else:
        return mcp_error(req_id, -32602, f'Unknown tool: {tool_name}')


def mcp_tool_execute(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_execute（預設異步，立即返回 request_id）"""
    command = str(arguments.get('command', '')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
    timeout = min(int(arguments.get('timeout', MCP_MAX_WAIT)), MCP_MAX_WAIT)
    # 預設異步（避免 API Gateway 29s 超時）
    sync_mode = arguments.get('sync', False)  # 明確要求同步才等待

    if not command:
        return mcp_error(req_id, -32602, 'Missing required parameter: command')

    # 初始化預設帳號
    init_default_account()

    # 解析帳號配置
    if account_id:
        # 驗證帳號 ID 格式
        valid, error = validate_account_id(account_id)
        if not valid:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
                'isError': True
            })

        # 查詢帳號配置
        account = get_account(account_id)
        if not account:
            available = [a['account_id'] for a in list_accounts()]
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'帳號 {account_id} 未配置',
                    'available_accounts': available
                })}],
                'isError': True
            })

        if not account.get('enabled', True):
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'帳號 {account_id} 已停用'
                })}],
                'isError': True
            })

        assume_role = account.get('role_arn')
        account_name = account.get('name', account_id)
    else:
        # 使用預設帳號
        account_id = DEFAULT_ACCOUNT_ID
        assume_role = None
        account_name = 'Default'

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
        result = execute_command(command, assume_role)
        paged = store_paged_output(generate_request_id(command), result)

        response_data = {
            'status': 'auto_approved',
            'command': command,
            'account': account_id,
            'account_name': account_name,
            'result': paged['result']
        }

        if paged.get('paged'):
            response_data['paged'] = True
            response_data['page'] = paged['page']
            response_data['total_pages'] = paged['total_pages']
            response_data['output_length'] = paged['output_length']
            response_data['next_page'] = paged.get('next_page')

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response_data)
            }]
        })

    # Rate Limit 檢查（只對需要審批的命令）
    try:
        check_rate_limit(source)
    except RateLimitExceeded as e:
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'rate_limited',
                    'error': str(e),
                    'command': command,
                    'retry_after': RATE_LIMIT_WINDOW
                })
            }],
            'isError': True
        })
    except PendingLimitExceeded as e:
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'pending_limit_exceeded',
                    'error': str(e),
                    'command': command,
                    'hint': '請等待 pending 請求處理後再試'
                })
            }],
            'isError': True
        })

    # Trust Session 檢查（連續批准功能）
    should_trust, trust_session, trust_reason = should_trust_approve(command, source, account_id)
    if should_trust and trust_session:
        # 增加命令計數
        new_count = increment_trust_command_count(trust_session['request_id'])

        # 執行命令
        result = execute_command(command, assume_role)
        paged = store_paged_output(generate_request_id(command), result)

        # 計算剩餘時間
        remaining = int(trust_session.get('expires_at', 0)) - int(time.time())
        remaining_str = f"{remaining // 60}:{remaining % 60:02d}" if remaining > 0 else "0:00"

        # 發送靜默通知
        send_trust_auto_approve_notification(
            command, trust_session['request_id'], remaining_str, new_count, result
        )

        response_data = {
            'status': 'trust_auto_approved',
            'command': command,
            'account': account_id,
            'account_name': account_name,
            'result': paged['result'],
            'trust_session': trust_session['request_id'],
            'remaining': remaining_str,
            'command_count': f"{new_count}/{TRUST_SESSION_MAX_COMMANDS}"
        }

        if paged.get('paged'):
            response_data['paged'] = True
            response_data['page'] = paged['page']
            response_data['total_pages'] = paged['total_pages']
            response_data['output_length'] = paged['output_length']
            response_data['next_page'] = paged.get('next_page')

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response_data)
            }]
        })

    # Layer 3: APPROVAL (human review)
    request_id = generate_request_id(command)
    ttl = int(time.time()) + timeout + 60

    # 存入 DynamoDB
    item = {
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'source': source or '__anonymous__',  # GSI 需要有值
        'account_id': account_id,
        'account_name': account_name,
        'assume_role': assume_role,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批請求
    send_approval_request(request_id, command, reason, timeout, source, account_id, account_name)

    # 預設異步：立即返回讓 client 用 bouncer_status 輪詢
    if not sync_mode:
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'pending_approval',
                    'request_id': request_id,
                    'command': command,
                    'account': account_id,
                    'account_name': account_name,
                    'message': '請求已發送，用 bouncer_status 查詢結果',
                    'expires_in': f'{timeout} seconds'
                })
            }]
        })

    # 同步模式（sync=True）：長輪詢等待結果（可能被 API Gateway 29s 超時）
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


def mcp_tool_trust_status(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_trust_status"""
    source = arguments.get('source')
    now = int(time.time())

    try:
        if source:
            # 查詢特定 source 的信任時段
            response = table.scan(
                FilterExpression='#type = :type AND #src = :source AND expires_at > :now',
                ExpressionAttributeNames={'#type': 'type', '#src': 'source'},
                ExpressionAttributeValues={
                    ':type': 'trust_session',
                    ':source': source,
                    ':now': now
                }
            )
        else:
            # 查詢所有活躍的信任時段
            response = table.scan(
                FilterExpression='#type = :type AND expires_at > :now',
                ExpressionAttributeNames={'#type': 'type'},
                ExpressionAttributeValues={
                    ':type': 'trust_session',
                    ':now': now
                }
            )

        items = response.get('Items', [])

        # 格式化輸出
        sessions = []
        for item in items:
            remaining = item.get('expires_at', 0) - now
            remaining = int(item.get('expires_at', 0)) - now
            sessions.append({
                'trust_id': item.get('request_id'),
                'source': item.get('source'),
                'account_id': item.get('account_id'),
                'remaining_seconds': remaining,
                'remaining': f"{remaining // 60}:{remaining % 60:02d}",
                'command_count': int(item.get('command_count', 0)),
                'approved_by': item.get('approved_by')
            })

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'active_sessions': len(sessions),
                    'sessions': sessions
                }, indent=2)
            }]
        })

    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')


def mcp_tool_trust_revoke(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_trust_revoke"""
    trust_id = arguments.get('trust_id', '')

    if not trust_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: trust_id')

    success = revoke_trust_session(trust_id)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'success': success,
                'trust_id': trust_id,
                'message': '信任時段已撤銷' if success else '撤銷失敗'
            })
        }],
        'isError': not success
    })


def mcp_tool_add_account(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_add_account（需要 Telegram 審批）"""
    account_id = str(arguments.get('account_id', '')).strip()
    name = str(arguments.get('name', '')).strip()
    role_arn = str(arguments.get('role_arn', '')).strip()
    source = arguments.get('source', None)
    async_mode = arguments.get('async', False)  # 如果 True，立即返回 pending

    # 驗證
    valid, error = validate_account_id(account_id)
    if not valid:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
            'isError': True
        })

    if not name:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': '名稱不能為空'})}],
            'isError': True
        })

    valid, error = validate_role_arn(role_arn)
    if not valid:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
            'isError': True
        })

    # 建立審批請求
    request_id = generate_request_id(f"add_account:{account_id}")
    ttl = int(time.time()) + 300 + 60

    item = {
        'request_id': request_id,
        'action': 'add_account',
        'account_id': account_id,
        'account_name': name,
        'role_arn': role_arn,
        'source': source or '__anonymous__',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批
    send_account_approval_request(request_id, 'add', account_id, name, role_arn, source)

    # 如果是 async 模式，立即返回讓 client 輪詢
    if async_mode:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': request_id,
                'message': '請求已發送，等待 Telegram 確認',
                'expires_in': '300 seconds'
            })}]
        })

    # 同步模式：等待結果（會被 API Gateway 29s 超時）
    result = wait_for_result_mcp(request_id, timeout=300)

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': result.get('status') != 'approved'
    })


def mcp_tool_list_accounts(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_list_accounts"""
    init_default_account()
    accounts = list_accounts()
    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'accounts': [decimal_to_native(a) for a in accounts],
                'default_account': DEFAULT_ACCOUNT_ID
            }, indent=2, ensure_ascii=False)
        }]
    })


def mcp_tool_get_page(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_get_page - 取得長輸出的下一頁"""
    page_id = str(arguments.get('page_id', '')).strip()

    if not page_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: page_id')

    result = get_paged_output(page_id)

    if 'error' in result:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(result)}],
            'isError': True
        })

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}]
    })


def mcp_tool_list_pending(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_list_pending - 列出待審批請求"""
    source = arguments.get('source')
    limit = min(int(arguments.get('limit', 20)), 100)

    try:
        if source:
            # 查詢特定 source 的 pending 請求 (用 source-created-index + filter)
            response = table.query(
                IndexName='source-created-index',
                KeyConditionExpression='#src = :source',
                FilterExpression='#status = :status',
                ExpressionAttributeNames={'#src': 'source', '#status': 'status'},
                ExpressionAttributeValues={
                    ':source': source,
                    ':status': 'pending'
                },
                ScanIndexForward=False,
                Limit=limit
            )
        else:
            # 查詢所有 pending 請求 (用 status-created-index)
            response = table.query(
                IndexName='status-created-index',
                KeyConditionExpression='#status = :status',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={':status': 'pending'},
                ScanIndexForward=False,
                Limit=limit
            )

        items = response.get('Items', [])

        # 格式化輸出
        pending = []
        for item in items:
            created = item.get('created_at', 0)
            age_seconds = int(time.time()) - int(created) if created else 0
            pending.append({
                'request_id': item.get('request_id'),
                'command': item.get('command', '')[:100],  # 截斷長命令
                'source': item.get('source'),
                'account_id': item.get('account_id'),
                'reason': item.get('reason'),
                'age_seconds': age_seconds,
                'age': f"{age_seconds // 60}m {age_seconds % 60}s"
            })

        # 按時間排序（最舊的先）
        pending.sort(key=lambda x: x.get('age_seconds', 0), reverse=True)

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'pending_count': len(pending),
                    'requests': pending
                }, indent=2, ensure_ascii=False)
            }]
        })

    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')


def mcp_tool_remove_account(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_remove_account（需要 Telegram 審批）"""
    account_id = str(arguments.get('account_id', '')).strip()
    source = arguments.get('source', None)
    async_mode = arguments.get('async', False)

    # 驗證
    valid, error = validate_account_id(account_id)
    if not valid:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
            'isError': True
        })

    # 不能刪除預設帳號
    if account_id == DEFAULT_ACCOUNT_ID:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': '不能移除預設帳號'})}],
            'isError': True
        })

    # 檢查帳號是否存在
    account = get_account(account_id)
    if not account:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': f'帳號 {account_id} 不存在'})}],
            'isError': True
        })

    # 建立審批請求
    request_id = generate_request_id(f"remove_account:{account_id}")
    ttl = int(time.time()) + 300 + 60

    item = {
        'request_id': request_id,
        'action': 'remove_account',
        'account_id': account_id,
        'account_name': account.get('name', account_id),
        'source': source or '__anonymous__',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批
    send_account_approval_request(request_id, 'remove', account_id, account.get('name', ''), None, source)

    # 如果是 async 模式，立即返回讓 client 輪詢
    if async_mode:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': request_id,
                'message': '請求已發送，等待 Telegram 確認',
                'expires_in': '300 seconds'
            })}]
        })

    # 同步模式：等待結果
    result = wait_for_result_mcp(request_id, timeout=300)

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': result.get('status') != 'approved'
    })


# 固定上傳桶
UPLOAD_BUCKET = 'bouncer-uploads-111111111111'


def mcp_tool_upload(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_upload（上傳檔案到固定 S3 桶，需要 Telegram 審批）"""
    import base64

    filename = str(arguments.get('filename', '')).strip()
    content_b64 = str(arguments.get('content', '')).strip()
    content_type = str(arguments.get('content_type', 'application/octet-stream')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    # 預設異步（避免 API Gateway 29s 超時）
    sync_mode = arguments.get('sync', False)

    # 向後相容：如果有 bucket/key 就用舊邏輯
    legacy_bucket = arguments.get('bucket', None)
    legacy_key = arguments.get('key', None)

    # 驗證必要參數
    if not filename and not legacy_key:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': 'filename is required'})}],
            'isError': True
        })
    if not content_b64:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': 'content is required'})}],
            'isError': True
        })

    # 解碼 base64 驗證格式
    try:
        content_bytes = base64.b64decode(content_b64)
        content_size = len(content_bytes)
    except Exception as e:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': f'Invalid base64 content: {str(e)}'})}],
            'isError': True
        })

    # 檢查大小（4.5 MB 限制）
    max_size = 4.5 * 1024 * 1024
    if content_size > max_size:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'Content too large: {content_size} bytes (max {int(max_size)} bytes)'
            })}],
            'isError': True
        })

    # 決定 bucket 和 key
    if legacy_bucket and legacy_key:
        # 向後相容模式
        bucket = legacy_bucket
        key = legacy_key
    else:
        # 固定桶模式：自動產生路徑
        # 格式: {date}/{request_id}/{filename}
        bucket = UPLOAD_BUCKET
        date_str = time.strftime('%Y-%m-%d')
        # request_id 在這裡先產生，後面會用到
        request_id = generate_request_id(f"upload:{filename}")
        key = f"{date_str}/{request_id}/{filename or legacy_key}"

    # Rate limit 檢查
    if source:
        try:
            check_rate_limit(source)
        except RateLimitExceeded as e:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
                'isError': True
            })
        except PendingLimitExceeded as e:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
                'isError': True
            })

    # 建立審批請求（固定桶模式已在上面產生 request_id）
    if legacy_bucket and legacy_key:
        request_id = generate_request_id(f"upload:{bucket}:{key}")
    ttl = int(time.time()) + 300 + 60

    # 格式化大小顯示
    if content_size >= 1024 * 1024:
        size_str = f"{content_size / 1024 / 1024:.2f} MB"
    elif content_size >= 1024:
        size_str = f"{content_size / 1024:.2f} KB"
    else:
        size_str = f"{content_size} bytes"

    item = {
        'request_id': request_id,
        'action': 'upload',
        'bucket': bucket,
        'key': key,
        'content': content_b64,  # 存 base64，審批後再上傳
        'content_type': content_type,
        'content_size': content_size,
        'reason': reason,
        'source': source or '__anonymous__',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批
    s3_uri = f"s3://{bucket}/{key}"

    # 跳脫 Markdown 特殊字元
    safe_s3_uri = escape_markdown(s3_uri)
    safe_reason = escape_markdown(reason)
    safe_source = escape_markdown(source or 'Unknown')
    safe_content_type = escape_markdown(content_type)

    message = (
        f"📤 *上傳檔案請求*\n\n"
        f"🤖 *來源：* {safe_source}\n"
        f"📁 *目標：* `{safe_s3_uri}`\n"
        f"📊 *大小：* {size_str}\n"
        f"📝 *類型：* {safe_content_type}\n"
        f"💬 *原因：* {safe_reason}\n\n"
        f"🆔 *ID：* `{request_id}`"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }

    send_telegram_message(message, keyboard)

    # 預設異步：立即返回讓 client 用 bouncer_status 輪詢
    if not sync_mode:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': request_id,
                's3_uri': s3_uri,
                'size': size_str,
                'message': '請求已發送，用 bouncer_status 查詢結果',
                'expires_in': '300 seconds'
            })}]
        })

    # 同步模式（sync=True）：等待結果（可能被 API Gateway 29s 超時）
    result = wait_for_upload_result(request_id, timeout=300)

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': result.get('status') != 'approved'
    })


def wait_for_upload_result(request_id: str, timeout: int = 300) -> dict:
    """等待上傳審批結果"""
    interval = 2
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
                        's3_uri': f"s3://{item.get('bucket')}/{item.get('key')}",
                        's3_url': item.get('s3_url', ''),
                        'size': int(item.get('content_size', 0)),
                        'approved_by': item.get('approver', 'unknown'),
                        'waited_seconds': int(time.time() - start_time)
                    }
                elif status == 'denied':
                    return {
                        'status': 'denied',
                        'request_id': request_id,
                        's3_uri': f"s3://{item.get('bucket')}/{item.get('key')}",
                        'denied_by': item.get('approver', 'unknown'),
                        'waited_seconds': int(time.time() - start_time)
                    }
        except Exception as e:
            print(f"Polling error: {e}")
            pass

    return {
        'status': 'timeout',
        'request_id': request_id,
        'message': '審批請求已過期',
        'waited_seconds': timeout
    }


def execute_upload(request_id: str, approver: str) -> dict:
    """執行已審批的上傳（上傳到固定桶）"""
    import base64

    try:
        result = table.get_item(Key={'request_id': request_id})
        item = result.get('Item')

        if not item:
            return {'success': False, 'error': 'Request not found'}

        bucket = item.get('bucket')
        key = item.get('key')
        content_b64 = item.get('content')
        content_type = item.get('content_type', 'application/octet-stream')

        # 解碼內容
        content_bytes = base64.b64decode(content_b64)

        # 使用 Lambda 本身的權限上傳
        s3 = boto3.client('s3')

        # 上傳
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content_bytes,
            ContentType=content_type
        )

        # 產生 S3 URL
        region = s3.meta.region_name or 'us-east-1'
        if region == 'us-east-1':
            s3_url = f"https://{bucket}.s3.amazonaws.com/{key}"
        else:
            s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"

        # 更新 DB
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #status = :status, approver = :approver, s3_url = :url, approved_at = :at',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'approved',
                ':approver': approver,
                ':url': s3_url,
                ':at': int(time.time())
            }
        )

        return {
            'success': True,
            's3_uri': f"s3://{bucket}/{key}",
            's3_url': s3_url
        }

    except Exception as e:
        # 記錄失敗
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #status = :status, #error = :error',
            ExpressionAttributeNames={'#status': 'status', '#error': 'error'},
            ExpressionAttributeValues={
                ':status': 'error',
                ':error': str(e)
            }
        )
        return {'success': False, 'error': str(e)}


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
                    response = {
                        'status': 'approved',
                        'request_id': request_id,
                        'command': item.get('command'),
                        'result': item.get('result', ''),
                        'approved_by': item.get('approver', 'unknown'),
                        'waited_seconds': int(time.time() - start_time)
                    }
                    # 加入分頁資訊
                    if item.get('paged'):
                        response['paged'] = True
                        response['page'] = 1
                        response['total_pages'] = int(item.get('total_pages', 1))
                        response['output_length'] = int(item.get('output_length', 0))
                        response['next_page'] = item.get('next_page')
                    return response
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

    if get_header(headers, 'x-approval-secret') != REQUEST_SECRET:
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

    if get_header(headers, 'x-approval-secret') != REQUEST_SECRET:
        return response(403, {'error': 'Invalid secret'})

    if ENABLE_HMAC:
        body_str = event.get('body', '')
        if not verify_hmac(headers, body_str):
            return response(403, {'error': 'Invalid HMAC signature'})

    try:
        body = json.loads(event.get('body', '{}'))
    except Exception as e:
        print(f"Error: {e}")
        return response(400, {'error': 'Invalid JSON'})

    command = body.get('command', '').strip()
    reason = body.get('reason', 'No reason provided')
    source = body.get('source', None)  # 來源（哪個 agent/系統）
    assume_role = body.get('assume_role', None)  # 目標帳號 role ARN
    wait = body.get('wait', False)
    timeout = min(body.get('timeout', 300), MCP_MAX_WAIT)

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
        result = execute_command(command, assume_role)
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
        'source': source or '__anonymous__',
        'assume_role': assume_role,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'rest'
    }
    table.put_item(Item=item)

    send_approval_request(request_id, command, reason, timeout, source, assume_role)

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
        except Exception as e:
            print(f"Error: {e}")
            pass

    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': f'等待 {timeout} 秒後仍未審批',
        'check_status': f'/status/{request_id}'
    })


# ============================================================================
# Telegram Command Handler
# ============================================================================

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


def send_telegram_message_to(chat_id: str, text: str, parse_mode: str = None):
    """發送訊息到指定 chat"""
    data = {
        'chat_id': chat_id,
        'text': text
    }
    if parse_mode:
        data['parse_mode'] = parse_mode
    _telegram_request('sendMessage', data, timeout=10, json_body=True)


# ============================================================================
# Telegram Webhook Handler
# ============================================================================

def handle_telegram_webhook(event):
    """處理 Telegram callback"""
    headers = event.get('headers', {})

    if TELEGRAM_WEBHOOK_SECRET:
        received_secret = get_header(headers, 'x-telegram-bot-api-secret-token') or ''
        if received_secret != TELEGRAM_WEBHOOK_SECRET:
            return response(403, {'error': 'Invalid webhook signature'})

    try:
        body = json.loads(event.get('body', '{}'))
    except Exception as e:
        print(f"Error: {e}")
        return response(400, {'error': 'Invalid JSON'})

    # 處理文字訊息（指令）
    message = body.get('message')
    if message:
        return handle_telegram_command(message)

    callback = body.get('callback_query')
    if not callback:
        return response(200, {'ok': True})

    user_id = str(callback.get('from', {}).get('id', ''))
    if user_id not in APPROVED_CHAT_IDS:
        answer_callback(callback['id'], '❌ 你沒有審批權限')
        return response(403, {'error': 'Unauthorized user'})

    data = callback.get('data', '')
    if ':' not in data:
        return response(400, {'error': 'Invalid callback data'})

    action, request_id = data.split(':', 1)

    # 特殊處理：撤銷信任時段
    if action == 'revoke_trust':
        success = revoke_trust_session(request_id)
        message_id = callback.get('message', {}).get('message_id')
        if success:
            update_message(message_id, f"🛑 *信任時段已結束*\n\n`{request_id}`", remove_buttons=True)
            answer_callback(callback['id'], '🛑 信任已結束')
        else:
            answer_callback(callback['id'], '❌ 撤銷失敗')
        return response(200, {'ok': True})

    try:
        db_start = time.time()
        item = table.get_item(Key={'request_id': request_id}).get('Item')
        print(f"[TIMING] DynamoDB get_item: {(time.time() - db_start) * 1000:.0f}ms")
    except Exception as e:
        print(f"Error: {e}")
        item = None

    if not item:
        answer_callback(callback['id'], '❌ 請求已過期或不存在')
        return response(404, {'error': 'Request not found'})

    # 取得 message_id（用於更新訊息）
    message_id = callback.get('message', {}).get('message_id')

    if item['status'] not in ['pending_approval', 'pending']:
        answer_callback(callback['id'], '⚠️ 此請求已處理過')
        # 更新訊息移除按鈕
        if message_id:
            status = item.get('status', 'unknown')
            status_emoji = '✅' if status == 'approved' else '❌' if status == 'denied' else '⏰'
            source = item.get('source', '')
            command = item.get('command', '')[:200]
            reason = item.get('reason', '')
            source_line = f"🤖 *來源：* {escape_markdown(source)}\n" if source else ""
            update_message(
                message_id,
                f"{status_emoji} *已處理* (狀態: {status})\n\n"
                f"{source_line}"
                f"📋 *命令：*\n`{escape_markdown(command)}`\n\n"
                f"💬 *原因：* {escape_markdown(reason)}",
                remove_buttons=True
            )
        return response(200, {'ok': True})

    # 檢查是否過期
    ttl = item.get('ttl', 0)
    if ttl and int(time.time()) > ttl:
        answer_callback(callback['id'], '⏰ 此請求已過期')
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #s = :s',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'timeout'}
        )
        # 更新 Telegram 訊息，移除按鈕
        if message_id:
            source = item.get('source', '')
            command = item.get('command', '')
            reason = item.get('reason', '')
            source_line = f"🤖 *來源：* {escape_markdown(source)}\n" if source else ""
            cmd_preview = command[:200] + '...' if len(command) > 200 else command
            update_message(
                message_id,
                f"⏰ *已過期*\n\n"
                f"{source_line}"
                f"📋 *命令：*\n`{escape_markdown(cmd_preview)}`\n\n"
                f"💬 *原因：* {escape_markdown(reason)}",
                remove_buttons=True
            )
        return response(200, {'ok': True, 'expired': True})

    # 根據請求類型處理
    request_action = item.get('action', 'execute')  # 預設是命令執行

    if request_action == 'add_account':
        return handle_account_add_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'remove_account':
        return handle_account_remove_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'deploy':
        return handle_deploy_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'upload':
        return handle_upload_callback(action, request_id, item, message_id, callback['id'], user_id)
    else:
        return handle_command_callback(action, request_id, item, message_id, callback['id'], user_id)


def handle_command_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str):
    """處理命令執行的審批 callback"""
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
    bucket = item.get('bucket', '')
    key = item.get('key', '')
    content_size = int(item.get('content_size', 0))
    source = item.get('source', '')
    reason = item.get('reason', '')

    s3_uri = f"s3://{bucket}/{key}"
    source_line = f"🤖 來源： {source}\n" if source else ""

    # 格式化大小
    if content_size >= 1024 * 1024:
        size_str = f"{content_size / 1024 / 1024:.2f} MB"
    elif content_size >= 1024:
        size_str = f"{content_size / 1024:.2f} KB"
    else:
        size_str = f"{content_size} bytes"

    if action == 'approve':
        # 執行上傳
        result = execute_upload(request_id, user_id)

        if result.get('success'):
            update_message(
                message_id,
                f"✅ 已上傳\n\n"
                f"{source_line}"
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
            f"📁 目標： {s3_uri}\n"
            f"📊 大小： {size_str}\n"
            f"💬 原因： {reason}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

    return response(200, {'ok': True})


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
    except Exception as e:
        print(f"Error: {e}")
        return False

    payload = f"{timestamp}.{nonce}.{body}"
    expected = hmac.new(
        REQUEST_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def send_approval_request(request_id: str, command: str, reason: str, timeout: int = 840,
                          source: str = None, account_id: str = None, account_name: str = None,
                          assume_role: str = None):
    """發送 Telegram 審批請求

    Args:
        request_id: 請求 ID
        command: AWS CLI 命令
        reason: 執行原因
        timeout: 超時秒數
        source: 來源識別（哪個 agent/系統發的請求）
        account_id: AWS 帳號 ID
        account_name: 帳號名稱
        assume_role: Role ARN（向後相容，如果沒有 account_id 會從這裡解析）
    """
    cmd_preview = command if len(command) <= 500 else command[:500] + '...'
    # 轉義用戶輸入的 Markdown 特殊字元
    cmd_preview = escape_markdown(cmd_preview)
    reason = escape_markdown(reason)
    source = escape_markdown(source) if source else None

    # 檢查是否是高危操作
    dangerous = is_dangerous(command)

    # 顯示時間（秒或分鐘）
    if timeout < 60:
        timeout_str = f"{timeout} 秒"
    elif timeout < 3600:
        timeout_str = f"{timeout // 60} 分鐘"
    else:
        timeout_str = f"{timeout // 3600} 小時"

    # 來源資訊
    source_line = f"🤖 *來源：* {source}\n" if source else ""

    # 帳號資訊
    if account_id and account_name:
        account_line = f"🏢 *帳號：* `{account_id}` ({account_name})\n"
    elif assume_role:
        # 向後相容：從 assume_role 解析帳號
        try:
            parsed_account_id = assume_role.split(':')[4]
            role_name = assume_role.split('/')[-1]
            account_line = f"🏢 *帳號：* `{parsed_account_id}` ({role_name})\n"
        except Exception as e:
            print(f"Error: {e}")
            account_line = f"🏢 *Role：* `{assume_role}`\n"
    else:
        # 預設帳號
        default_account = os.environ.get('AWS_ACCOUNT_ID', '111111111111')
        account_line = f"🏢 *帳號：* `{default_account}` (預設)\n"

    # 根據是否高危決定訊息格式
    if dangerous:
        text = (
            f"⚠️ *高危操作請求* ⚠️\n\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"⚠️ *此操作可能不可逆，請仔細確認！*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"⏰ *{timeout_str}後過期*"
        )
        # 高危操作不提供信任選項
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '⚠️ 確認執行', 'callback_data': f'approve:{request_id}'},
                    {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
                ]
            ]
        }
    else:
        text = (
            f"🔐 *AWS 執行請求*\n\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"⏰ *{timeout_str}後過期*"
        )
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '✅ 批准', 'callback_data': f'approve:{request_id}'},
                    {'text': '🔓 信任10分鐘', 'callback_data': f'approve_trust:{request_id}'},
                    {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
                ]
            ]
        }

    send_telegram_message(text, keyboard)


def send_account_approval_request(request_id: str, action: str, account_id: str, name: str, role_arn: str, source: str):
    """發送帳號管理的 Telegram 審批請求"""
    # 轉義用戶輸入
    name = escape_markdown(name) if name else name
    source = escape_markdown(source) if source else None
    source_line = f"🤖 *來源：* {source}\n" if source else ""

    if action == 'add':
        text = (
            f"🔐 *新增 AWS 帳號請求*\n\n"
            f"{source_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {name}\n"
            f"🔗 *Role：* `{role_arn}`\n\n"
            f"📝 *請求 ID：* `{request_id}`\n"
            f"⏰ *5 分鐘後過期*"
        )
    else:  # remove
        text = (
            f"🔐 *移除 AWS 帳號請求*\n\n"
            f"{source_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {name}\n\n"
            f"📝 *請求 ID：* `{request_id}`\n"
            f"⏰ *5 分鐘後過期*"
        )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }

    send_telegram_message(text, keyboard)


def send_trust_auto_approve_notification(command: str, trust_id: str, remaining: str, count: int,
                                         result: str = None):
    """
    發送 Trust Session 自動批准的靜默通知

    Args:
        command: 執行的命令
        trust_id: 信任時段 ID
        remaining: 剩餘時間 (不再顯示)
        count: 已執行命令數
        result: 執行結果（可選）
    """
    cmd_preview = command if len(command) <= 100 else command[:100] + '...'
    cmd_preview = escape_markdown(cmd_preview)

    # 結果摘要
    result_preview = ""
    if result:
        # 判斷成功/失敗
        if result.startswith('❌') or 'error' in result.lower()[:100]:
            result_status = "❌"
        else:
            result_status = "✅"
        # 取前 200 字元
        result_text = result[:200] + '...' if len(result) > 200 else result
        result_text = escape_markdown(result_text)
        result_preview = f"\n{result_status} `{result_text}`"

    text = (
        f"🔓 *自動批准* (信任中)\n"
        f"📋 `{cmd_preview}`\n"
        f"📊 {count}/{TRUST_SESSION_MAX_COMMANDS}"
        f"{result_preview}"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '🛑 結束信任', 'callback_data': f'revoke_trust:{trust_id}'}
        ]]
    }

    # 靜默通知
    send_telegram_message_silent(text, keyboard)


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
# test
