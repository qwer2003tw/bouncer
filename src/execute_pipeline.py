"""
Bouncer - Execute Pipeline

All pipeline check functions (_check_*) and risk scoring.
"""

import json
import time
import urllib.error
from typing import Optional

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from config_store import _is_silent_source
from execute_context import ExecuteContext
from execute_helpers import _safe_risk_category, _safe_risk_factors
from utils import mcp_result, generate_request_id, log_decision, generate_display_summary, record_execution_error, extract_exit_code
from commands import get_block_reason, is_auto_approve, execute_command, execute_boto3_native
from paging import store_paged_output
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit
from trust import (
    increment_trust_command_count, should_trust_approve, track_command_executed,
    TrustRateExceeded,
)
from db import table
from notifications import (
    send_approval_request,
    send_trust_auto_approve_notification,
    send_grant_execute_notification,
    send_blocked_notification,
    _should_throttle_notification,
)
from telegram import send_telegram_message_silent, escape_markdown
from metrics import emit_metric
from constants import (
    RATE_LIMIT_WINDOW,
    TRUST_SESSION_MAX_COMMANDS,
    APPROVAL_TTL_BUFFER,
    GRANT_SESSION_ENABLED,
    TELEGRAM_PAGE_SIZE,
)
from compliance_checker import check_compliance  # noqa: E402
from constants import PENDING_REMINDER_MINUTES  # noqa: E402
from grant import (  # noqa: E402
    get_grant_session,
    is_command_in_grant,
    normalize_command,
    try_use_grant_command,
)
from notifications import post_notification_setup  # noqa: E402
from risk_scorer import load_risk_rules  # noqa: E402
from scheduler_service import get_scheduler_service  # noqa: E402
from smart_approval import evaluate_command  # noqa: E402
from template_scanner import scan_command_payloads  # noqa: E402

logger = Logger(service="bouncer")


def _score_risk(ctx: ExecuteContext) -> None:
    """Smart Approval Shadow Mode — score risk, log to DynamoDB.

    Mutates ctx.smart_decision in-place.  Never raises.
    """
    try:
        ctx.smart_decision = evaluate_command(
            command=ctx.command,
            reason=ctx.reason,
            source=ctx.source or 'unknown',
            account_id=ctx.account_id,
            enable_sequence_analysis=False,
        )
    except Exception as e:  # noqa: BLE001 — shadow smart approval, non-blocking
        logger.exception("Smart approval error: %s", e, extra={"src_module": "shadow", "operation": "score_risk", "error": str(e)})


def _scan_template(ctx: ExecuteContext) -> None:
    """Layer 2.5: Template scan — detect HIGH/CRITICAL risks in inline JSON payloads.

    Mutates ctx.template_scan_result in-place.  Never raises.
    Result shape:
        {
            'max_score': int,          # 0–100
            'hit_count': int,
            'severity': str,           # 'none' | 'low' | 'medium' | 'high' | 'critical'
            'factors': [...],          # list of RiskFactor details
            'escalate': bool,          # True when HIGH or CRITICAL → force MANUAL
        }
    """
    ctx.template_scan_result = {
        'max_score': 0,
        'hit_count': 0,
        'severity': 'none',
        'factors': [],
        'escalate': False,
    }
    try:

        rules = load_risk_rules()
        template_rules = rules.template_rules if hasattr(rules, 'template_rules') else []

        max_score, factors = scan_command_payloads(ctx.command, template_rules)

        hit_count = len(factors)

        # Severity buckets (align with risk_scorer thresholds)
        if max_score >= 90:
            severity = 'critical'
        elif max_score >= 75:
            severity = 'high'
        elif max_score >= 50:
            severity = 'medium'
        elif max_score > 0:
            severity = 'low'
        else:
            severity = 'none'

        escalate = severity in ('high', 'critical')

        ctx.template_scan_result = {
            'max_score': max_score,
            'hit_count': hit_count,
            'severity': severity,
            'factors': [
                {'name': f.name, 'details': f.details, 'score': f.raw_score}
                for f in factors
            ],
            'escalate': escalate,
        }

        if escalate:
            logger.info(
                "Escalating to MANUAL: %d hits, max_score=%d, severity=%s",
                hit_count, max_score, severity,
                extra={"src_module": "execute", "operation": "scan_template", "hit_count": hit_count, "max_score": max_score, "severity": severity},
            )

    except ImportError as e:
        logger.warning("template_scanner or load_risk_rules not available: %s", e, extra={"src_module": "execute", "operation": "scan_template", "error": str(e)})
    except (ValueError, TypeError, OSError) as e:
        logger.exception("Template scan error (non-fatal): %s", e, extra={"src_module": "execute", "operation": "scan_template", "error": str(e)})


def _check_compliance(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 0: compliance check — blocks on security-rule violations."""
    try:
        is_compliant, violation = check_compliance(ctx.command)
        if not is_compliant:
            logger.warning("Compliance blocked: %s - %s", violation.rule_id, violation.rule_name, extra={"src_module": "execute", "operation": "check_compliance", "rule_id": violation.rule_id, "rule_name": violation.rule_name})
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
                agent_id=ctx.agent_id,
                verified_identity=ctx.verified_identity,
            )
            return mcp_result(ctx.req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'compliance_violation',
                        'error_code': 'COMPLIANCE_BLOCKED',
                        'rule_id': violation.rule_id,
                        'rule_name': violation.rule_name,
                        'description': violation.description,
                        'remediation': violation.remediation,
                        'command': ctx.command[:200],
                        'suggestion': f'命令被安全規則 {violation.rule_id} 攔截。{violation.remediation}'
                    })
                }],
                'isError': True
            })
    except ImportError:
        logger.exception("compliance_checker module import failed - failing closed", extra={"src_module": "execute", "operation": "check_compliance"})
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'error',
                    'error_code': 'SYSTEM_ERROR',
                    'error': 'Compliance checker module unavailable - request rejected for safety',
                    'suggestion': '請聯繫管理員檢查 compliance_checker 模組是否正常部署'
                })
            }],
            'isError': True
        })
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
            agent_id=ctx.agent_id,
            verified_identity=ctx.verified_identity,
        )
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'blocked',
                    'error_code': 'BLOCKED_COMMAND',
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
        matched = is_command_in_grant(normalized_cmd, grant)

        logger.info(
            "Grant session check result",
            extra={
                "src_module": "mcp_execute",
                "operation": "_check_grant_session",
                "grant_id": grant_id,
                "matched": matched,
            }
        )

        if not matched:
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

        # 執行命令 (使用 grant 的 assume_role_arn，fallback 到 account role)
        grant_req_id = generate_request_id(ctx.command)
        grant_assume_role = grant.get('assume_role_arn') or ctx.assume_role
        # Execute: use native boto3 or traditional awscli
        if ctx.is_native:
            result = execute_boto3_native(
                service=ctx.native_service,
                operation=ctx.native_operation,
                params=ctx.native_params,
                region=ctx.native_region,
                assume_role_arn=grant_assume_role,
            )
        else:
            result = execute_command(ctx.command, grant_assume_role, cli_input_json=ctx.cli_input_json)
        _exit_code = extract_exit_code(result)
        is_failed = _exit_code is not None and _exit_code != 0
        cmd_status = 'error' if is_failed else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'grant'})
        paged = store_paged_output(grant_req_id, result)

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
            request_id=grant_req_id,
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
            command_status='failed' if is_failed else 'success',
            agent_id=ctx.agent_id,
            verified_identity=ctx.verified_identity,
        )

        # Record execution error to DDB if command failed (sprint9-001)
        if is_failed:
            record_execution_error(table, grant_req_id, exit_code=_exit_code, error_output=result)

        response_data = {
            'status': 'grant_auto_approved',
            'request_id': grant_req_id,
            'command': ctx.command,
            'account': ctx.account_id,
            'account_name': ctx.account_name,
            'result': paged.result,  # full result for MCP caller
            'grant_id': grant_id,
            'remaining': remaining_info,
        }
        if is_failed:
            response_data['exit_code'] = _exit_code
        if ctx.warnings:
            response_data['warnings'] = ctx.warnings

        # No paging metadata in MCP response (Sprint 83)

        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response_data)
            }]
        })

    except ClientError as e:
        # Grant 失敗不影響主流程 → fallthrough
        logger.exception("_check_grant_session error", extra={"src_module": "grant", "operation": "_check_grant_session", "error": str(e)})
        return None


def _check_auto_approve(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 2: safelist auto-approve — execute immediately."""
    if not is_auto_approve(ctx.command):
        return None

    request_id = generate_request_id(ctx.command)
    # Execute: use native boto3 or traditional awscli
    if ctx.is_native:
        result = execute_boto3_native(
            service=ctx.native_service,
            operation=ctx.native_operation,
            params=ctx.native_params,
            region=ctx.native_region,
            assume_role_arn=ctx.assume_role,
        )
    else:
        result = execute_command(ctx.command, ctx.assume_role, cli_input_json=ctx.cli_input_json)
    _exit_code = extract_exit_code(result)
    is_failed = _exit_code is not None and _exit_code != 0
    cmd_status = 'error' if is_failed else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'auto_approve'})
    paged = store_paged_output(request_id, result)

    # Silent Telegram notification for safelist auto-approve (sprint24-003: throttled)
    # Silent notification mode (#380): skip notification for configured sources
    notification_suppressed = _is_silent_source(ctx.source)

    if not notification_suppressed and not _should_throttle_notification('auto_approve'):
        try:
            reason_line = f"\U0001f4ac *原因：* {escape_markdown(ctx.reason or '(未填寫)')}\n" if ctx.reason else ""
            account_line = (
                f"\U0001f3e6 *帳號：* `{ctx.account_id}` ({escape_markdown(ctx.account_name or '')})\n"
                if ctx.account_id else ""
            )
            result_emoji = "❌" if is_failed else "✅"

            # Build header first to calculate remaining space for result
            header = (
                f"\u26a1 *自動執行*\n\n"
                f"\U0001f916 *來源：* {escape_markdown(ctx.source or '(unknown)')}\n"
                f"{account_line}"
                f"{reason_line}"
                f"\U0001f4cb *命令：*\n```\n{ctx.command[:300]}\n```\n\n"
                f"{result_emoji} *結果：*\n```\n"
            )
            footer = "\n```"

            # Telegram 4096 limit: use TELEGRAM_PAGE_SIZE to match DDB pages (no gap)
            max_result_chars = TELEGRAM_PAGE_SIZE  # 3800 chars

            result_text = result or '(無輸出)'
            result_preview = result_text[:max_result_chars].strip()

            _notif_text = f"{header}{result_preview}{footer}"

            # Add Next Page button if Telegram pages exist
            _reply_markup = None
            if paged.telegram_pages > 1:
                _reply_markup = {
                    'inline_keyboard': [[{
                        'text': f'📄 下一頁 (2/{paged.telegram_pages})',
                        'callback_data': f'show_page:{request_id}:2',
                    }]]
                }
            send_telegram_message_silent(_notif_text, reply_markup=_reply_markup)
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.warning("[EXECUTE] Result notification failed (non-critical)", exc_info=True, extra={"src_module": "execute", "operation": "auto_approve_notification"})
    else:
        if notification_suppressed:
            logger.info("Notification suppressed for silent source", extra={
                "src_module": "execute", "operation": "silent_source",
                "source": ctx.source, "command": ctx.command[:100]
            })
        else:
            logger.info("Skipped auto-approve notification for command: %s...", ctx.command[:50], extra={"src_module": "execute", "operation": "auto_approve_notification"})

    log_decision(
        table=table,
        request_id=request_id,
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
        command_status='failed' if is_failed else 'success',
        result=result,  # store full result, not paged first page
        notification_suppressed=notification_suppressed,
        agent_id=ctx.agent_id,
        verified_identity=ctx.verified_identity,
    )

    # Record execution error to DDB if command failed (sprint9-001)
    if is_failed:
        record_execution_error(table, request_id, exit_code=_exit_code, error_output=result)

    response_data = {
        'status': 'auto_approved',
        'request_id': request_id,
        'command': ctx.command,
        'account': ctx.account_id,
        'account_name': ctx.account_name,
        'result': paged.result,  # full result
    }
    if is_failed:
        response_data['exit_code'] = _exit_code
    if ctx.warnings:
        response_data['warnings'] = ctx.warnings

    # No paging metadata in MCP response (Sprint 83)

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
        logger.warning("Rate limited", extra={
            "src_module": "mcp_execute", "operation": "rate_limited",
            "source": ctx.source or 'unknown',
            "bot_id": ctx.bot_id,
            "limit_type": "rate",
        })
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'rate_limited',
                    'error_code': 'RATE_LIMITED',
                    'error': str(e),
                    'command': ctx.command,
                    'retry_after': RATE_LIMIT_WINDOW,
                    'suggestion': f'請等待 {RATE_LIMIT_WINDOW} 秒後再試，或等待現有請求完成'
                })
            }],
            'isError': True
        })
    except PendingLimitExceeded as e:
        logger.warning("Rate limited", extra={
            "src_module": "mcp_execute", "operation": "rate_limited",
            "source": ctx.source or 'unknown',
            "bot_id": ctx.bot_id,
            "limit_type": "pending",
        })
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'pending_limit_exceeded',
                    'error_code': 'PENDING_LIMIT_EXCEEDED',
                    'error': str(e),
                    'command': ctx.command,
                    'suggestion': '請等待現有審批請求處理完成後再試，或拒絕/批准現有請求'
                })
            }],
            'isError': True
        })
    return None


def _check_trust_session(ctx: ExecuteContext) -> Optional[dict]:
    """Trust session auto-approve — execute if trusted."""
    should_trust, trust_session, trust_reason = should_trust_approve(
        ctx.command, ctx.trust_scope, ctx.account_id, source=ctx.source or '', caller_ip=ctx.caller_ip
    )
    if not (should_trust and trust_session):
        return None

    # 增加命令計數 (s59-002: catch rate exceeded)
    try:
        new_count = increment_trust_command_count(trust_session['request_id'])
    except TrustRateExceeded as exc:
        logger.warning("Trust rate exceeded: %s", exc, extra={"src_module": "mcp_execute", "operation": "trust_auto_approve", "trust_id": trust_session['request_id']})
        emit_metric('Bouncer', 'TrustRateExceeded', 1, dimensions={'Event': 'rate_exceeded'})
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'error',
                    'error_code': 'TRUST_RATE_EXCEEDED',
                    'error': f'信任時段命令速率過高，請稍候再試。{str(exc)}',
                    'suggestion': '等待幾秒後重試，或降低命令執行頻率'
                })
            }],
            'isError': True
        })

    # 執行命令
    request_id = generate_request_id(ctx.command)
    # Execute: use native boto3 or traditional awscli
    if ctx.is_native:
        result = execute_boto3_native(
            service=ctx.native_service,
            operation=ctx.native_operation,
            params=ctx.native_params,
            region=ctx.native_region,
            assume_role_arn=ctx.assume_role,
        )
    else:
        result = execute_command(ctx.command, ctx.assume_role, cli_input_json=ctx.cli_input_json)
    _exit_code = extract_exit_code(result)
    is_failed = _exit_code is not None and _exit_code != 0
    cmd_status = 'error' if is_failed else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'trust'})
    paged = store_paged_output(request_id, result)

    # 計算剩餘時間
    remaining = int(trust_session.get('expires_at', 0)) - int(time.time())
    remaining_str = f"{remaining // 60}:{remaining % 60:02d}" if remaining > 0 else "0:00"

    # 發送靜默通知
    send_trust_auto_approve_notification(
        ctx.command, trust_session['request_id'], remaining_str, new_count, result,
        source=ctx.source,
        reason=ctx.reason,
    )

    log_decision(
        table=table,
        request_id=request_id,
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
        command_status='failed' if is_failed else 'success',
        agent_id=ctx.agent_id,
        verified_identity=ctx.verified_identity,
    )

    # Record execution error to DDB if command failed (sprint9-001)
    if is_failed:
        record_execution_error(table, request_id, exit_code=_exit_code, error_output=result)

    # Track command in trust session for end-of-session summary (sprint9-007-phase-a)
    track_command_executed(trust_session['request_id'], ctx.command, not is_failed)

    response_data = {
        'status': 'trust_auto_approved',
        'request_id': request_id,
        'command': ctx.command,
        'account': ctx.account_id,
        'account_name': ctx.account_name,
        'result': paged.result,  # full result for MCP caller
        'trust_session': trust_session['request_id'],
        'remaining': remaining_str,
        'command_count': f"{new_count}/{TRUST_SESSION_MAX_COMMANDS}"
    }
    if is_failed:
        response_data['exit_code'] = _exit_code
    if ctx.warnings:
        response_data['warnings'] = ctx.warnings

    # No paging metadata in MCP response (Sprint 83)

    return mcp_result(ctx.req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps(response_data)
        }]
    })


def _submit_for_approval(ctx: ExecuteContext) -> dict:
    """Layer 3: submit for human approval — always returns a result."""

    request_id = generate_request_id(ctx.command)
    now = int(time.time())
    approval_expiry = now + ctx.timeout  # 審批到期時間（通常 300-600 秒）
    ttl = now + ctx.timeout + APPROVAL_TTL_BUFFER

    # Preview for logging
    command_preview = ctx.command[:100] + '...' if len(ctx.command) > 100 else ctx.command
    logger.info(
        "Submitting command for approval",
        extra={
            "src_module": "mcp_execute",
            "operation": "_submit_for_approval",
            "request_id": request_id,
            "command_preview": command_preview,
        }
    )

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
        'created_at': now,
        'ttl': ttl,
        'approval_expiry': approval_expiry,
        'mode': 'mcp',
        'display_summary': generate_display_summary('execute', command=ctx.command),
    }
    if ctx.smart_decision:
        from decimal import Decimal as _Dec
        item['risk_score'] = _Dec(str(ctx.smart_decision.final_score))
        item['risk_category'] = _safe_risk_category(ctx.smart_decision) or ''
        item['risk_factors'] = _safe_risk_factors(ctx.smart_decision) or []
        item['decision_type'] = 'pending'  # 會在 callback 時更新
    # Store native execution info for callback to use boto3 instead of awscli
    if ctx.is_native:
        import json as _json
        item['action_type'] = 'native'
        item['native_service'] = ctx.native_service or ''
        item['native_operation'] = ctx.native_operation or ''
        item['native_params'] = _json.dumps(ctx.native_params or {})
        item['native_region'] = ctx.native_region or ''
    table.put_item(Item=item)

    # 發送 Telegram 審批請求
    # 若發送失敗，刪除剛寫入的 DynamoDB record，避免產生孤兒審批請求
    try:
        notified = send_approval_request(
            request_id, ctx.command, ctx.reason, ctx.timeout, ctx.source,
            ctx.account_id, ctx.account_name, context=ctx.context,
            template_scan_result=ctx.template_scan_result,
        )
        if not notified.ok:
            raise RuntimeError("Telegram notification returned failure (ok=False or empty response)")
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, RuntimeError, Exception) as tg_err:
        # Cleanup DDB to prevent orphan pending record
        try:
            table.delete_item(Key={'request_id': request_id})
        except ClientError as del_err:
            logger.exception("Failed to delete DDB record %s: %s", request_id, del_err, extra={"src_module": "execute", "operation": "orphan_cleanup", "request_id": request_id, "error": str(del_err)})
        logger.exception("Telegram notification failed for %s: %s", request_id, tg_err, extra={"src_module": "execute", "operation": "orphan_cleanup", "request_id": request_id, "error": str(tg_err)})
        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'error',
                    'error_code': 'NOTIFICATION_FAILED',
                    'error': 'Telegram notification failed; approval request was not created. Please retry.',
                    'detail': str(tg_err),
                    'suggestion': '請重試，或聯繫管理員檢查 Telegram Bot 設定是否正常'
                })
            }],
            'isError': True,
        })

    # Post-notification: store telegram_message_id + schedule expiry cleanup + warning
    if notified.message_id:
        post_notification_setup(
            request_id=request_id,
            telegram_message_id=notified.message_id,
            expires_at=ttl,
        )

        # S35-003: Schedule expiry warning (60s before TTL) — best-effort, never block approval flow
        try:
            svc = get_scheduler_service()
            svc.create_expiry_warning_schedule(
                request_id=request_id,
                expires_at=ttl,
                command_preview=ctx.command[:100],
                source=ctx.source or '',
            )
        except Exception as _e:  # noqa: BLE001 — best-effort, never block approval flow
            logger.debug("post-execute notify ignored: %s", _e, extra={"src_module": "mcp_execute", "operation": "post_execute_notify"})

        # S59-001: Schedule pending approval reminder — best-effort, never block approval flow
        try:
            svc = get_scheduler_service()
            svc.create_pending_reminder_schedule(
                request_id=request_id,
                expires_at=ttl,
                reminder_minutes=PENDING_REMINDER_MINUTES,
                command_preview=ctx.command[:100],
                source=ctx.source or '',
            )
        except Exception as _e:  # noqa: BLE001 — best-effort, never block approval flow
            logger.debug("pending reminder schedule ignored: %s", _e, extra={"src_module": "mcp_execute", "operation": "create_pending_reminder"})

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
