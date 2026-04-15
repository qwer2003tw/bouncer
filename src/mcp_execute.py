"""
Bouncer - Execute Pipeline

ExecuteContext + all _check_* functions + mcp_tool_execute()
Grant-session tools moved to mcp_grant.py (sprint60-002).
Chain risk analysis moved to chain_analyzer.py (sprint60-002).
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



from utils import mcp_result, generate_request_id, log_decision, generate_display_summary, record_execution_error, extract_exit_code
from commands import get_block_reason, is_auto_approve, execute_command, execute_boto3_native
from chain_analyzer import check_chain_risks
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id,
)
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

    DEFAULT_ACCOUNT_ID, MCP_MAX_WAIT, RATE_LIMIT_WINDOW,
    TRUST_SESSION_MAX_COMMANDS,
    APPROVAL_TTL_BUFFER,
    AUDIT_TTL_SHORT,
    GRANT_SESSION_ENABLED,
    TELEGRAM_PAGE_SIZE,
)
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

# Backward-compat alias for tests that reference mcp_execute._check_chain_risks (s60-002)
_check_chain_risks = check_chain_risks


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
    caller_ip: str = ''  # IP address of the caller (from Lambda event)
    bot_id: str = 'unknown'  # Bot ID for audit trail
    smart_decision: object = None  # smart_approval result (or None)
    mode: str = 'mcp'
    grant_id: Optional[str] = None
    template_scan_result: Optional[dict] = None  # Layer 2.5 template scan result
    cli_input_json: Optional[dict] = None
    # Native execution fields (for bouncer_execute_native)
    is_native: bool = False  # True if this is a boto3 native execution
    native_service: Optional[str] = None  # boto3 service name (e.g. 'eks')
    native_operation: Optional[str] = None  # boto3 operation (e.g. 'create_cluster')
    native_params: Optional[dict] = None  # boto3 params dict
    native_region: Optional[str] = None  # AWS region


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
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: command',
                'suggestion': '請提供要執行的命令。範例：aws s3 ls'
            })}],
            'isError': True
        })

    if not trust_scope:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: trust_scope',
                'suggestion': '設定 trust_scope 為穩定的呼叫者識別碼（如 "private-bot-main"）以啟用信任時段自動審批',
                'details': (
                    'trust_scope is a stable caller identifier used for trust session matching.\n'
                    'Examples:\n'
                    '  - "private-bot-main"        (for general usage)\n'
                    '  - "private-bot-deploy"      (for deployment tasks)\n'
                    '  - "private-bot-kubectl"     (for kubectl operations)'
                )
            })}],
            'isError': True
        })

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
                    'error_code': 'ACCOUNT_NOT_FOUND',
                    'error': f'帳號 {account_id} 未配置',
                    'available_accounts': available,
                    'suggestion': f'使用可用帳號之一：{", ".join(available)}，或聯繫管理員新增此帳號'
                })}],
                'isError': True
            })

        if not account.get('enabled', True):
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error_code': 'ACCOUNT_DISABLED',
                    'error': f'帳號 {account_id} 已停用',
                    'suggestion': '聯繫管理員啟用此帳號，或使用其他已啟用的帳號'
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

    # Sprint 81: Override assume_role with caller's role_arn if provided
    caller = arguments.get('_caller', {})
    if caller.get('role_arn'):
        assume_role = caller['role_arn']

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
        caller_ip=arguments.get('caller_ip', ''),
        bot_id=arguments.get('_caller', {}).get('bot_id', 'unknown'),
        grant_id=arguments.get('grant_id', None),
        cli_input_json=arguments.get('cli_input_json') or None,
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
        logger.error("compliance_checker module import failed - failing closed", extra={"src_module": "execute", "operation": "check_compliance"})
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

        # No paging metadata in MCP response (Sprint 83)

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
    if not _should_throttle_notification('auto_approve'):
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
            logger.error("Failed to delete DDB record %s: %s", request_id, del_err, extra={"src_module": "execute", "operation": "orphan_cleanup", "request_id": request_id, "error": str(del_err)})
        logger.error("Telegram notification failed for %s: %s", request_id, tg_err, extra={"src_module": "execute", "operation": "orphan_cleanup", "request_id": request_id, "error": str(tg_err)})
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
        except Exception as _e:  # noqa: BLE001 — best-effort, never block approval flow
            logger.debug("post-execute notify ignored: %s", _e, extra={"src_module": "mcp_execute", "operation": "post_execute_notify"})

        # S59-001: Schedule pending approval reminder — best-effort, never block approval flow
        try:
            from scheduler_service import get_scheduler_service
            from constants import PENDING_REMINDER_MINUTES
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
    chain_check = check_chain_risks(ctx)
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
            or _check_trust_session(ctx)
            or _check_rate_limit(ctx)
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



def mcp_tool_eks_get_token(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_eks_get_token — generate kubectl EKS token via STS presigned URL."""
    from commands import generate_eks_token

    cluster_name = arguments.get('cluster_name', '').strip()
    region = arguments.get('region', 'us-east-1').strip()
    account = arguments.get('account', '').strip()

    if not cluster_name:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: cluster_name',
                'suggestion': '請提供 EKS cluster 名稱。範例：my-eks-cluster'
            })}],
            'isError': True
        })

    # Resolve assume_role_arn from account
    assume_role_arn = None
    if account:
        from accounts import get_account
        acct = get_account(account)
        if acct and acct.get('assume_role'):
            assume_role_arn = acct['assume_role']
        elif account:
            assume_role_arn = f'arn:aws:iam::{account}:role/BouncerRole'

    result = generate_eks_token(cluster_name, region=region, assume_role_arn=assume_role_arn)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': result,
        }]
    })

def mcp_tool_execute_native(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_execute_native — boto3 native execution without awscli dependency.

    Input format:
    {
      "aws": {
        "service": "eks",
        "operation": "create_cluster",
        "region": "us-east-1",
        "account": "123456789012",
        "params": {...}
      },
      "bouncer": {
        "reason": "建 EKS cluster",
        "source": "Private Bot (EKS)",
        "trust_scope": "agent-bouncer-exec",
        "approval_timeout": 600
      }
    }
    """
    # Parse aws section
    aws_section = arguments.get('aws', {})
    if not aws_section:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: aws',
                'suggestion': '請提供 aws 區段，包含 service, operation, params 等欄位'
            })}],
            'isError': True
        })

    service = str(aws_section.get('service', '')).strip()
    operation = str(aws_section.get('operation', '')).strip()
    params = aws_section.get('params', {})
    region = aws_section.get('region', None)
    account_id = aws_section.get('account', None)

    if not service:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: aws.service',
                'suggestion': '請提供 AWS 服務名稱。範例：eks, s3, ec2'
            })}],
            'isError': True
        })
    if not operation:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: aws.operation',
                'suggestion': '請提供 boto3 操作名稱。範例：create_cluster, list_buckets'
            })}],
            'isError': True
        })
    if not isinstance(params, dict):
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'INVALID_PARAM',
                'error': 'aws.params must be a dict',
                'suggestion': '請提供有效的 params 物件（dict/object），而非字串或陣列'
            })}],
            'isError': True
        })

    # Parse bouncer section
    bouncer_section = arguments.get('bouncer', {})
    reason = str(bouncer_section.get('reason', 'No reason provided'))
    source = bouncer_section.get('source', None)
    trust_scope = str(bouncer_section.get('trust_scope', '')).strip()
    context = bouncer_section.get('context', None)
    timeout = min(int(bouncer_section.get('approval_timeout', MCP_MAX_WAIT)), MCP_MAX_WAIT)
    sync_mode = bouncer_section.get('sync', False)

    if not trust_scope:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error_code': 'MISSING_PARAM',
                'error': 'Missing required parameter: bouncer.trust_scope',
                'suggestion': '設定 bouncer.trust_scope 為穩定的呼叫者識別碼（如 "agent-bouncer-exec"）以啟用信任時段自動審批',
                'details': (
                    'trust_scope is a stable caller identifier used for trust session matching.\n'
                    'Examples:\n'
                    '  - "agent-bouncer-exec"      (for general agent usage)\n'
                    '  - "private-bot-eks"         (for EKS operations)'
                )
            })}],
            'isError': True
        })

    # Convert boto3 operation to kebab-case for synthetic command (for compliance checking)
    # e.g. create_cluster -> create-cluster
    operation_kebab = operation.replace('_', '-')

    # Build synthetic command string for compliance/risk scoring
    # Format: "aws {service} {operation-kebab} {params_json}"
    # This allows existing compliance rules to work with native calls
    synthetic_command = f"aws {service} {operation_kebab} {json.dumps(params, separators=(',', ':'))}"

    # 初始化預設帳號
    init_default_account()

    # 解析帳號配置
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
            available = [a['account_id'] for a in list_accounts()]
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error_code': 'ACCOUNT_NOT_FOUND',
                    'error': f'帳號 {account_id} 未配置',
                    'available_accounts': available,
                    'suggestion': f'使用可用帳號之一：{", ".join(available)}，或聯繫管理員新增此帳號'
                })}],
                'isError': True
            })

        if not account.get('enabled', True):
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error_code': 'ACCOUNT_DISABLED',
                    'error': f'帳號 {account_id} 已停用',
                    'suggestion': '聯繫管理員啟用此帳號，或使用其他已啟用的帳號'
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

    # Sprint 81: Override assume_role with caller's role_arn if provided
    caller = arguments.get('_caller', {})
    if caller.get('role_arn'):
        assume_role = caller['role_arn']

    # Create ExecuteContext with native mode enabled
    ctx = ExecuteContext(
        req_id=req_id,
        command=synthetic_command,  # Used for compliance/risk checking
        reason=reason,
        source=source,
        trust_scope=trust_scope,
        context=context,
        account_id=account_id,
        account_name=account_name,
        assume_role=assume_role,
        timeout=timeout,
        sync_mode=sync_mode,
        caller_ip=arguments.get('caller_ip', ''),
        bot_id=arguments.get('_caller', {}).get('bot_id', 'unknown'),
        # Native execution fields
        is_native=True,
        native_service=service,
        native_operation=operation,
        native_params=params,
        native_region=region,
    )

    # Phase 2: Smart approval shadow scoring (before any decision)
    _score_risk(ctx)

    # Phase 2.5: Template scan — escalate to MANUAL on HIGH/CRITICAL hits
    _scan_template(ctx)

    # Phase 2.6: Chain risk pre-check — skip for native (no && chains in native mode)
    # Native calls don't support command chaining

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
            or _check_trust_session(ctx)
            or _check_rate_limit(ctx)
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
