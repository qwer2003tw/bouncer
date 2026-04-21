"""
Bouncer - Grant Session MCP Tools

Extracted from mcp_execute.py (sprint60-002).
Contains:
- mcp_tool_request_grant
- mcp_tool_grant_status
- mcp_tool_revoke_grant
- mcp_tool_grant_execute
"""

import json
import time
import urllib.error

from aws_lambda_powertools import Logger

from utils import mcp_result, mcp_error, log_decision
from commands import execute_boto3_native
from accounts import (
    init_default_account, get_account, validate_account_id,
)
from paging import store_paged_output
from db import table
from notifications import (
    send_grant_request_notification,
    send_grant_execute_notification,
)
from constants import DEFAULT_ACCOUNT_ID
from compliance_checker import check_compliance  # noqa: E402
from grant import (  # noqa: E402
    create_grant_request,
    get_grant_session,
    get_grant_status,
    is_command_in_grant,
    revoke_grant,
    try_use_grant_command,
)

logger = Logger(service="bouncer")


def mcp_tool_request_grant(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_grant — 批次申請命令執行權限"""
    try:

        commands = arguments.get('commands', [])
        reason = str(arguments.get('reason', '')).strip()
        source = arguments.get('source', None)
        account_id = arguments.get('account', None)
        ttl_minutes = arguments.get('ttl_minutes', None)
        allow_repeat = arguments.get('allow_repeat', False)
        approval_timeout = arguments.get('approval_timeout', None)
        project = arguments.get('project', None)

        if not commands:
            return mcp_error(req_id, -32602, 'Missing required parameter: commands')
        if not reason:
            return mcp_error(req_id, -32602, 'Missing required parameter: reason')
        if not source:
            return mcp_error(req_id, -32602, 'Missing required parameter: source')

        # 解析帳號
        init_default_account()
        if account_id:
            account_id = str(account_id).strip()
            valid, error = validate_account_id(account_id)
            if not valid:
                return mcp_result(req_id, {
                    'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
                    'isError': True
                })
            account = get_account(account_id)
            if not account:
                return mcp_result(req_id, {
                    'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': f'帳號 {account_id} 未配置'})}],
                    'isError': True
                })
        else:
            account_id = DEFAULT_ACCOUNT_ID

        if ttl_minutes is not None:
            ttl_minutes = int(ttl_minutes)

        if approval_timeout is not None:
            approval_timeout = int(approval_timeout)

        result = create_grant_request(
            commands=commands,
            reason=reason,
            source=source,
            account_id=account_id,
            ttl_minutes=ttl_minutes,
            allow_repeat=allow_repeat,
            approval_timeout=approval_timeout,
            project=project,
        )

        # 發送 Telegram 審批通知
        try:
            send_grant_request_notification(
                grant_id=result['grant_id'],
                commands_detail=result['commands_detail'],
                reason=reason,
                source=source,
                account_id=account_id,
                ttl_minutes=result['ttl_minutes'],
                allow_repeat=allow_repeat,
                project=project,
            )
        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
            logger.exception(f"[GRANT] Failed to send notification: {e}", extra={"src_module": "grant", "operation": "send_notification", "error": str(e)})
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'pending_approval',
                    'grant_request_id': result['grant_id'],
                    'summary': result['summary'],
                    'expires_in': f"{result['expires_in']} seconds",
                })
            }]
        })

    except ValueError as e:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
            'isError': True
        })
    except Exception:  # noqa: BLE001 — MCP tool entry point
        logger.exception("Internal error", extra={"src_module": "mcp_grant", "operation": "request_grant"})
        return mcp_error(req_id, -32603, 'Internal server error')


def mcp_tool_grant_status(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_grant_status — 查詢 Grant Session 狀態"""
    try:

        grant_id = str(arguments.get('grant_id', '')).strip()
        source = arguments.get('source', None)

        if not grant_id:
            return mcp_error(req_id, -32602, 'Missing required parameter: grant_id')
        if not source:
            return mcp_error(req_id, -32602, 'Missing required parameter: source')

        status = get_grant_status(grant_id, source)
        if not status:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'error': 'Grant not found or source mismatch',
                    'grant_id': grant_id,
                })}],
                'isError': True
            })

        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(status)}]
        })

    except Exception:  # noqa: BLE001 — MCP tool entry point
        logger.exception("Internal error", extra={"src_module": "mcp_grant", "operation": "grant_status"})
        return mcp_error(req_id, -32603, 'Internal server error')


def mcp_tool_revoke_grant(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_revoke_grant — 撤銷 Grant Session"""
    try:

        grant_id = str(arguments.get('grant_id', '')).strip()
        if not grant_id:
            return mcp_error(req_id, -32602, 'Missing required parameter: grant_id')

        success = revoke_grant(grant_id)

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'success': success,
                    'grant_id': grant_id,
                    'message': 'Grant 已撤銷' if success else '撤銷失敗',
                })
            }],
            'isError': not success
        })

    except Exception:  # noqa: BLE001 — MCP tool entry point
        logger.exception("Internal error", extra={"src_module": "mcp_grant", "operation": "revoke_grant"})
        return mcp_error(req_id, -32603, 'Internal server error')


def mcp_tool_grant_execute(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_grant_execute — 在 Grant Session 內執行 AWS 操作（boto3 native，fail-fast）"""
    try:
        # 1. 解析必填參數（native 格式）
        grant_id = str(arguments.get('grant_id', '')).strip()
        aws_args = arguments.get('aws', {})
        service = str(aws_args.get('service', '')).strip().lower()
        operation = str(aws_args.get('operation', '')).strip().lower()
        params = aws_args.get('params', {})
        region = aws_args.get('region') or None
        source = str(arguments.get('source', '')).strip()
        reason = str(arguments.get('reason', 'Grant execute')).strip()
        account_param = str(arguments.get('account', '')).strip() if arguments.get('account') else None

        if not grant_id or not service or not operation or not source:
            return mcp_error(req_id, -32602, 'Missing required parameter: grant_id, aws.service, aws.operation, source')

        # 2. 產生 native command key（用於 grant 匹配）
        cmd_key = f"{service}:{operation}"

        # 3. 帳號解析
        init_default_account()
        if account_param:
            valid, error = validate_account_id(account_param)
            if not valid:
                return mcp_result(req_id, {
                    'content': [{
                        'type': 'text',
                        'text': json.dumps({
                            'status': 'account_not_found',
                            'message': f'Invalid account: {error}'
                        })
                    }],
                    'isError': True
                })

            account = get_account(account_param)
            if not account:
                return mcp_result(req_id, {
                    'content': [{
                        'type': 'text',
                        'text': json.dumps({
                            'status': 'account_not_found',
                            'message': f'Account {account_param} not found'
                        })
                    }],
                    'isError': True
                })
            account_id = account_param
        else:
            account_id = DEFAULT_ACCOUNT_ID
            account = get_account(account_id) if account_id else None

        if not account_id:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'account_not_found',
                        'message': 'Default account not configured'
                    })
                }],
                'isError': True
            })

        # 4. 取 grant session（不存在 → grant_not_found）

        grant = get_grant_session(grant_id)
        if not grant:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'grant_not_found',
                        'message': 'Grant not found or expired'
                    })
                }],
                'isError': True
            })

        # 5. source 匹配（失敗也回 grant_not_found，不洩漏 grant 是否存在）
        if grant.get('source') != source:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'grant_not_found',
                        'message': 'Grant not found or expired'
                    })
                }],
                'isError': True
            })

        # 6. status 檢查
        if grant.get('status') != 'active':
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'grant_not_active',
                        'message': f'Grant is not active (status: {grant.get("status")})'
                    })
                }],
                'isError': True
            })

        # 7. TTL 檢查
        if time.time() > grant.get('expires_at', 0):
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'grant_expired',
                        'message': 'Grant has expired'
                    })
                }],
                'isError': True
            })

        # 8. account 匹配（grant 建立時指定的帳號）
        if grant.get('account_id') and grant['account_id'] != account_id:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'account_mismatch',
                        'message': 'Account does not match grant'
                    })
                }],
                'isError': True
            })

        # 9. compliance_checker（用 synthetic CLI 命令做 pattern 匹配）

        synthetic_cmd = f"aws {service} {operation.replace('_', '-')}"
        is_compliant, violation = check_compliance(synthetic_cmd)
        if not is_compliant:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'compliance_violation',
                        'rule_id': violation.rule_id,
                        'message': violation.message
                    })
                }],
                'isError': True
            })

        # 10. 命令在 grant 白名單？（native key 格式匹配）
        if not is_command_in_grant(cmd_key, grant):
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'command_not_in_grant',
                        'message': 'Operation is not in the approved grant list'
                    })
                }],
                'isError': True
            })

        # 11. allow_repeat 檢查（pre-check 以提供具體錯誤訊息）
        allow_repeat = grant.get('allow_repeat', False)
        used_commands = grant.get('used_commands', {})

        if not allow_repeat and cmd_key in used_commands:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'command_already_used',
                        'message': 'Operation already used (allow_repeat=False)'
                    })
                }],
                'isError': True
            })

        # 12. 原子性標記使用（防並發）
        if not try_use_grant_command(grant_id, cmd_key, allow_repeat):
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'command_already_used',
                        'message': 'Operation already used or SEC-009 limit reached'
                    })
                }],
                'isError': True
            })

        # 13. 執行命令（boto3 native）
        assume_role = account.get('role_arn') if account and account_id != DEFAULT_ACCOUNT_ID else None
        if not assume_role and grant.get('assume_role_arn'):
            assume_role = grant['assume_role_arn']
        result = execute_boto3_native(
            service=service, operation=operation, params=params,
            region=region, assume_role_arn=assume_role,
        )

        # 14. Store Telegram pages (Sprint 83: MCP never paged)
        paged = store_paged_output(req_id, result)
        result_text = paged.result  # full result

        # 15. Telegram 通知（best-effort）
        display_cmd = f"{service}.{operation}"
        try:
            send_grant_execute_notification(
                grant_id=grant_id,
                command=display_cmd,
                result=result_text,
                source=source,
                request_id=req_id
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send grant execute notification", extra={"src_module": "grant", "operation": "send_notification"})

        # 16. DynamoDB audit log
        log_decision(
            table=table,
            request_id=req_id,
            command=display_cmd,
            reason=reason,
            source=source,
            account_id=account_id,
            decision_type='grant_approved',
            grant_id=grant_id,
            result_summary=result_text[:200]
        )

        # 17. Return full result (Sprint 83: no page_id)
        response = {
            'status': 'grant_executed',
            'result': result_text,
            'request_id': req_id,
            'grant_id': grant_id,
        }

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response)
            }],
            'isError': False
        })

    except Exception:
        logger.exception("Internal error", extra={"src_module": "mcp_grant", "operation": "grant_execute"})
        return mcp_error(req_id, -32603, 'Internal server error')
