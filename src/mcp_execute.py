"""
Bouncer - Execute Pipeline

ExecuteContext + all _check_* functions + mcp_tool_execute()
Grant-session tools moved to mcp_grant.py (sprint60-002).
Chain risk analysis moved to chain_analyzer.py (sprint60-002).
"""

import json

from execute_context import ExecuteContext, _parse_execute_request
from execute_pipeline import (
    _score_risk, _scan_template, _check_compliance, _check_blocked,
    _check_grant_session, _check_auto_approve, _check_rate_limit,
    _check_trust_session, _submit_for_approval,
)
from execute_helpers import _extract_actual_decision, _log_smart_approval_shadow
from chain_analyzer import check_chain_risks
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id,
)
from commands import generate_eks_token
from utils import mcp_result
from constants import DEFAULT_REGION, MCP_MAX_WAIT, DEFAULT_ACCOUNT_ID
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

# Backward-compat alias for tests that reference mcp_execute._check_chain_risks (s60-002)
_check_chain_risks = check_chain_risks


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
    cluster_name = arguments.get('cluster_name', '').strip()
    region = arguments.get('region', DEFAULT_REGION).strip()
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
        acct = get_account(account)
        if acct and acct.get('assume_role'):
            assume_role_arn = acct['assume_role']
        elif account:
            assume_role_arn = f'arn:aws:iam::{account}:role/BouncerRole'

    result = generate_eks_token(cluster_name, region=region, assume_role_arn=assume_role_arn)

    # Audit log: EKS token generated
    body = arguments
    logger.info("EKS token generated", extra={
        "src_module": "mcp_execute", "operation": "eks_get_token",
        "cluster_name": cluster_name,
        "region": region,
        "account_id": account,
        "source": body.get('source', 'unknown'),
        "bot_id": body.get('_caller', {}).get('bot_id', 'unknown'),
    })

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
        "region": DEFAULT_REGION,
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


# =============================================================================
# Backward Compatibility Re-exports
# =============================================================================

# These were originally in mcp_execute.py — re-export for test compatibility
# Tests using `from mcp_execute import X` or `patch('mcp_execute.X')` will still work
#
# The following are already imported at the top and available in mcp_execute namespace:
#   - ExecuteContext (from execute_context)
#   - _parse_execute_request (from execute_context)
#   - _score_risk, _scan_template (from execute_pipeline)
#   - _check_compliance, _check_blocked, _check_grant_session (from execute_pipeline)
#   - _check_auto_approve, _check_rate_limit, _check_trust_session (from execute_pipeline)
#   - _submit_for_approval (from execute_pipeline)
#   - _extract_actual_decision, _log_smart_approval_shadow (from execute_helpers)
#
# Additional re-exports needed for full backward compatibility:

from execute_context import _normalize_command  # noqa: F401, E402
from execute_helpers import (  # noqa: F401, E402
    _safe_risk_category,
    _safe_risk_factors,
    _map_status_to_decision,
)

# Additional backward-compat re-exports (tests patch these on mcp_execute module)
from commands import is_auto_approve, execute_command  # noqa: F401, E402
from utils import extract_exit_code  # noqa: F401, E402
from trust import should_trust_approve, increment_trust_command_count  # noqa: F401, E402
from rate_limit import check_rate_limit  # noqa: F401, E402
from notifications import send_blocked_notification  # noqa: F401, E402
from metrics import emit_metric  # noqa: F401, E402
from constants import GRANT_SESSION_ENABLED  # noqa: F401, E402
from db import table  # noqa: F401, E402

# More backward-compat re-exports (tests patch these on mcp_execute module)
from utils import log_decision, record_execution_error, generate_request_id  # noqa: F401, E402
from notifications import (  # noqa: F401, E402
    send_approval_request, send_grant_execute_notification,
    send_trust_auto_approve_notification,
)
from telegram import send_telegram_message_silent  # noqa: F401, E402
from paging import store_paged_output  # noqa: F401, E402
from trust import track_command_executed  # noqa: F401, E402
from commands import execute_boto3_native  # noqa: F401, E402
