"""
Bouncer - Chain Risk Analysis

Extracted from mcp_execute.py (sprint60-002).
Contains:
- check_chain_risks (formerly _check_chain_risks)
"""

import json
from typing import Optional

from aws_lambda_powertools import Logger

import commands
import compliance_checker
from utils import mcp_result, log_decision, generate_request_id
from commands import get_block_reason, _split_chain
from db import table
from notifications import send_blocked_notification
from metrics import emit_metric

logger = Logger(service="bouncer")


def _safe_risk_category(smart_decision):
    """安全取得 risk category 值（相容 enum 和 string）"""
    if not smart_decision:
        return None
    try:
        cat = smart_decision.risk_result.category
        return cat.value if hasattr(cat, 'value') else cat
    except (AttributeError, KeyError):
        return None


def _safe_risk_factors(smart_decision):
    """安全取得 risk factors 值"""
    if not smart_decision:
        return None
    return getattr(smart_decision, 'risk_factors', None)


def check_chain_risks(ctx) -> Optional[dict]:
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
        args = commands.aws_cli_split(sub_cmd)
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
            is_compliant, violation = compliance_checker.check_compliance(sub_cmd)
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
            logger.debug("compliance_checker module not available", exc_info=True)

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
