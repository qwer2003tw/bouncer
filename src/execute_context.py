"""
Bouncer - Execute Context

ExecuteContext dataclass and request parsing.
"""

import re
from dataclasses import dataclass
from typing import Optional

from utils import mcp_result
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id,
)
from constants import DEFAULT_ACCOUNT_ID, MCP_MAX_WAIT


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
    import json

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
