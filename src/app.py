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
from typing import Optional

# 從模組導入（逐步遷移中）
try:
    from telegram import (  # noqa: F401
        escape_markdown, send_telegram_message, send_telegram_message_silent,
        update_message, answer_callback,
        _telegram_request,
    )
    from paging import store_paged_output, get_paged_output  # noqa: F401
    from trust import revoke_trust_session, create_trust_session, increment_trust_command_count, should_trust_approve, is_trust_excluded  # noqa: F401
    from commands import is_blocked, is_dangerous, is_auto_approve, execute_command, aws_cli_split  # noqa: F401
    from accounts import (  # noqa: F401
        init_bot_commands, init_default_account, get_account, list_accounts,
        validate_account_id, validate_role_arn,
    )
    from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit  # noqa: F401
    from utils import response, generate_request_id, decimal_to_native, mcp_result, mcp_error, get_header
    # 新模組
    from mcp_tools import (
        mcp_tool_execute, mcp_tool_status, mcp_tool_help, mcp_tool_trust_status, mcp_tool_trust_revoke,
        mcp_tool_add_account, mcp_tool_list_accounts, mcp_tool_get_page,
        mcp_tool_list_pending, mcp_tool_remove_account, mcp_tool_upload,
    )
    from callbacks import (
        handle_command_callback, handle_account_add_callback, handle_account_remove_callback,
        handle_deploy_callback, handle_upload_callback,
    )
    from telegram_commands import handle_telegram_command
except ImportError:
    from src.telegram import (  # noqa: F401
        escape_markdown, send_telegram_message, send_telegram_message_silent,
        update_message, answer_callback,
    )
    from src.paging import store_paged_output, get_paged_output  # noqa: F401
    from src.trust import revoke_trust_session, create_trust_session, increment_trust_command_count, should_trust_approve, is_trust_excluded  # noqa: F401
    from src.commands import is_blocked, is_dangerous, is_auto_approve, execute_command, aws_cli_split  # noqa: F401
    from src.accounts import (  # noqa: F401
        init_bot_commands, init_default_account, get_account, list_accounts,
        validate_account_id, validate_role_arn,
    )
    from src.rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit  # noqa: F401
    from src.utils import response, generate_request_id, decimal_to_native, mcp_result, mcp_error, get_header
    # 新模組
    from src.mcp_tools import (
        mcp_tool_execute, mcp_tool_status, mcp_tool_help, mcp_tool_trust_status, mcp_tool_trust_revoke,
        mcp_tool_add_account, mcp_tool_list_accounts, mcp_tool_get_page,
        mcp_tool_list_pending, mcp_tool_remove_account, mcp_tool_upload,
    )
    from src.callbacks import (
        handle_command_callback, handle_account_add_callback, handle_account_remove_callback,
        handle_deploy_callback, handle_upload_callback,
    )
    from src.telegram_commands import handle_telegram_command

# 從 constants.py 導入所有常數
try:
    # Lambda 環境
    from constants import (  # noqa: F401
        VERSION,
        TELEGRAM_TOKEN, TELEGRAM_WEBHOOK_SECRET,
        APPROVED_CHAT_IDS,
        TABLE_NAME, ACCOUNTS_TABLE_NAME,
        DEFAULT_ACCOUNT_ID,
        REQUEST_SECRET, ENABLE_HMAC,
        MCP_MAX_WAIT,
        RATE_LIMIT_WINDOW,
        TRUST_SESSION_MAX_COMMANDS,
        BLOCKED_PATTERNS, AUTO_APPROVE_PREFIXES,
    )
except ImportError:
    # 本地測試環境
    from src.constants import (  # noqa: F401
        VERSION,
        TELEGRAM_WEBHOOK_SECRET,
        APPROVED_CHAT_IDS,
        TABLE_NAME, ACCOUNTS_TABLE_NAME,
        DEFAULT_ACCOUNT_ID,
        REQUEST_SECRET, ENABLE_HMAC,
        MCP_MAX_WAIT,
        RATE_LIMIT_WINDOW,
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
    'bouncer_help': {
        'description': '查詢 AWS CLI 命令的參數說明，不需要執行命令',
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：ec2 modify-instance-attribute）'
                },
                'service': {
                    'type': 'string',
                    'description': '只列出服務的所有操作（例如：ec2）'
                }
            }
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
        'description': '上傳檔案到 S3 桶（需要 Telegram 審批）。支援跨帳號上傳，檔案會上傳到 bouncer-uploads-{account_id} 桶，30 天後自動刪除。',
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
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（預設使用 Bouncer 所在帳號）'
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

    elif tool_name == 'bouncer_help':
        return mcp_tool_help(req_id, arguments)

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


# ============================================================================
# Upload 相關函數（被 callbacks 呼叫）
# ============================================================================

# 固定上傳桶
# 預設上傳帳號（Bouncer 所在帳號，向後相容）
DEFAULT_UPLOAD_ACCOUNT_ID = '190825685292'


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
    """執行已審批的上傳（支援跨帳號）"""
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
        assume_role_arn = item.get('assume_role')

        # 解碼內容
        content_bytes = base64.b64decode(content_b64)

        # 建立 S3 client（跨帳號時用 assume role）
        if assume_role_arn:
            sts = boto3.client('sts')
            assumed = sts.assume_role(
                RoleArn=assume_role_arn,
                RoleSessionName='bouncer-upload'
            )
            creds = assumed['Credentials']
            s3 = boto3.client(
                's3',
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretAccessKey'],
                aws_session_token=creds['SessionToken']
            )
        else:
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
                    response_data = {
                        'status': 'approved',
                        'request_id': request_id,
                        'command': item.get('command'),
                        'result': item.get('result', ''),
                        'approved_by': item.get('approver', 'unknown'),
                        'waited_seconds': int(time.time() - start_time)
                    }
                    # 加入分頁資訊
                    if item.get('paged'):
                        response_data['paged'] = True
                        response_data['page'] = 1
                        response_data['total_pages'] = int(item.get('total_pages', 1))
                        response_data['output_length'] = int(item.get('output_length', 0))
                        response_data['next_page'] = item.get('next_page')
                    return response_data
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
        default_account = os.environ.get('AWS_ACCOUNT_ID', '')
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
# 向後兼容 - re-export 移到子模組的函數 (測試用)
# ============================================================================

# 從 telegram_commands 模組 re-export (for tests)
try:
    from telegram_commands import (  # noqa: F401
        send_telegram_message_to,
        handle_accounts_command,
        handle_trust_command,
        handle_pending_command,
        handle_help_command,
    )
except ImportError:
    from src.telegram_commands import (  # noqa: F401
        send_telegram_message_to,
        handle_accounts_command,
        handle_trust_command,
        handle_pending_command,
        handle_help_command,
    )
