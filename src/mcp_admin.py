"""
Bouncer - Admin / Query MCP Tools

mcp_tool_status, mcp_tool_help, mcp_tool_trust_status, mcp_tool_trust_revoke,
mcp_tool_add_account, mcp_tool_list_accounts, mcp_tool_remove_account,
mcp_tool_get_page, mcp_tool_list_pending, mcp_tool_list_safelist
"""

import json
import time


from utils import mcp_result, mcp_error, generate_request_id, decimal_to_native, generate_display_summary
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id, validate_role_arn,
)
from paging import get_paged_output
from trust import revoke_trust_session
from db import table
from notifications import send_account_approval_request
from constants import (
    DEFAULT_ACCOUNT_ID,
    APPROVAL_TIMEOUT_DEFAULT, APPROVAL_TTL_BUFFER,
    AUTO_APPROVE_PREFIXES, BLOCKED_PATTERNS,
)


def mcp_tool_status(req_id: str, arguments: dict) -> dict:
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


def mcp_tool_help(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_help - 查詢 AWS CLI 命令說明"""
    try:
        from help_command import get_command_help, get_service_operations, format_help_text
    except ImportError:
        return mcp_error(req_id, -32603, 'help_command module not found')

    command = arguments.get('command', '').strip()
    service = arguments.get('service', '').strip()

    if service:
        # 列出服務的所有操作
        result = get_service_operations(service)
    elif command:
        # 查詢特定命令的參數
        result = get_command_help(command)
    else:
        return mcp_error(req_id, -32602, 'Missing parameter: command or service')

    # 加入格式化文字版本
    if 'error' not in result or 'similar_operations' in result:
        result['formatted'] = format_help_text(result)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps(result, ensure_ascii=False, indent=2)
        }]
    })


def mcp_tool_trust_status(req_id: str, arguments: dict) -> dict:
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


def mcp_tool_trust_revoke(req_id: str, arguments: dict) -> dict:
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


def mcp_tool_add_account(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_add_account（需要 Telegram 審批）"""

    account_id = str(arguments.get('account_id', '')).strip()
    name = str(arguments.get('name', '')).strip()
    role_arn = str(arguments.get('role_arn', '')).strip()
    source = arguments.get('source', None)
    context = arguments.get('context', None)

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
    ttl = int(time.time()) + APPROVAL_TIMEOUT_DEFAULT + APPROVAL_TTL_BUFFER

    item = {
        'request_id': request_id,
        'action': 'add_account',
        'account_id': account_id,
        'account_name': name,
        'role_arn': role_arn,
        'source': source or '__anonymous__',
        'context': context or '',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp',
        'display_summary': generate_display_summary('add_account', account_name=name, account_id=account_id),
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批
    send_account_approval_request(request_id, 'add', account_id, name, role_arn, source, context=context)

    # 一律異步返回（sync long-polling 已移除）
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'status': 'pending_approval',
            'request_id': request_id,
            'message': '請求已發送，等待 Telegram 確認',
            'expires_in': f'{APPROVAL_TIMEOUT_DEFAULT} seconds'
        })}]
    })


def mcp_tool_list_accounts(req_id: str, arguments: dict) -> dict:
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


def mcp_tool_get_page(req_id: str, arguments: dict) -> dict:
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


def mcp_tool_list_pending(req_id: str, arguments: dict) -> dict:
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


def mcp_tool_remove_account(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_remove_account（需要 Telegram 審批）"""

    account_id = str(arguments.get('account_id', '')).strip()
    source = arguments.get('source', None)
    context = arguments.get('context', None)

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
    ttl = int(time.time()) + APPROVAL_TIMEOUT_DEFAULT + APPROVAL_TTL_BUFFER

    item = {
        'request_id': request_id,
        'action': 'remove_account',
        'account_id': account_id,
        'account_name': account.get('name', account_id),
        'source': source or '__anonymous__',
        'context': context or '',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp',
        'display_summary': generate_display_summary('remove_account', account_name=account.get('name', account_id), account_id=account_id),
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批
    send_account_approval_request(request_id, 'remove', account_id, account.get('name', ''), None, source, context=context)

    # 一律異步返回（sync long-polling 已移除）
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'status': 'pending_approval',
            'request_id': request_id,
            'message': '請求已發送，等待 Telegram 確認',
            'expires_in': f'{APPROVAL_TIMEOUT_DEFAULT} seconds'
        })}]
    })


def mcp_tool_list_safelist(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_list_safelist — 列出 safelist 和 blocked patterns"""
    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'safelist_prefixes': AUTO_APPROVE_PREFIXES,
                'blocked_patterns': BLOCKED_PATTERNS
            }, indent=2)
        }]
    })
