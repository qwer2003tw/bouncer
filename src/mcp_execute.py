"""
Bouncer - Execute Pipeline

Entry points: mcp_tool_execute, mcp_tool_execute_native, mcp_tool_eks_get_token.
Grant-session tools moved to mcp_grant.py (sprint60-002).
Chain risk analysis moved to chain_analyzer.py (sprint60-002).
Context and helpers split to execute_context/execute_helpers/execute_pipeline (#268).
"""

import json

from aws_lambda_powertools import Logger

from execute_context import ExecuteContext, _parse_execute_request
from execute_pipeline import (
    _score_risk, _scan_template,
    _check_compliance, _check_blocked, _check_grant_session, _check_auto_approve,
    _check_rate_limit, _check_trust_session, _submit_for_approval,
)
from execute_helpers import _extract_actual_decision, _log_smart_approval_shadow
from chain_analyzer import check_chain_risks
from commands import generate_eks_token
from accounts import get_account, init_default_account, list_accounts, validate_account_id
from utils import mcp_result, mcp_error
from constants import DEFAULT_ACCOUNT_ID, DEFAULT_REGION, MCP_MAX_WAIT
from agent_keys import identify_agent

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

    # Phase 1.5: Agent identity check (#418) — server-side source override
    bouncer_key = arguments.get('bouncer_key') or arguments.get('bouncer', {}).get('key')
    if bouncer_key:
        agent = identify_agent(bouncer_key)
        if agent:
            # Server-side source override — cannot be spoofed
            ctx.source = agent['agent_name']
            ctx.agent_id = agent['agent_id']
            ctx.verified_identity = True
        else:
            # Invalid key — reject request
            return mcp_error(req_id, -32001, "Invalid or expired agent key")

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

    # Resolve account: aws.account (canonical) → bouncer.account (fallback)
    if not account_id:
        account_id = bouncer_section.get('account', None)

    # Convert boto3 operation to kebab-case for synthetic command (for compliance checking)
    # e.g. create_cluster -> create-cluster
    operation_kebab = operation.replace('_', '-')

    # Map boto3 service name to AWS CLI subcommand name
    # boto3 uses 's3' for all S3 operations, but AWS CLI splits into:
    #   - 'aws s3' (high-level: ls, cp, sync)
    #   - 'aws s3api' (low-level API: list-objects-v2, get-object, etc.)
    # Safelist (AUTO_APPROVE_PREFIXES) uses CLI naming, so we must match.
    _SERVICE_TO_CLI = {
        's3': 's3api',           # boto3 s3 operations are s3api CLI commands
        'logs': 'logs',          # same
        'sts': 'sts',            # same
    }
    cli_service = _SERVICE_TO_CLI.get(service, service)

    # Build synthetic command string for compliance/risk scoring
    # Format: "aws {cli_service} {operation-kebab} {params_json}"
    # This allows existing compliance rules to work with native calls
    synthetic_command = f"aws {cli_service} {operation_kebab} {json.dumps(params, separators=(',', ':'))}"

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

    # Phase 1.5: Agent identity check (#418) — server-side source override
    bouncer_key = arguments.get('bouncer_key') or bouncer_section.get('key')
    if bouncer_key:
        agent = identify_agent(bouncer_key)
        if agent:
            # Server-side source override — cannot be spoofed
            ctx.source = agent['agent_name']
            ctx.agent_id = agent['agent_id']
            ctx.verified_identity = True
        else:
            # Invalid key — reject request
            return mcp_error(req_id, -32001, "Invalid or expired agent key")

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
