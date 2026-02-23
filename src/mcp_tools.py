"""
Bouncer - MCP Tool 實作模組

所有 mcp_tool_* 函數

MCP 錯誤格式規則：
- Business error（命令被阻擋、帳號不存在、格式錯誤等）→ mcp_result with isError: True
- Protocol error（缺少參數、JSON 解析失敗、內部錯誤等）→ mcp_error
"""

import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional


# 從其他模組導入
from utils import mcp_result, mcp_error, generate_request_id, decimal_to_native, log_decision
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
from db import table
from notifications import (
    send_approval_request,
    send_account_approval_request,
    send_trust_auto_approve_notification,
    send_grant_request_notification,
    send_grant_execute_notification,
)
from constants import (
    DEFAULT_ACCOUNT_ID, MCP_MAX_WAIT, RATE_LIMIT_WINDOW,
    TRUST_SESSION_MAX_COMMANDS,
    APPROVAL_TIMEOUT_DEFAULT, APPROVAL_TTL_BUFFER, UPLOAD_TIMEOUT,
    AUDIT_TTL_SHORT,
    GRANT_SESSION_ENABLED,
)


# DynamoDB tables imported from db.py (no circular dependency)
# Notification functions imported from notifications.py


# 預設上傳帳號 ID（Bouncer 所在帳號）
# Shadow mode 表名（用於收集智慧審批數據）
SHADOW_TABLE_NAME = os.environ.get('SHADOW_TABLE', 'bouncer-shadow-approvals')


def _safe_risk_category(smart_decision):
    """安全取得 risk category 值（相容 enum 和 string）"""
    if not smart_decision:
        return None
    try:
        cat = smart_decision.risk_result.category
        return cat.value if hasattr(cat, 'value') else cat
    except Exception:
        return None


def _safe_risk_factors(smart_decision):
    """安全取得 risk factors（相容各種格式，float → Decimal）"""
    if not smart_decision:
        return None
    try:
        from decimal import Decimal as _Dec
        factors = [f.__dict__ for f in smart_decision.risk_result.factors[:5]]
        # 將 float 轉為 Decimal（DynamoDB 不接受 float）
        sanitized = []
        for factor in factors:
            sanitized.append({
                k: _Dec(str(v)) if isinstance(v, float) else v
                for k, v in factor.items()
            })
        return sanitized
    except Exception:
        return None


def _log_smart_approval_shadow(
    req_id: str,
    command: str,
    reason: str,
    source: str,
    account_id: str,
    smart_decision,
    actual_decision: str = '',
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

        shadow_id = f"shadow-{secrets.token_hex(12)}"
        item = {
            'request_id': shadow_id,
            'mcp_req_id': req_id,
            'timestamp': int(time.time()),
            'command': command[:500],
            'reason': reason[:200],
            'source': source or 'unknown',
            'account_id': account_id,
            'smart_decision': smart_decision.decision,
            'smart_score': smart_decision.final_score,
            'smart_category': smart_decision.risk_result.category.value,
            'smart_factors': json.dumps([f.__dict__ for f in smart_decision.risk_result.factors[:5]], default=str),
            'actual_decision': actual_decision,
            'ttl': int(time.time()) + AUDIT_TTL_SHORT,
        }

        table.put_item(Item=item)
        print(f"[SHADOW] Logged: {shadow_id} -> {smart_decision.decision} (score={smart_decision.final_score}, actual={actual_decision})")
    except Exception as e:
        # Shadow 記錄失敗不影響主流程
        print(f"[SHADOW] Failed to log: {e}")


# =============================================================================
# Execute Pipeline — Context + Check Functions
# =============================================================================

@dataclass
class ExecuteContext:
    """Pipeline context for mcp_tool_execute"""
    req_id: str
    command: str
    reason: str
    source: Optional[str]
    context: Optional[str]
    account_id: str
    account_name: str
    assume_role: Optional[str]
    timeout: int
    sync_mode: bool
    smart_decision: object = None  # smart_approval result (or None)
    mode: str = 'mcp'
    grant_id: Optional[str] = None


def _parse_execute_request(req_id, arguments: dict) -> 'dict | ExecuteContext':
    """Parse and validate execute request arguments.

    Returns an ExecuteContext on success, or an MCP error/result dict on
    validation failure (caller should return immediately).
    """
    command = str(arguments.get('command', '')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    context = arguments.get('context', None)
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
    timeout = min(int(arguments.get('timeout', MCP_MAX_WAIT)), MCP_MAX_WAIT)
    sync_mode = arguments.get('sync', False)

    if not command:
        return mcp_error(req_id, -32602, 'Missing required parameter: command')

    # 初始化預設帳號
    init_default_account()

    # 解析帳號配置
    if account_id:
        valid, error = validate_account_id(account_id)
        if not valid:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
                'isError': True
            })

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
        account_id = DEFAULT_ACCOUNT_ID
        account = get_account(account_id) if account_id else None
        assume_role = account.get('role_arn') if account else None
        account_name = account.get('name', 'Default') if account else 'Default'

    return ExecuteContext(
        req_id=req_id,
        command=command,
        reason=reason,
        source=source,
        context=context,
        account_id=account_id,
        account_name=account_name,
        assume_role=assume_role,
        timeout=timeout,
        sync_mode=sync_mode,
        grant_id=arguments.get('grant_id', None),
    )


def _score_risk(ctx: ExecuteContext) -> None:
    """Smart Approval Shadow Mode — score risk, log to DynamoDB.

    Mutates ctx.smart_decision in-place.  Never raises.
    """
    try:
        from smart_approval import evaluate_command as smart_evaluate
        ctx.smart_decision = smart_evaluate(
            command=ctx.command,
            reason=ctx.reason,
            source=ctx.source or 'unknown',
            account_id=ctx.account_id,
            enable_sequence_analysis=False,
        )
    except Exception as e:
        print(f"[SHADOW] Smart approval error: {e}")


def _extract_actual_decision(result: dict) -> str:
    """Extract actual decision from pipeline result for shadow comparison."""
    try:
        content = result.get('result', {}).get('content', [{}])
        if content:
            text = content[0].get('text', '{}')
            data = json.loads(text)
            status = data.get('status', '')
            # Map to comparable decision labels
            if status == 'auto_approved':
                return 'auto_approve'
            elif status == 'blocked':
                return 'blocked'
            elif status == 'compliance_blocked':
                return 'blocked'
            elif status == 'pending_approval':
                return 'needs_approval'
            elif status == 'trust_auto_approved':
                return 'auto_approve'
            return status
    except Exception:
        pass
    return 'unknown'


def _check_compliance(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 0: compliance check — blocks on security-rule violations."""
    try:
        from compliance_checker import check_compliance
        is_compliant, violation = check_compliance(ctx.command)
        if not is_compliant:
            print(f"[COMPLIANCE] Blocked: {violation.rule_id} - {violation.rule_name}")
            log_decision(
                table=table,
                request_id=generate_request_id(ctx.command),
                command=ctx.command,
                reason=ctx.reason,
                source=ctx.source,
                account_id=ctx.account_id,
                decision_type='compliance_violation',
                risk_score=ctx.smart_decision.final_score if ctx.smart_decision else None,
                risk_category=_safe_risk_category(ctx.smart_decision),
                risk_factors=_safe_risk_factors(ctx.smart_decision),
                violation_rule_id=violation.rule_id,
                violation_rule_name=violation.rule_name,
            )
            return mcp_result(ctx.req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'compliance_violation',
                        'rule_id': violation.rule_id,
                        'rule_name': violation.rule_name,
                        'description': violation.description,
                        'remediation': violation.remediation,
                        'command': ctx.command[:200],
                    })
                }],
                'isError': True
            })
    except ImportError:
        pass  # compliance_checker 模組不存在時跳過（向後兼容）
    return None


def _check_blocked(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 1: blocked commands."""
    if is_blocked(ctx.command):
        log_decision(
            table=table,
            request_id=generate_request_id(ctx.command),
            command=ctx.command,
            reason=ctx.reason,
            source=ctx.source,
            account_id=ctx.account_id,
            decision_type='blocked',
            risk_score=ctx.smart_decision.final_score if ctx.smart_decision else None,
            risk_category=_safe_risk_category(ctx.smart_decision),
            risk_factors=_safe_risk_factors(ctx.smart_decision),
        )
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'blocked',
                    'error': 'Command blocked for security',
                    'command': ctx.command
                })
            }],
            'isError': True
        })
    return None


def _check_grant_session(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 2: Grant session auto-approve — execute if command is in an active grant.

    Fallthrough design: returns None on any mismatch/error so the pipeline
    continues to the next layer (auto_approve, trust, approval, etc.).
    """
    try:
        if not GRANT_SESSION_ENABLED:
            return None

        grant_id = ctx.grant_id
        if not grant_id:
            return None

        from grant import (
            normalize_command, get_grant_session, is_command_in_grant,
            try_use_grant_command,
        )

        grant = get_grant_session(grant_id)

        # Grant 不存在或非 active → fallthrough
        if not grant or grant.get('status') != 'active':
            return None

        # Source/Account 不匹配 → fallthrough
        if grant.get('source') != (ctx.source or '') or grant.get('account_id') != ctx.account_id:
            return None

        # 過期 → fallthrough
        if int(time.time()) > int(grant.get('expires_at', 0)):
            return None

        # Normalize 比對
        normalized_cmd = normalize_command(ctx.command)
        if not is_command_in_grant(normalized_cmd, grant):
            return None  # 不在清單 → fallthrough

        # 總執行次數檢查
        if int(grant.get('total_executions', 0)) >= int(grant.get('max_total_executions', 50)):
            return None  # 超限 → fallthrough

        # Conditional update（防並發）
        success = try_use_grant_command(
            grant_id, normalized_cmd,
            allow_repeat=grant.get('allow_repeat', False),
        )
        if not success:
            return None  # 已用過或並發衝突 → fallthrough

        # 執行命令
        result = execute_command(ctx.command, ctx.assume_role)
        paged = store_paged_output(generate_request_id(ctx.command), result)

        # 計算剩餘資訊
        granted_commands = grant.get('granted_commands', [])
        used_commands = grant.get('used_commands', {})
        remaining_seconds = max(0, int(grant.get('expires_at', 0)) - int(time.time()))
        remaining_str = f"{remaining_seconds // 60}:{remaining_seconds % 60:02d}"
        remaining_info = f"{len(used_commands) + 1}/{len(granted_commands)} 命令, {remaining_str}"

        # 通知
        send_grant_execute_notification(ctx.command, grant_id, result, remaining_info)

        # Audit log
        log_decision(
            table=table,
            request_id=generate_request_id(ctx.command),
            command=ctx.command,
            reason=ctx.reason,
            source=ctx.source,
            account_id=ctx.account_id,
            decision_type='grant_approved',
            risk_score=ctx.smart_decision.final_score if ctx.smart_decision else None,
            risk_category=_safe_risk_category(ctx.smart_decision),
            risk_factors=_safe_risk_factors(ctx.smart_decision),
            account_name=ctx.account_name,
            grant_id=grant_id,
            mode='mcp',
        )

        response_data = {
            'status': 'grant_auto_approved',
            'command': ctx.command,
            'account': ctx.account_id,
            'account_name': ctx.account_name,
            'result': paged['result'],
            'grant_id': grant_id,
            'remaining': remaining_info,
        }

        if paged.get('paged'):
            response_data['paged'] = True
            response_data['page'] = paged['page']
            response_data['total_pages'] = paged['total_pages']
            response_data['output_length'] = paged['output_length']
            response_data['next_page'] = paged.get('next_page')

        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response_data)
            }]
        })

    except Exception as e:
        # Grant 失敗不影響主流程 → fallthrough
        print(f"[GRANT] _check_grant_session error: {e}")
        return None


def _check_auto_approve(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 2: safelist auto-approve — execute immediately."""
    if not is_auto_approve(ctx.command):
        return None

    result = execute_command(ctx.command, ctx.assume_role)
    paged = store_paged_output(generate_request_id(ctx.command), result)

    log_decision(
        table=table,
        request_id=generate_request_id(ctx.command),
        command=ctx.command,
        reason=ctx.reason,
        source=ctx.source,
        account_id=ctx.account_id,
        decision_type='auto_approved',
        risk_score=ctx.smart_decision.final_score if ctx.smart_decision else None,
        risk_category=_safe_risk_category(ctx.smart_decision),
        risk_factors=_safe_risk_factors(ctx.smart_decision),
        account_name=ctx.account_name,
        mode='mcp',
    )

    response_data = {
        'status': 'auto_approved',
        'command': ctx.command,
        'account': ctx.account_id,
        'account_name': ctx.account_name,
        'result': paged['result']
    }

    if paged.get('paged'):
        response_data['paged'] = True
        response_data['page'] = paged['page']
        response_data['total_pages'] = paged['total_pages']
        response_data['output_length'] = paged['output_length']
        response_data['next_page'] = paged.get('next_page')

    return mcp_result(ctx.req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps(response_data)
        }]
    })


def _check_rate_limit(ctx: ExecuteContext) -> Optional[dict]:
    """Rate limit check — only for commands requiring approval."""
    try:
        check_rate_limit(ctx.source)
    except RateLimitExceeded as e:
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'rate_limited',
                    'error': str(e),
                    'command': ctx.command,
                    'retry_after': RATE_LIMIT_WINDOW
                })
            }],
            'isError': True
        })
    except PendingLimitExceeded as e:
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'pending_limit_exceeded',
                    'error': str(e),
                    'command': ctx.command,
                    'hint': '請等待 pending 請求處理後再試'
                })
            }],
            'isError': True
        })
    return None


def _check_trust_session(ctx: ExecuteContext) -> Optional[dict]:
    """Trust session auto-approve — execute if trusted."""
    should_trust, trust_session, trust_reason = should_trust_approve(
        ctx.command, ctx.source, ctx.account_id
    )
    if not (should_trust and trust_session):
        return None

    # 增加命令計數
    new_count = increment_trust_command_count(trust_session['request_id'])

    # 執行命令
    result = execute_command(ctx.command, ctx.assume_role)
    paged = store_paged_output(generate_request_id(ctx.command), result)

    # 計算剩餘時間
    remaining = int(trust_session.get('expires_at', 0)) - int(time.time())
    remaining_str = f"{remaining // 60}:{remaining % 60:02d}" if remaining > 0 else "0:00"

    # 發送靜默通知
    send_trust_auto_approve_notification(
        ctx.command, trust_session['request_id'], remaining_str, new_count, result,
        source=ctx.source
    )

    log_decision(
        table=table,
        request_id=generate_request_id(ctx.command),
        command=ctx.command,
        reason=ctx.reason,
        source=ctx.source,
        account_id=ctx.account_id,
        decision_type='trust_approved',
        risk_score=ctx.smart_decision.final_score if ctx.smart_decision else None,
        risk_category=_safe_risk_category(ctx.smart_decision),
        risk_factors=_safe_risk_factors(ctx.smart_decision),
        account_name=ctx.account_name,
        trust_session_id=trust_session['request_id'],
        mode='mcp',
    )

    response_data = {
        'status': 'trust_auto_approved',
        'command': ctx.command,
        'account': ctx.account_id,
        'account_name': ctx.account_name,
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

    return mcp_result(ctx.req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps(response_data)
        }]
    })


def _submit_for_approval(ctx: ExecuteContext) -> dict:
    """Layer 3: submit for human approval — always returns a result."""

    request_id = generate_request_id(ctx.command)
    ttl = int(time.time()) + ctx.timeout + APPROVAL_TTL_BUFFER

    # 存入 DynamoDB
    item = {
        'request_id': request_id,
        'command': ctx.command,
        'reason': ctx.reason,
        'source': ctx.source or '__anonymous__',  # GSI 需要有值
        'context': ctx.context or '',
        'account_id': ctx.account_id,
        'account_name': ctx.account_name,
        'assume_role': ctx.assume_role,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    if ctx.smart_decision:
        from decimal import Decimal as _Dec
        item['risk_score'] = _Dec(str(ctx.smart_decision.final_score))
        item['risk_category'] = _safe_risk_category(ctx.smart_decision) or ''
        item['risk_factors'] = _safe_risk_factors(ctx.smart_decision) or []
        item['decision_type'] = 'pending'  # 會在 callback 時更新
    table.put_item(Item=item)

    # 發送 Telegram 審批請求
    send_approval_request(
        request_id, ctx.command, ctx.reason, ctx.timeout, ctx.source,
        ctx.account_id, ctx.account_name, context=ctx.context
    )

    # 一律異步返回：讓 client 用 bouncer_status 輪詢結果。
    # sync long-polling 已移除（Lambda 60s timeout + API Gateway 29s timeout 使其無意義）。
    return mcp_result(ctx.req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'status': 'pending_approval',
                'request_id': request_id,
                'command': ctx.command,
                'account': ctx.account_id,
                'account_name': ctx.account_name,
                'message': '請求已發送，用 bouncer_status 查詢結果',
                'expires_in': f'{ctx.timeout} seconds'
            })
        }]
    })


# =============================================================================
# Public Entry Point
# =============================================================================

def mcp_tool_execute(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_execute（預設異步，立即返回 request_id）"""
    # Phase 1: Parse & validate request, resolve account
    ctx = _parse_execute_request(req_id, arguments)
    if not isinstance(ctx, ExecuteContext):
        return ctx  # validation error — already an MCP response dict

    # Phase 2: Smart approval shadow scoring (before any decision)
    _score_risk(ctx)

    # Phase 3: Pipeline — first non-None result wins
    result = (
        _check_compliance(ctx)
        or _check_blocked(ctx)
        or _check_grant_session(ctx)
        or _check_auto_approve(ctx)
        or _check_rate_limit(ctx)
        or _check_trust_session(ctx)
        or _submit_for_approval(ctx)
    )

    # Phase 4: Log shadow with actual decision for comparison
    if ctx.smart_decision:
        actual = _extract_actual_decision(result)
        _log_smart_approval_shadow(
            req_id=ctx.req_id,
            command=ctx.command,
            reason=ctx.reason,
            source=ctx.source,
            account_id=ctx.account_id,
            smart_decision=ctx.smart_decision,
            actual_decision=actual,
        )

    return result


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
        'mode': 'mcp'
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
        'mode': 'mcp'
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


# =============================================================================
# Upload Pipeline — Context + Step Functions
# =============================================================================

@dataclass
class UploadContext:
    """Pipeline context for mcp_tool_upload"""
    req_id: str
    filename: str
    content_b64: str
    content_type: str
    content_size: int
    reason: str
    source: Optional[str]
    sync_mode: bool
    legacy_bucket: Optional[str]
    legacy_key: Optional[str]
    account_id: str
    account_name: str
    assume_role: Optional[str]
    target_account_id: str
    bucket: str = ''
    key: str = ''
    request_id: str = ''


def _parse_upload_request(req_id, arguments: dict) -> 'dict | UploadContext':
    """Parse and validate upload request arguments.

    Returns an UploadContext on success, or an MCP response dict on failure.
    """
    import base64

    filename = str(arguments.get('filename', '')).strip()
    content_b64 = str(arguments.get('content', '')).strip()
    content_type = str(arguments.get('content_type', 'application/octet-stream')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
    sync_mode = arguments.get('sync', False)

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

    # 解析帳號
    assume_role = None
    account_name = 'Default'
    target_account_id = DEFAULT_ACCOUNT_ID

    if not account_id and DEFAULT_ACCOUNT_ID:
        default_account = get_account(DEFAULT_ACCOUNT_ID)
        if default_account:
            assume_role = default_account.get('role_arn')
            account_name = default_account.get('name', 'Default')

    if account_id:
        valid, error = validate_account_id(account_id)
        if not valid:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
                'isError': True
            })

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

    return UploadContext(
        req_id=req_id,
        filename=filename,
        content_b64=content_b64,
        content_type=content_type,
        content_size=content_size,
        reason=reason,
        source=source,
        sync_mode=sync_mode,
        legacy_bucket=legacy_bucket,
        legacy_key=legacy_key,
        account_id=account_id or DEFAULT_ACCOUNT_ID,
        account_name=account_name,
        assume_role=assume_role,
        target_account_id=target_account_id,
    )


def _resolve_upload_target(ctx: UploadContext) -> None:
    """Determine bucket, key, and request_id.  Mutates ctx in-place."""
    if ctx.legacy_bucket and ctx.legacy_key:
        ctx.bucket = ctx.legacy_bucket
        ctx.key = ctx.legacy_key
    else:
        ctx.bucket = f"bouncer-uploads-{ctx.target_account_id}"
        date_str = time.strftime('%Y-%m-%d')
        ctx.request_id = generate_request_id(f"upload:{ctx.filename}")
        ctx.key = f"{date_str}/{ctx.request_id}/{ctx.filename or ctx.legacy_key}"


def _check_upload_rate_limit(ctx: UploadContext) -> Optional[dict]:
    """Rate limit check for uploads."""
    if not ctx.source:
        return None
    try:
        check_rate_limit(ctx.source)
    except RateLimitExceeded as e:
        return mcp_result(ctx.req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
            'isError': True
        })
    except PendingLimitExceeded as e:
        return mcp_result(ctx.req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
            'isError': True
        })
    return None


def _submit_upload_for_approval(ctx: UploadContext) -> dict:
    """Submit upload for human approval — always returns a result."""

    # 固定桶模式在 _resolve_upload_target 時 request_id 尚未設定
    if ctx.legacy_bucket and ctx.legacy_key:
        ctx.request_id = generate_request_id(f"upload:{ctx.bucket}:{ctx.key}")
    ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER

    # 格式化大小顯示
    if ctx.content_size >= 1024 * 1024:
        size_str = f"{ctx.content_size / 1024 / 1024:.2f} MB"
    elif ctx.content_size >= 1024:
        size_str = f"{ctx.content_size / 1024:.2f} KB"
    else:
        size_str = f"{ctx.content_size} bytes"

    item = {
        'request_id': ctx.request_id,
        'action': 'upload',
        'bucket': ctx.bucket,
        'key': ctx.key,
        'content': ctx.content_b64,  # 存 base64，審批後再上傳
        'content_type': ctx.content_type,
        'content_size': ctx.content_size,
        'reason': ctx.reason,
        'source': ctx.source or '__anonymous__',
        'account_id': ctx.target_account_id,
        'account_name': ctx.account_name,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    if ctx.assume_role:
        item['assume_role'] = ctx.assume_role
    table.put_item(Item=item)

    # 發送 Telegram 審批
    s3_uri = f"s3://{ctx.bucket}/{ctx.key}"

    safe_s3_uri = escape_markdown(s3_uri)
    safe_reason = escape_markdown(ctx.reason)
    safe_source = escape_markdown(ctx.source or 'Unknown')
    safe_content_type = escape_markdown(ctx.content_type)
    safe_account = escape_markdown(f"{ctx.target_account_id} ({ctx.account_name})")

    message = (
        f"📤 *上傳檔案請求*\n\n"
        f"🤖 *來源：* {safe_source}\n"
        f"🏦 *帳號：* {safe_account}\n"
        f"📁 *目標：* `{safe_s3_uri}`\n"
        f"📊 *大小：* {size_str}\n"
        f"📝 *類型：* {safe_content_type}\n"
        f"💬 *原因：* {safe_reason}\n\n"
        f"🆔 *ID：* `{ctx.request_id}`"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准', 'callback_data': f'approve:{ctx.request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{ctx.request_id}'}
        ]]
    }

    send_telegram_message(message, keyboard)

    # 一律異步返回：讓 client 用 bouncer_status 輪詢結果。
    # sync long-polling 已移除。
    return mcp_result(ctx.req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'status': 'pending_approval',
            'request_id': ctx.request_id,
            's3_uri': s3_uri,
            'size': size_str,
            'message': '請求已發送，用 bouncer_status 查詢結果',
            'expires_in': f'{UPLOAD_TIMEOUT} seconds'
        })}]
    })


def mcp_tool_upload(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_upload（上傳檔案到 S3 桶，支援跨帳號，需要 Telegram 審批）"""
    # Phase 1: Parse & validate request, resolve account
    ctx = _parse_upload_request(req_id, arguments)
    if not isinstance(ctx, UploadContext):
        return ctx  # validation error

    # Phase 2: Determine bucket/key/request_id
    _resolve_upload_target(ctx)

    # Phase 3: Pipeline — first non-None result wins
    result = (
        _check_upload_rate_limit(ctx)
        or _submit_upload_for_approval(ctx)
    )

    return result


# =============================================================================
# Grant Session MCP Tools
# =============================================================================

def mcp_tool_request_grant(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_grant — 批次申請命令執行權限"""
    try:
        from grant import create_grant_request

        commands = arguments.get('commands', [])
        reason = str(arguments.get('reason', '')).strip()
        source = arguments.get('source', None)
        account_id = arguments.get('account', None)
        ttl_minutes = arguments.get('ttl_minutes', None)
        allow_repeat = arguments.get('allow_repeat', False)

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

        result = create_grant_request(
            commands=commands,
            reason=reason,
            source=source,
            account_id=account_id,
            ttl_minutes=ttl_minutes,
            allow_repeat=allow_repeat,
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
            )
        except Exception as e:
            print(f"[GRANT] Failed to send notification: {e}")

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
    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')


def mcp_tool_grant_status(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_grant_status — 查詢 Grant Session 狀態"""
    try:
        from grant import get_grant_status

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

    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')


def mcp_tool_revoke_grant(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_revoke_grant — 撤銷 Grant Session"""
    try:
        from grant import revoke_grant

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

    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')
