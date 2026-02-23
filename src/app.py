"""
Bouncer - Clawdbot AWS 命令審批執行系統
版本: 3.0.0 (MCP 支援)
更新: 2026-02-03

支援兩種模式：
1. REST API（向後兼容）
2. MCP JSON-RPC（新增）
"""

import json
import hashlib
import hmac
import time
import boto3


# 從模組導入
from telegram import (  # noqa: F401
    escape_markdown, send_telegram_message, send_telegram_message_silent,
    update_message, answer_callback,
    _telegram_request,
)
from paging import store_paged_output, get_paged_output  # noqa: F401
from trust import revoke_trust_session, create_trust_session, increment_trust_command_count, should_trust_approve, is_trust_excluded  # noqa: F401
from commands import is_blocked, get_block_reason, is_dangerous, is_auto_approve, execute_command, aws_cli_split  # noqa: F401
from accounts import (  # noqa: F401
    init_bot_commands, init_default_account, get_account, list_accounts,
    validate_account_id, validate_role_arn,
)
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit  # noqa: F401
from utils import response, generate_request_id, decimal_to_native, mcp_result, mcp_error, get_header, log_decision
# 新模組
from mcp_tools import (
    mcp_tool_execute, mcp_tool_status, mcp_tool_help, mcp_tool_trust_status, mcp_tool_trust_revoke,
    mcp_tool_add_account, mcp_tool_list_accounts, mcp_tool_get_page,
    mcp_tool_list_pending, mcp_tool_remove_account, mcp_tool_upload,
    mcp_tool_request_grant, mcp_tool_grant_status, mcp_tool_revoke_grant,
    mcp_tool_upload_batch,
)
from callbacks import (
    handle_command_callback, handle_account_add_callback, handle_account_remove_callback,
    handle_deploy_callback, handle_upload_callback, handle_upload_batch_callback,
    handle_grant_approve_all, handle_grant_approve_safe, handle_grant_deny,
)
from telegram_commands import handle_telegram_command
from tool_schema import MCP_TOOLS  # noqa: F401
from metrics import emit_metric

# 從 constants.py 導入所有常數
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
    APPROVAL_TIMEOUT_DEFAULT, APPROVAL_TTL_BUFFER, COMMAND_APPROVAL_TIMEOUT,
    UPLOAD_TIMEOUT, TELEGRAM_TIMESTAMP_MAX_AGE,
)


# DynamoDB — canonical references in db.py; re-exported for backward compat
from db import table, accounts_table  # noqa: F401


# ============================================================================
# Lambda Handler
# ============================================================================

def lambda_handler(event: dict, context) -> dict:
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
    emit_metric('Bouncer', 'ToolCall', 1, dimensions={'ToolName': tool_name})

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

    elif tool_name == 'bouncer_upload_batch':
        return mcp_tool_upload_batch(req_id, arguments)

    # Grant Session tools
    elif tool_name == 'bouncer_request_grant':
        return mcp_tool_request_grant(req_id, arguments)

    elif tool_name == 'bouncer_grant_status':
        return mcp_tool_grant_status(req_id, arguments)

    elif tool_name == 'bouncer_revoke_grant':
        return mcp_tool_revoke_grant(req_id, arguments)

    else:
        return mcp_error(req_id, -32602, f'Unknown tool: {tool_name}')


# ============================================================================
# Upload 相關函數（被 callbacks 呼叫）
# ============================================================================

def wait_for_upload_result(request_id: str, timeout: int = UPLOAD_TIMEOUT) -> dict:
    """等待上傳審批結果

    DEPRECATED: Sync long-polling 已移除。Lambda timeout 已降至 60s，
    API Gateway 29s timeout 使 sync wait 無意義。
    此函數保留以維持測試相容性，但不應再被呼叫。
    """
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


def wait_for_result_mcp(request_id: str, timeout: int = COMMAND_APPROVAL_TIMEOUT) -> dict:
    """MCP 模式的長輪詢，最多 timeout 秒

    DEPRECATED: Sync long-polling 已移除。Lambda timeout 已降至 60s，
    API Gateway 29s timeout 使 sync wait 無意義。
    此函數保留以維持測試相容性，但不應再被呼叫。
    """
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


def handle_clawdbot_request(event: dict) -> dict:
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
    timeout = min(body.get('timeout', APPROVAL_TIMEOUT_DEFAULT), MCP_MAX_WAIT)

    if not command:
        return response(400, {'error': 'Missing command'})

    # Layer 1: BLOCKED
    block_reason = get_block_reason(command)
    if block_reason:
        log_decision(
            table=table,
            request_id=generate_request_id(command),
            command=command,
            reason=reason,
            source=source,
            account_id=None,
            decision_type='blocked',
        )
        return response(403, {
            'status': 'blocked',
            'error': '命令被安全規則封鎖',
            'block_reason': block_reason,
            'command': command[:200]
        })

    # Layer 2: SAFELIST
    if is_auto_approve(command):
        result = execute_command(command, assume_role)
        log_decision(
            table=table,
            request_id=generate_request_id(command),
            command=command,
            reason=reason,
            source=source,
            account_id=None,
            decision_type='auto_approved',
            mode='rest',
        )
        return response(200, {
            'status': 'auto_approved',
            'command': command,
            'result': result
        })

    # Layer 3: APPROVAL
    request_id = generate_request_id(command)
    ttl = int(time.time()) + timeout + APPROVAL_TTL_BUFFER

    item = {
        'request_id': request_id,
        'command': command,
        'reason': reason,
        'source': source or '__anonymous__',
        'assume_role': assume_role,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'rest',
        'decision_type': 'pending',
    }
    table.put_item(Item=item)

    send_approval_request(request_id, command, reason, timeout, source, assume_role)

    # Sync long-polling 已移除（wait=True 不再生效）。
    # 一律返回 pending，讓 client 用 /status/{id} 輪詢。
    return response(202, {
        'status': 'pending_approval',
        'request_id': request_id,
        'message': '請求已發送，等待 Telegram 確認',
        'expires_in': f'{timeout} seconds',
        'check_status': f'/status/{request_id}'
    })


def wait_for_result_rest(request_id: str, timeout: int = 50) -> dict:
    """REST API 的輪詢等待

    DEPRECATED: Sync long-polling 已移除。Lambda timeout 已降至 60s，
    API Gateway 29s timeout 使 sync wait 無意義。
    此函數保留以維持測試相容性，但不應再被呼叫。
    """
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

def handle_telegram_webhook(event: dict) -> dict:
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
    emit_metric('Bouncer', 'ApprovalAction', 1, dimensions={'Action': action})

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

    # 特殊處理：Grant Session callbacks
    if action == 'grant_approve_all':
        return handle_grant_approve_all(callback, request_id)
    elif action == 'grant_approve_safe':
        return handle_grant_approve_safe(callback, request_id)
    elif action == 'grant_deny':
        return handle_grant_deny(callback, request_id)
    elif action == 'grant_revoke':
        from grant import revoke_grant
        success = revoke_grant(request_id)
        message_id = callback.get('message', {}).get('message_id')
        if success:
            update_message(message_id, f"🛑 *Grant 已撤銷*\n\n`{request_id}`", remove_buttons=True)
            answer_callback(callback['id'], '🛑 Grant 已撤銷')
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
            context = item.get('context', '')
            source_line = f"🤖 *來源：* {escape_markdown(source)}\n" if source else ""
            context_line = f"📝 *任務：* {escape_markdown(context)}\n" if context else ""
            update_message(
                message_id,
                f"{status_emoji} *已處理* (狀態: {status})\n\n"
                f"{source_line}"
                f"{context_line}"
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
            context = item.get('context', '')
            source_line = f"🤖 *來源：* {escape_markdown(source)}\n" if source else ""
            context_line = f"📝 *任務：* {escape_markdown(context)}\n" if context else ""
            cmd_preview = command[:200] + '...' if len(command) > 200 else command
            update_message(
                message_id,
                f"⏰ *已過期*\n\n"
                f"{source_line}"
                f"{context_line}"
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
    elif request_action == 'upload_batch':
        return handle_upload_batch_callback(action, request_id, item, message_id, callback['id'], user_id)
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
        if abs(time.time() - ts) > TELEGRAM_TIMESTAMP_MAX_AGE:
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


# Notification functions moved to notifications.py — re-exported for backward compat
from notifications import (  # noqa: F401, E402
    send_approval_request,
    send_account_approval_request,
    send_trust_auto_approve_notification,
    send_grant_request_notification,
    send_grant_execute_notification,
    send_grant_complete_notification,
    send_blocked_notification,
)

# ============================================================================
# 向後兼容 - re-export 移到子模組的函數 (測試用)
# ============================================================================

# 從 telegram_commands 模組 re-export (for tests)
from telegram_commands import (  # noqa: F401, E402
    send_telegram_message_to,
    handle_accounts_command,
    handle_trust_command,
    handle_pending_command,
    handle_help_command,
)
