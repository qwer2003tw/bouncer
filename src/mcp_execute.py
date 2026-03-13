"""
Bouncer - Execute Pipeline

ExecuteContext + all _check_* functions + mcp_tool_execute()
Also includes grant-session tools: request_grant, grant_status, revoke_grant.
"""

import json
import os
import re
import secrets
import time
import urllib.error
from dataclasses import dataclass
from typing import Optional

from botocore.exceptions import ClientError



from utils import mcp_result, mcp_error, generate_request_id, log_decision, generate_display_summary, record_execution_error, extract_exit_code
from commands import get_block_reason, is_auto_approve, execute_command, _split_chain
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id,
)
from paging import store_paged_output
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit
from trust import (
    increment_trust_command_count, should_trust_approve, track_command_executed,
)
from db import table
from notifications import (
    send_approval_request,
    send_trust_auto_approve_notification,
    send_grant_request_notification,
    send_grant_execute_notification,
    send_blocked_notification,
    _should_throttle_notification,
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
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")


# Shadow mode 表名（用於收集智慧審批數據）
SHADOW_TABLE_NAME = os.environ.get('SHADOW_TABLE', 'bouncer-shadow-approvals')


# =============================================================================
# SEC-003: Unicode 正規化
# =============================================================================

# 零寬 / 不可見字元（直接移除）
_INVISIBLE_CHARS_RE = re.compile(
    r'[\u200b\u200c\u200d\ufeff\u2060\u180e\u00ad]'
)

# Unicode 空白字元（替換為普通空白）
_UNICODE_SPACE_RE = re.compile(
    r'[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\u2003\u2002\u2001]'
)


def _normalize_command(cmd: str) -> str:
    """
    SEC-003: 正規化命令字串，防止 Unicode 注入繞過：
    1. 移除零寬 / 不可見字元
    2. 替換 Unicode 空白為普通空白
    3. 折疊多餘空白
    4. strip 前後空白
    """
    if not cmd:
        return cmd
    # 1. 移除不可見字元
    cmd = _INVISIBLE_CHARS_RE.sub('', cmd)
    # 2. Unicode 空白 → 普通空白
    cmd = _UNICODE_SPACE_RE.sub(' ', cmd)
    # 3. 折疊多餘空白
    cmd = re.sub(r' {2,}', ' ', cmd)
    # 4. strip
    return cmd.strip()


def _safe_risk_category(smart_decision):
    """安全取得 risk category 值（相容 enum 和 string）"""
    if not smart_decision:
        return None
    try:
        cat = smart_decision.risk_result.category
        return cat.value if hasattr(cat, 'value') else cat
    except (AttributeError, KeyError) as e:
        logger.warning("Failed to extract risk category: %s", e, extra={"src_module": "execute", "operation": "safe_risk_category", "error": str(e)})
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
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning("Failed to extract/convert risk factors: %s", e, extra={"src_module": "execute", "operation": "safe_risk_factors", "error": str(e)})
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
        logger.info("Shadow logged: %s -> %s (score=%s, actual=%s)", shadow_id, smart_decision.decision, smart_decision.final_score, actual_decision, extra={"src_module": "shadow", "operation": "log_shadow", "shadow_id": shadow_id, "decision": smart_decision.decision})
    except ClientError as e:
        # Shadow 記錄失敗不影響主流程
        logger.error("Shadow log failed: %s", e, extra={"src_module": "shadow", "operation": "log_shadow", "error": str(e)})


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
    template_scan_result: Optional[dict] = None  # Layer 2.5 template scan result


def _parse_execute_request(req_id, arguments: dict) -> 'dict | ExecuteContext':
    """Parse and validate execute request arguments.

    Returns an ExecuteContext on success, or an MCP error/result dict on
    validation failure (caller should return immediately).
    """
    command = str(arguments.get('command', '')).strip()
    command = _normalize_command(command)  # SEC-003: normalize unicode before any check
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
        return mcp_error(req_id, -32602, (
            'Missing required parameter: trust_scope\n\n'
            'trust_scope is a stable caller identifier used for trust session matching.\n'
            'Examples:\n'
            '  - "private-bot-main"        (for general usage)\n'
            '  - "private-bot-deploy"      (for deployment tasks)\n'
            '  - "private-bot-kubectl"     (for kubectl operations)\n\n'
            'Use a consistent value per bot/task to enable trust session auto-approval.'
        ))

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
    except Exception as e:  # noqa: BLE001 — shadow smart approval, non-blocking
        logger.error("Smart approval error: %s", e, extra={"src_module": "shadow", "operation": "score_risk", "error": str(e)})


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
        from template_scanner import scan_command_payloads
        from risk_scorer import load_risk_rules

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
        logger.error("Template scan error (non-fatal): %s", e, extra={"src_module": "execute", "operation": "scan_template", "error": str(e)})


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
    except (json.JSONDecodeError, KeyError, TypeError, IndexError) as e:
        logger.warning("Failed to parse response decision: %s", e, extra={"src_module": "execute", "operation": "extract_decision", "error": str(e)})
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
        grant_req_id = generate_request_id(ctx.command)
        result = execute_command(ctx.command, ctx.assume_role)
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
            'result': paged['result'],
            'grant_id': grant_id,
            'remaining': remaining_info,
        }
        if is_failed:
            response_data['exit_code'] = _exit_code

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

    except ClientError as e:
        # Grant 失敗不影響主流程 → fallthrough
        logger.error("_check_grant_session error", extra={"src_module": "grant", "operation": "_check_grant_session", "error": str(e)})
        return None


def _check_auto_approve(ctx: ExecuteContext) -> Optional[dict]:
    """Layer 2: safelist auto-approve — execute immediately."""
    if not is_auto_approve(ctx.command):
        return None

    request_id = generate_request_id(ctx.command)
    result = execute_command(ctx.command, ctx.assume_role)
    _exit_code = extract_exit_code(result)
    is_failed = _exit_code is not None and _exit_code != 0
    cmd_status = 'error' if is_failed else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'auto_approve'})
    paged = store_paged_output(request_id, result)

    # Silent Telegram notification for safelist auto-approve (sprint24-003: throttled)
    if not _should_throttle_notification('auto_approve'):
        try:
            result_preview = (result[:300] if result else '(無輸出)').strip()
            reason_line = f"\U0001f4ac *原因：* {escape_markdown(ctx.reason or '(未填寫)')}\n" if ctx.reason else ""
            _notif_text = (
                f"\u26a1 *自動執行*\n\n"
                f"\U0001f916 *來源：* {escape_markdown(ctx.source or '(unknown)')}\n"
                f"{reason_line}"
                f"\U0001f4cb *命令：*\n```\n{ctx.command[:300]}\n```\n\n"
                f"\u2705 *結果：*\n```\n{result_preview}\n```"
            )
            send_telegram_message_silent(_notif_text)
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.warning("[EXECUTE] Result notification failed (non-critical)", exc_info=True, extra={"src_module": "execute", "operation": "auto_approve_notification"})
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
        'result': paged['result']
    }
    if is_failed:
        response_data['exit_code'] = _exit_code

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
    request_id = generate_request_id(ctx.command)
    result = execute_command(ctx.command, ctx.assume_role)
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
        'result': paged['result'],
        'trust_session': trust_session['request_id'],
        'remaining': remaining_str,
        'command_count': f"{new_count}/{TRUST_SESSION_MAX_COMMANDS}"
    }
    if is_failed:
        response_data['exit_code'] = _exit_code

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
            logger.error("Failed to delete DDB record %s: %s", request_id, del_err, extra={"src_module": "execute", "operation": "orphan_cleanup", "request_id": request_id, "error": str(del_err)})
        logger.error("Telegram notification failed for %s: %s", request_id, tg_err, extra={"src_module": "execute", "operation": "orphan_cleanup", "request_id": request_id, "error": str(tg_err)})
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

    # Post-notification: store telegram_message_id + schedule expiry cleanup + warning
    if notified.message_id:
        from notifications import post_notification_setup
        post_notification_setup(
            request_id=request_id,
            telegram_message_id=notified.message_id,
            expires_at=ttl,
        )

        # S35-003: Schedule expiry warning (60s before TTL) — best-effort, never block approval flow
        try:
            from scheduler_service import get_scheduler_service
            svc = get_scheduler_service()
            svc.create_expiry_warning_schedule(
                request_id=request_id,
                expires_at=ttl,
                command_preview=ctx.command[:100],
                source=ctx.source or '',
            )
        except Exception:  # noqa: BLE001 — best-effort, never block approval flow
            pass

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
# Chain Risk Pre-Check
# =============================================================================

def _check_chain_risks(ctx: ExecuteContext) -> Optional[dict]:
    """Pre-validate all sub-commands in a && chain before execution.

    Each sub-command is risk-checked individually (blocked / compliance).
    If any sub-command fails a risk check, the entire chain is rejected
    and the problematic sub-command is identified in the response.

    Returns None when all sub-commands pass (chain may proceed), or an
    MCP result/error dict when the chain should be aborted.
    """
    sub_cmds = _split_chain(ctx.command)
    if len(sub_cmds) <= 1:
        return None  # single command — normal pipeline handles it

    for sub_cmd in sub_cmds:
        sub_cmd = sub_cmd.strip()
        if not sub_cmd:
            continue

        # Layer -1: validate that all sub-commands are AWS CLI commands
        # This prevents misleading errors when first command succeeds but second fails
        from commands import aws_cli_split
        args = aws_cli_split(sub_cmd)
        if not args or args[0] != 'aws':
            non_aws_cmd = args[0] if args else '(empty)'
            logger.warning("Non-AWS command in chain: %s", non_aws_cmd, extra={"src_module": "execute", "operation": "check_chain_risks", "non_aws_cmd": non_aws_cmd})
            emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'chain_non_aws'})
            return mcp_result(ctx.req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'validation_error',
                        'error': f'❌ 命令包含非 AWS CLI 指令 ({non_aws_cmd})，Bouncer 只支援 aws 命令串接。',
                        'remediation': '請拆成獨立命令分別執行，確認第一個命令成功後再執行下一個。',
                        'command': ctx.command[:200],
                        'failed_sub_command': sub_cmd[:200],
                    })
                }],
                'isError': True
            })

        # Layer 0: compliance check per sub-command
        try:
            from compliance_checker import check_compliance
            is_compliant, violation = check_compliance(sub_cmd)
            if not is_compliant:
                logger.warning("Compliance violation in sub-command: %s", sub_cmd[:100], extra={"src_module": "execute", "operation": "check_chain_risks", "sub_cmd": sub_cmd[:100]})
                emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'chain_compliance'})
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
                            'failed_sub_command': sub_cmd[:200],
                        })
                    }],
                    'isError': True
                })
        except ImportError:
            pass

        # Layer 1: blocked check per sub-command
        block_reason = get_block_reason(sub_cmd)
        if block_reason:
            logger.warning("Blocked sub-command: %s", sub_cmd[:100], extra={"src_module": "execute", "operation": "check_chain_risks", "sub_cmd": sub_cmd[:100]})
            send_blocked_notification(sub_cmd, block_reason, ctx.source)
            emit_metric('Bouncer', 'BlockedCommand', 1, dimensions={'Reason': 'chain_blocked'})
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
                        'error': '串接命令中有子命令被安全規則封鎖',
                        'block_reason': block_reason,
                        'command': ctx.command[:200],
                        'failed_sub_command': sub_cmd[:200],
                        'suggestion': '如果需要執行此操作，請聯繫管理員或使用替代方案',
                    })
                }],
                'isError': True
            })

    return None  # all sub-commands passed


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

    # Phase 2.5: Template scan — escalate to MANUAL on HIGH/CRITICAL hits
    _scan_template(ctx)

    # Phase 2.6: Chain risk pre-check — validate each sub-command individually
    # when command contains && (reject entire chain if any sub-command is blocked)
    chain_check = _check_chain_risks(ctx)
    if chain_check is not None:
        return chain_check

    # Phase 3: Pipeline — first non-None result wins
    # NOTE: if template_scan says escalate, skip auto_approve and trust layers
    if ctx.template_scan_result and ctx.template_scan_result.get('escalate'):
        result = (
            _check_compliance(ctx)
            or _check_blocked(ctx)
            or _check_rate_limit(ctx)
            or _submit_for_approval(ctx)
        )
    else:
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
        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
            logger.error(f"[GRANT] Failed to send notification: {e}", extra={"src_module": "grant", "operation": "send_notification", "error": str(e)})
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
    except Exception as e:  # noqa: BLE001 — MCP tool entry point
        logger.exception(f"[MCP] request_grant error: {e}")
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

    except Exception as e:  # noqa: BLE001 — MCP tool entry point
        logger.exception(f"[MCP] grant_status error: {e}")
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

    except Exception as e:  # noqa: BLE001 — MCP tool entry point
        logger.exception(f"[MCP] revoke_grant error: {e}")
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')


def mcp_tool_grant_execute(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_grant_execute — 在 Grant Session 內執行命令（fail-fast）"""
    try:
        # 1. 解析必填參數
        grant_id = str(arguments.get('grant_id', '')).strip()
        command = str(arguments.get('command', '')).strip()
        source = str(arguments.get('source', '')).strip()
        reason = str(arguments.get('reason', 'Grant execute')).strip()
        account_param = str(arguments.get('account', '')).strip() if arguments.get('account') else None

        if not grant_id or not command or not source:
            return mcp_error(req_id, -32602, 'Missing required parameter: grant_id, command, source')

        # 2. 命令正規化（SEC-003: unicode normalize）
        normalized_cmd = _normalize_command(command)

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
        from grant import get_grant_session, is_command_in_grant, try_use_grant_command, normalize_command

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

        # 9. compliance_checker（安全優先，即使是 grant 核准的命令也要通過）
        from compliance_checker import check_compliance

        is_compliant, violation = check_compliance(normalized_cmd)
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

        # 10. 命令在白名單？
        if not is_command_in_grant(normalize_command(normalized_cmd), grant):
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'command_not_in_grant',
                        'message': 'Command is not in the approved grant list'
                    })
                }],
                'isError': True
            })

        # 11. allow_repeat 檢查（pre-check 以提供具體錯誤訊息）
        allow_repeat = grant.get('allow_repeat', False)
        used_commands = grant.get('used_commands', {})
        cmd_key = normalize_command(normalized_cmd)

        if not allow_repeat and cmd_key in used_commands:
            return mcp_result(req_id, {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'status': 'command_already_used',
                        'message': 'Command already used (allow_repeat=False)'
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
                        'message': 'Command already used or SEC-009 limit reached'
                    })
                }],
                'isError': True
            })

        # 13. 執行命令
        assume_role = account.get('role_arn') if account and account_id != DEFAULT_ACCOUNT_ID else None
        result = execute_command(normalized_cmd, assume_role_arn=assume_role)

        # 14. 分頁輸出（大輸出時）
        paged = store_paged_output(req_id, result)
        result_text = paged.result
        page_id = paged.next_page if paged.paged else None

        # 15. Telegram 通知（best-effort）
        try:
            send_grant_execute_notification(
                grant_id=grant_id,
                command=normalized_cmd,
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
            command=normalized_cmd,
            reason=reason,
            source=source,
            account_id=account_id,
            decision_type='grant_approved',
            grant_id=grant_id,
            result_summary=result_text[:200]
        )

        # 17. 回傳
        response = {
            'status': 'grant_executed',
            'result': result_text,
            'request_id': req_id,
            'grant_id': grant_id,
        }
        if page_id:
            response['page_id'] = page_id

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response)
            }],
            'isError': False
        })

    except Exception as e:
        logger.exception(f"[GRANT_EXECUTE] Internal error: {e}")
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')
