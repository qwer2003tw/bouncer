"""
Bouncer - Execute Pipeline

ExecuteContext + all _check_* functions + mcp_tool_execute()
Also includes grant-session tools: request_grant, grant_status, revoke_grant.
"""

import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional


from utils import mcp_result, mcp_error, generate_request_id, log_decision, generate_display_summary
from commands import get_block_reason, is_auto_approve, execute_command
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id,
)
from paging import store_paged_output
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit
from trust import (
    increment_trust_command_count, should_trust_approve,
)
from db import table
from notifications import (
    send_approval_request,
    send_trust_auto_approve_notification,
    send_grant_request_notification,
    send_grant_execute_notification,
    send_blocked_notification,
)
from telegram import send_telegram_message_silent, escape_markdown
from metrics import emit_metric
from constants import (
    DEFAULT_ACCOUNT_ID, MCP_MAX_WAIT, RATE_LIMIT_WINDOW,
    TRUST_SESSION_MAX_COMMANDS,
    APPROVAL_TTL_BUFFER,
    AUDIT_TTL_SHORT,
    GRANT_SESSION_ENABLED,
)


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
    trust_scope: str
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
    trust_scope = str(arguments.get('trust_scope', '')).strip()
    context = arguments.get('context', None)
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
    timeout = min(int(arguments.get('timeout', MCP_MAX_WAIT)), MCP_MAX_WAIT)
    sync_mode = arguments.get('sync', False)

    if not command:
        return mcp_error(req_id, -32602, 'Missing required parameter: command')

    if not trust_scope:
        return mcp_error(req_id, -32602, 'Missing required parameter: trust_scope (use session key or stable ID)')

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
        trust_scope=trust_scope,
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
    """Extract actual decision from pipeline result for shadow comparison.

    Pipeline returns: {'statusCode': 200, 'body': '{"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"{\\"status\\":\\"auto_approved\\",...}"}]}}'}
    """
    try:
        body = result.get('body', '{}')
        if isinstance(body, str):
            body = json.loads(body)
        # MCP result path
        content = body.get('result', {}).get('content', [])
        if not content:
            # MCP error path
            if 'error' in body:
                return 'error'
            # REST path (body is the response directly)
            status = body.get('status', '')
            if status:
                return _map_status_to_decision(status)
            return 'unknown'
        text = content[0].get('text', '{}')
        data = json.loads(text)
        status = data.get('status', '')
        return _map_status_to_decision(status)
    except Exception:
        return 'unknown'


def _map_status_to_decision(status: str) -> str:
    """Map pipeline status to comparable decision label."""
    mapping = {
        'auto_approved': 'auto_approve',
        'blocked': 'blocked',
        'compliance_violation': 'blocked',
        'pending_approval': 'needs_approval',
        'trust_auto_approved': 'auto_approve',
        'grant_auto_approved': 'auto_approve',
    }
    return mapping.get(status, status or 'unknown')


def _check_compliance(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 0: compliance check — blocks on security-rule violations."""
    try:
        from compliance_checker import check_compliance
        is_compliant, violation = check_compliance(ctx.command)
        if not is_compliant:
            print(f"[COMPLIANCE] Blocked: {violation.rule_id} - {violation.rule_name}")
            emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'compliance'})
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
    block_reason = get_block_reason(ctx.command)
    if block_reason:
        send_blocked_notification(ctx.command, block_reason, ctx.source)
        emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'blocked'})
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
                    'error': '命令被安全規則封鎖',
                    'block_reason': block_reason,
                    'command': ctx.command[:200],
                    'suggestion': '如果需要執行此操作，請聯繫管理員或使用替代方案',
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
        cmd_status = 'error' if result.startswith('❌') else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'grant'})
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
    cmd_status = 'error' if result.startswith('❌') else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'auto_approve'})
    paged = store_paged_output(generate_request_id(ctx.command), result)

    # Silent Telegram notification for safelist auto-approve
    try:
        result_preview = (result[:300] if result else '(無輸出)').strip()
        _notif_text = (
            f"⚡ *自動執行*\n\n"
            f"🤖 *來源：* {escape_markdown(ctx.source or '(unknown)')}\n"
            f"📋 *命令：*\n```\n{ctx.command[:300]}\n```\n\n"
            f"✅ *結果：*\n```\n{result_preview}\n```"
        )
        send_telegram_message_silent(_notif_text)
    except Exception:
        pass  # Notification failure must not break execution

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
        ctx.command, ctx.trust_scope, ctx.account_id
    )
    if not (should_trust and trust_session):
        return None

    # 增加命令計數
    new_count = increment_trust_command_count(trust_session['request_id'])

    # 執行命令
    result = execute_command(ctx.command, ctx.assume_role)
    cmd_status = 'error' if result.startswith('❌') else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'trust'})
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
        'trust_scope': ctx.trust_scope,
        'context': ctx.context or '',
        'account_id': ctx.account_id,
        'account_name': ctx.account_name,
        'assume_role': ctx.assume_role,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp',
        'display_summary': generate_display_summary('execute', command=ctx.command),
    }
    if ctx.smart_decision:
        from decimal import Decimal as _Dec
        item['risk_score'] = _Dec(str(ctx.smart_decision.final_score))
        item['risk_category'] = _safe_risk_category(ctx.smart_decision) or ''
        item['risk_factors'] = _safe_risk_factors(ctx.smart_decision) or []
        item['decision_type'] = 'pending'  # 會在 callback 時更新
    table.put_item(Item=item)

    # 發送 Telegram 審批請求
    # 若發送失敗，刪除剛寫入的 DynamoDB record，避免產生孤兒審批請求
    try:
        notified = send_approval_request(
            request_id, ctx.command, ctx.reason, ctx.timeout, ctx.source,
            ctx.account_id, ctx.account_name, context=ctx.context
        )
        if not notified:
            raise RuntimeError("Telegram notification returned failure (ok=False or empty response)")
    except Exception as tg_err:
        # Cleanup DDB to prevent orphan pending record
        try:
            table.delete_item(Key={'request_id': request_id})
        except Exception as del_err:
            print(f"[ORPHAN CLEANUP] Failed to delete DDB record {request_id}: {del_err}")
        print(f"[ORPHAN CLEANUP] Telegram notification failed for {request_id}: {tg_err}")
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'error',
                    'error': 'Telegram notification failed; approval request was not created. Please retry.',
                    'detail': str(tg_err),
                })
            }],
            'isError': True,
        })

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
