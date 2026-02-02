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
import shlex
import boto3
from decimal import Decimal
from typing import Optional, Dict


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


# ============================================================================
# 版本
# ============================================================================
VERSION = '3.0.0'

# ============================================================================
# 環境變數
# ============================================================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
# 支援多個 Chat ID，用逗號分隔
APPROVED_CHAT_IDS = set(os.environ.get('APPROVED_CHAT_ID', '999999999').replace(' ', '').split(','))
APPROVED_CHAT_ID = os.environ.get('APPROVED_CHAT_ID', '999999999').split(',')[0]  # 向後相容，取第一個作為主要發送目標
REQUEST_SECRET = os.environ.get('REQUEST_SECRET', '')
TABLE_NAME = os.environ.get('TABLE_NAME', 'clawdbot-approval-requests')
ACCOUNTS_TABLE_NAME = os.environ.get('ACCOUNTS_TABLE_NAME', 'bouncer-accounts')
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')
DEFAULT_ACCOUNT_ID = os.environ.get('DEFAULT_ACCOUNT_ID', '111111111111')

# HMAC 驗證開關
ENABLE_HMAC = os.environ.get('ENABLE_HMAC', 'false').lower() == 'true'

# MCP 模式的最大等待時間（秒）- Lambda 最長 15 分鐘，保留 1 分鐘餘量
MCP_MAX_WAIT = int(os.environ.get('MCP_MAX_WAIT', '840'))  # 14 分鐘

# DynamoDB
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)
accounts_table = dynamodb.Table(ACCOUNTS_TABLE_NAME)

# ============================================================================
# 帳號管理
# ============================================================================

def init_default_account():
    """初始化預設帳號（如果不存在）"""
    try:
        result = accounts_table.get_item(Key={'account_id': DEFAULT_ACCOUNT_ID})
        if 'Item' not in result:
            accounts_table.put_item(Item={
                'account_id': DEFAULT_ACCOUNT_ID,
                'name': 'Default',
                'role_arn': None,
                'is_default': True,
                'enabled': True,
                'created_at': int(time.time())
            })
    except Exception as e:
        print(f"Error initializing default account: {e}")

def get_account(account_id: str) -> Optional[Dict]:
    """取得帳號配置"""
    try:
        result = accounts_table.get_item(Key={'account_id': account_id})
        return result.get('Item')
    except:
        return None

def list_accounts() -> list:
    """列出所有帳號"""
    try:
        result = accounts_table.scan()
        return result.get('Items', [])
    except:
        return []

def validate_account_id(account_id: str) -> tuple:
    """驗證帳號 ID 格式"""
    if not account_id:
        return False, "帳號 ID 不能為空"
    if not account_id.isdigit():
        return False, "帳號 ID 必須是數字"
    if len(account_id) != 12:
        return False, "帳號 ID 必須是 12 位數字"
    return True, None

def validate_role_arn(role_arn: str) -> tuple:
    """驗證 Role ARN 格式"""
    if not role_arn:
        return True, None  # 空的 role_arn 是允許的（預設帳號）
    if not role_arn.startswith('arn:aws:iam::'):
        return False, "Role ARN 格式不正確，應該是 arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME"
    if ':role/' not in role_arn:
        return False, "Role ARN 格式不正確，缺少 :role/"
    return True, None

# ============================================================================
# Rate Limiting
# ============================================================================

RATE_LIMIT_WINDOW = 60  # 60 秒視窗
RATE_LIMIT_MAX_REQUESTS = 5  # 每視窗最多 5 個審批請求
MAX_PENDING_PER_SOURCE = 10  # 每 source 最多 10 個 pending
RATE_LIMIT_ENABLED = os.environ.get('RATE_LIMIT_ENABLED', 'true').lower() == 'true'

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
# 命令分類系統（四層）
# ============================================================================

# Layer 1: BLOCKED - 永遠拒絕
BLOCKED_PATTERNS = [
    # IAM 危險操作
    'iam create', 'iam delete', 'iam attach', 'iam detach',
    'iam put', 'iam update', 'iam add', 'iam remove',
    # STS 危險操作
    'sts assume-role',
    'sts get-session-token',      # 取得臨時 credentials
    'sts get-federation-token',   # 取得 federation token
    # Secrets/KMS 危險操作
    'secretsmanager get-secret-value',  # 讀取 secrets
    'kms decrypt',                       # 解密資料
    # Compute 危險操作
    'lambda invoke',              # 直接呼叫 Lambda
    'ecs run-task',               # 執行 ECS task
    'eks get-token',              # 取得 EKS token
    # S3 Presigned URL
    '--presign',                  # 產生 presigned URL
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
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（12 位數字），不填則使用預設帳號'
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
    }
}


# ============================================================================
# Lambda Handler
# ============================================================================

def lambda_handler(event, context):
    """主入口 - 路由請求"""
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

    elif tool_name == 'bouncer_add_account':
        return mcp_tool_add_account(req_id, arguments)

    elif tool_name == 'bouncer_list_accounts':
        return mcp_tool_list_accounts(req_id, arguments)

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

    else:
        return mcp_error(req_id, -32602, f'Unknown tool: {tool_name}')


def mcp_tool_execute(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_execute"""
    command = str(arguments.get('command', '')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
    timeout = min(int(arguments.get('timeout', MCP_MAX_WAIT)), MCP_MAX_WAIT)
    async_mode = arguments.get('async', False)  # 如果 True，立即返回 pending

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
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'auto_approved',
                    'command': command,
                    'account': account_id,
                    'account_name': account_name,
                    'result': result
                })
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

    # 如果是 async 模式，立即返回讓 client 輪詢
    if async_mode:
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'pending_approval',
                    'request_id': request_id,
                    'command': command,
                    'account': account_id,
                    'account_name': account_name,
                    'message': '請求已發送，等待 Telegram 確認',
                    'expires_in': f'{timeout} seconds'
                })
            }]
        })

    # 同步模式：長輪詢等待結果（會被 API Gateway 29s 超時）
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
    except:
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
        received_secret = get_header(headers, 'x-telegram-bot-api-secret-token') or ''
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
    if user_id not in APPROVED_CHAT_IDS:
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
        return response(200, {'ok': True, 'expired': True})

    message_id = callback.get('message', {}).get('message_id')

    # 根據請求類型處理
    request_action = item.get('action', 'execute')  # 預設是命令執行

    if request_action == 'add_account':
        return handle_account_add_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'remove_account':
        return handle_account_remove_callback(action, request_id, item, message_id, callback['id'], user_id)
    elif request_action == 'deploy':
        return handle_deploy_callback(action, request_id, item, message_id, callback['id'], user_id)
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

        result_preview = result[:1000] if len(result) > 1000 else result
        update_message(
            message_id,
            f"✅ *已批准並執行*\n\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"📤 *結果：*\n```\n{result_preview}\n```"
        )
        answer_callback(callback_id, '✅ 已執行')

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
            f"❌ *已拒絕*\n\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{command}`\n\n"
            f"💬 *原因：* {reason}"
        )
        answer_callback(callback_id, '❌ 已拒絕')

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
            update_message(
                message_id,
                f"🚀 *部署已啟動*\n\n"
                f"{source_line}"
                f"📦 *專案：* {project_name}\n"
                f"🌿 *分支：* {branch}\n"
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


# ============================================================================
# 命令分類函數
# ============================================================================

def is_blocked(command: str) -> bool:
    """Layer 1: 檢查命令是否在黑名單"""
    import re
    # 移除 --query 參數內容（JMESPath 語法可能包含反引號）
    # 匹配 --query '...' 或 --query "..." 或 --query xxx（無引號，到下一個空格或結尾）
    cmd_sanitized = re.sub(r"--query\s+['\"].*?['\"]", "--query REDACTED", command)
    cmd_sanitized = re.sub(r"--query\s+[^\s'\"]+", "--query REDACTED", cmd_sanitized)
    cmd_lower = cmd_sanitized.lower()
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

def execute_command(command: str, assume_role_arn: str = None) -> str:
    """執行 AWS CLI 命令

    Args:
        command: AWS CLI 命令
        assume_role_arn: 可選，要 assume 的 role ARN
    """
    import sys
    from io import StringIO

    try:
        # 使用 shlex.split 但保留引號給 JSON 參數
        # shlex.split 會移除引號，所以我們需要特殊處理
        try:
            args = shlex.split(command)
        except ValueError as e:
            return f'❌ 命令格式錯誤: {str(e)}'

        if not args or args[0] != 'aws':
            return '❌ 只能執行 aws CLI 命令'

        # 移除 'aws' 前綴，awscli.clidriver 不需要它
        cli_args = args[1:]

        # 修復 JSON 參數：shlex.split 會把 {"key": "value"} 變成 {key: value}
        # 需要從原始命令中重新提取 JSON 參數
        json_params = ['--item', '--expression-attribute-values', '--expression-attribute-names',
                       '--key', '--update-expression', '--cli-input-json', '--filter-expression']

        for i, arg in enumerate(cli_args):
            if arg in json_params and i + 1 < len(cli_args):
                # 從原始命令中找出這個參數的值
                param_pos = command.find(arg)
                if param_pos != -1:
                    # 找參數後面的值（可能是 JSON）
                    after_param = command[param_pos + len(arg):].lstrip()
                    if after_param.startswith('{') or after_param.startswith("'{") or after_param.startswith('"{'):
                        # 提取 JSON 字串
                        after_param = after_param.lstrip("'\"")
                        brace_count = 0
                        json_end = 0
                        for j, c in enumerate(after_param):
                            if c == '{':
                                brace_count += 1
                            elif c == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    json_end = j + 1
                                    break
                        if json_end > 0:
                            json_str = after_param[:json_end]
                            cli_args[i + 1] = json_str

        # 保存原始環境變數
        original_env = {}

        # 如果需要 assume role，先取得臨時 credentials
        if assume_role_arn:
            try:
                sts = boto3.client('sts')
                assumed = sts.assume_role(
                    RoleArn=assume_role_arn,
                    RoleSessionName='bouncer-execution',
                    DurationSeconds=900  # 15 分鐘
                )
                creds = assumed['Credentials']

                # 設定環境變數讓 awscli 使用這些 credentials
                original_env = {
                    'AWS_ACCESS_KEY_ID': os.environ.get('AWS_ACCESS_KEY_ID'),
                    'AWS_SECRET_ACCESS_KEY': os.environ.get('AWS_SECRET_ACCESS_KEY'),
                    'AWS_SESSION_TOKEN': os.environ.get('AWS_SESSION_TOKEN'),
                }
                os.environ['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
                os.environ['AWS_SECRET_ACCESS_KEY'] = creds['SecretAccessKey']
                os.environ['AWS_SESSION_TOKEN'] = creds['SessionToken']

            except Exception as e:
                return f'❌ Assume role 失敗: {str(e)}'

        # 捕獲 stdout/stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()

        try:
            from awscli.clidriver import create_clidriver
            driver = create_clidriver()

            # 禁用 pager
            os.environ['AWS_PAGER'] = ''

            exit_code = driver.main(cli_args)

            stdout_output = sys.stdout.getvalue()
            stderr_output = sys.stderr.getvalue()

        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

            # 還原環境變數
            if assume_role_arn and original_env:
                for key, value in original_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        output = stdout_output or stderr_output or '(no output)'

        if exit_code != 0 and not output.strip():
            output = f'(exit code: {exit_code})'

        return output[:4000]

    except ImportError:
        return '❌ awscli 模組未安裝'
    except ValueError as e:
        return f'❌ 命令格式錯誤: {str(e)}'
    except Exception as e:
        return f'❌ 執行錯誤: {str(e)}'


# ============================================================================
# Telegram API
# ============================================================================

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
        except:
            account_line = f"🏢 *Role：* `{assume_role}`\n"
    else:
        # 預設帳號
        default_account = os.environ.get('AWS_ACCOUNT_ID', '111111111111')
        account_line = f"🏢 *帳號：* `{default_account}` (預設)\n"

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
        'inline_keyboard': [[
            {'text': '✅ 批准執行', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }

    send_telegram_message(text, keyboard)


def send_account_approval_request(request_id: str, action: str, account_id: str, name: str, role_arn: str, source: str):
    """發送帳號管理的 Telegram 審批請求"""
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
