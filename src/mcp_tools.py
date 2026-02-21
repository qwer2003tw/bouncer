"""
Bouncer - MCP Tool 實作模組

所有 mcp_tool_* 函數

MCP 錯誤格式規則：
- Business error（命令被阻擋、帳號不存在、格式錯誤等）→ mcp_result with isError: True
- Protocol error（缺少參數、JSON 解析失敗、內部錯誤等）→ mcp_error
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

# 從其他模組導入
from utils import mcp_result, mcp_error, generate_request_id, decimal_to_native
from commands import is_blocked, is_auto_approve, execute_command
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id, validate_role_arn,
)
from paging import store_paged_output, get_paged_output
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit
from trust import (
    revoke_trust_session, increment_trust_command_count, should_trust_approve,
)
from telegram import escape_markdown, send_telegram_message
from constants import (
    DEFAULT_ACCOUNT_ID, MCP_MAX_WAIT, RATE_LIMIT_WINDOW,
    TRUST_SESSION_MAX_COMMANDS,
)


# 延遲 import 避免循環依賴
def _get_app_module():
    """延遲取得 app module 避免循環 import"""
    import app as app_module
    return app_module

def _get_table():
    """取得 DynamoDB table"""
    app = _get_app_module()
    return app.table

def _get_accounts_table():
    """取得 accounts DynamoDB table"""
    app = _get_app_module()
    return app.accounts_table


# 預設上傳帳號 ID（Bouncer 所在帳號）
# Shadow mode 表名（用於收集智慧審批數據）
SHADOW_TABLE_NAME = os.environ.get('SHADOW_TABLE', 'bouncer-shadow-approvals')


def _log_smart_approval_shadow(
    req_id: str,
    command: str,
    reason: str,
    source: str,
    account_id: str,
    smart_decision,
) -> None:
    """
    記錄智慧審批決策到 DynamoDB（Shadow Mode）
    用於收集數據，評估準確率後再啟用
    """
    import time
    import boto3 as boto3_shadow  # 避免與頂層 import 衝突
    try:
        dynamodb = boto3_shadow.resource('dynamodb')
        table = dynamodb.Table(SHADOW_TABLE_NAME)

        item = {
            'request_id': req_id,
            'timestamp': int(time.time()),
            'command': command[:500],  # 截斷過長命令
            'reason': reason[:200],
            'source': source or 'unknown',
            'account_id': account_id,
            'smart_decision': smart_decision.decision,
            'smart_score': smart_decision.final_score,
            'smart_category': smart_decision.risk_result.category.value,
            'smart_factors': [f.__dict__ for f in smart_decision.risk_result.factors[:5]],  # 只記錄前 5 個因素
            # 30 天後自動刪除
            'ttl': int(time.time()) + 30 * 24 * 60 * 60,
        }

        table.put_item(Item=item)
        print(f"[SHADOW] Logged: {req_id} -> {smart_decision.decision} (score={smart_decision.final_score})")
    except Exception as e:
        # Shadow 記錄失敗不影響主流程
        print(f"[SHADOW] Failed to log: {e}")


def mcp_tool_execute(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_execute（預設異步，立即返回 request_id）"""
    app = _get_app_module()
    table = _get_table()

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

    # ========== Smart Approval Shadow Mode ==========
    # 記錄風險評分但不影響現有決策（收集 100 樣本後評估）
    smart_decision = None
    try:
        from smart_approval import evaluate_command as smart_evaluate
        smart_decision = smart_evaluate(
            command=command,
            reason=reason,
            source=source or 'unknown',
            account_id=account_id,
            enable_sequence_analysis=False  # 先不啟用序列分析
        )
        # 記錄到 DynamoDB（異步，不阻塞主流程）
        _log_smart_approval_shadow(
            req_id=req_id,
            command=command,
            reason=reason,
            source=source,
            account_id=account_id,
            smart_decision=smart_decision,
        )
    except Exception as e:
        # Shadow mode 失敗不影響主流程
        print(f"[SHADOW] Smart approval error: {e}")
    # ========== End Shadow Mode ==========

    # Layer 0: 合規檢查（最高優先，違反安規直接攔截）
    try:
        from compliance_checker import check_compliance
        is_compliant, violation = check_compliance(command)
        if not is_compliant:
            print(f"[COMPLIANCE] Blocked: {violation.rule_id} - {violation.rule_name}")
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'compliance_violation',
                        'rule_id': violation.rule_id,
                        'rule_name': violation.rule_name,
                        'description': violation.description,
                        'remediation': violation.remediation,
                        'command': command[:200],
                    })
                }],
                'isError': True
            })
    except ImportError:
        pass  # compliance_checker 模組不存在時跳過（向後兼容）

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
        app.send_trust_auto_approve_notification(
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
    app.send_approval_request(request_id, command, reason, timeout, source, account_id, account_name)

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
    result = app.wait_for_result_mcp(request_id, timeout=timeout)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps(result)
        }],
        'isError': result.get('status') in ['denied', 'timeout', 'error']
    })


def mcp_tool_status(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_status"""
    table = _get_table()
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


def mcp_tool_help(req_id, arguments: dict) -> dict:
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


def mcp_tool_trust_status(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_trust_status"""
    table = _get_table()
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
    app = _get_app_module()
    table = _get_table()

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
    app.send_account_approval_request(request_id, 'add', account_id, name, role_arn, source)

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
    result = app.wait_for_result_mcp(request_id, timeout=300)

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
    table = _get_table()
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
    app = _get_app_module()
    table = _get_table()

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
    app.send_account_approval_request(request_id, 'remove', account_id, account.get('name', ''), None, source)

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
    result = app.wait_for_result_mcp(request_id, timeout=300)

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': result.get('status') != 'approved'
    })


def mcp_tool_upload(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_upload（上傳檔案到 S3 桶，支援跨帳號，需要 Telegram 審批）"""
    import base64
    app = _get_app_module()
    table = _get_table()

    filename = str(arguments.get('filename', '')).strip()
    content_b64 = str(arguments.get('content', '')).strip()
    content_type = str(arguments.get('content_type', 'application/octet-stream')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
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

    assume_role = None
    account_name = 'Default'
    target_account_id = DEFAULT_ACCOUNT_ID

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
        target_account_id = account_id

    # 決定 bucket 和 key
    if legacy_bucket and legacy_key:
        # 向後相容模式
        bucket = legacy_bucket
        key = legacy_key
    else:
        # 自動產生路徑: bouncer-uploads-{account_id}/{date}/{request_id}/{filename}
        bucket = f"bouncer-uploads-{target_account_id}"
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
        'account_id': target_account_id,
        'account_name': account_name,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    # Only store assume_role if it has a value (DynamoDB doesn't accept None for strings)
    if assume_role:
        item['assume_role'] = assume_role
    table.put_item(Item=item)

    # 發送 Telegram 審批
    s3_uri = f"s3://{bucket}/{key}"

    # 跳脫 Markdown 特殊字元
    safe_s3_uri = escape_markdown(s3_uri)
    safe_reason = escape_markdown(reason)
    safe_source = escape_markdown(source or 'Unknown')
    safe_content_type = escape_markdown(content_type)
    safe_account = escape_markdown(f"{target_account_id} ({account_name})")

    message = (
        f"📤 *上傳檔案請求*\n\n"
        f"🤖 *來源：* {safe_source}\n"
        f"🏦 *帳號：* {safe_account}\n"
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
    result = app.wait_for_upload_result(request_id, timeout=300)

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': result.get('status') != 'approved'
    })
