"""
Bouncer - 命令分類與執行模組
處理 AWS CLI 命令的分類（blocked/dangerous/auto-approve）和執行
"""
import os
import re
import shlex
from io import StringIO

import boto3

try:
    from constants import BLOCKED_PATTERNS, DANGEROUS_PATTERNS, AUTO_APPROVE_PREFIXES
except ImportError:
    from src.constants import BLOCKED_PATTERNS, DANGEROUS_PATTERNS, AUTO_APPROVE_PREFIXES

__all__ = [
    'is_blocked',
    'is_dangerous',
    'is_auto_approve',
    'execute_command',
    'fix_json_args',
]


def is_blocked(command: str) -> bool:
    """Layer 1: 檢查命令是否在黑名單（絕對禁止）"""
    # 移除 --query 參數內容（JMESPath 語法可能包含反引號）
    cmd_sanitized = re.sub(r"--query\s+['\"].*?['\"]", "--query REDACTED", command)
    cmd_sanitized = re.sub(r"--query\s+[^\s'\"]+", "--query REDACTED", cmd_sanitized)
    cmd_lower = cmd_sanitized.lower()
    return any(pattern in cmd_lower for pattern in BLOCKED_PATTERNS)


def is_dangerous(command: str) -> bool:
    """Layer 2: 檢查命令是否是高危操作（需特殊審批）"""
    cmd_lower = command.lower()
    return any(pattern in cmd_lower for pattern in DANGEROUS_PATTERNS)


def is_auto_approve(command: str) -> bool:
    """Layer 3: 檢查命令是否可自動批准"""
    cmd_lower = command.lower()
    return any(cmd_lower.startswith(prefix) for prefix in AUTO_APPROVE_PREFIXES)


def fix_json_args(command: str, cli_args: list) -> list:
    """
    修復被 shlex.split 破壞的 JSON/陣列參數

    shlex.split 會移除引號，導致 {"key":"val"} 變成 {key:val}
    此函數從原始命令中重新提取正確的 JSON

    Args:
        command: 原始命令字串
        cli_args: shlex.split 後的參數列表（不含 'aws'）

    Returns:
        修復後的參數列表
    """
    for i, arg in enumerate(cli_args):
        if i + 1 >= len(cli_args):
            continue
        next_val = cli_args[i + 1]

        # 檢查是否是 JSON 或陣列開頭
        if not (next_val.startswith('{') or next_val.startswith('[')):
            continue

        # 簡單 JSON 匹配
        pattern = re.escape(arg) + r'''\s+(['"]?)(\{[^}]*\}|\[[^\]]*\])\1'''
        match = re.search(pattern, command)
        if match:
            cli_args[i + 1] = match.group(2)
            continue

        # 複雜 JSON（多層巢狀）：用括號計數
        param_pos = command.find(arg)
        if param_pos == -1:
            continue
        after_param = command[param_pos + len(arg):].lstrip()

        # 移除開頭的引號
        quote_char = None
        if after_param and after_param[0] in "'\"":
            quote_char = after_param[0]
            after_param = after_param[1:]

        if not after_param or after_param[0] not in '{[':
            continue

        # 計數括號找結尾
        open_char = after_param[0]
        close_char = '}' if open_char == '{' else ']'
        depth = 0
        in_string = False
        escape_next = False
        end_pos = 0

        for j, c in enumerate(after_param):
            if escape_next:
                escape_next = False
                continue
            if c == '\\':
                escape_next = True
                continue
            if c == '"' and not in_string:
                in_string = True
            elif c == '"' and in_string:
                in_string = False
            elif not in_string:
                if c == open_char:
                    depth += 1
                elif c == close_char:
                    depth -= 1
                    if depth == 0:
                        end_pos = j + 1
                        break

        if end_pos > 0:
            json_str = after_param[:end_pos]
            if quote_char and json_str.endswith(quote_char):
                json_str = json_str[:-1]
            cli_args[i + 1] = json_str

    return cli_args


def execute_command(command: str, assume_role_arn: str = None) -> str:
    """執行 AWS CLI 命令

    Args:
        command: AWS CLI 命令
        assume_role_arn: 可選，要 assume 的 role ARN

    Returns:
        命令輸出（成功或錯誤訊息）
    """
    import sys

    try:
        # 使用 shlex.split 解析命令
        try:
            args = shlex.split(command)
        except ValueError as e:
            return f'❌ 命令格式錯誤: {str(e)}'

        if not args or args[0] != 'aws':
            return '❌ 只能執行 aws CLI 命令'

        # 移除 'aws' 前綴，awscli.clidriver 不需要它
        cli_args = args[1:]

        # 修復被 shlex 破壞的 JSON 參數
        cli_args = fix_json_args(command, cli_args)

        # 保存原始環境變數
        original_env = {}

        # 如果需要 assume role，先取得臨時 credentials
        if assume_role_arn:
            try:
                sts = boto3.client('sts')
                assumed = sts.assume_role(
                    RoleArn=assume_role_arn,
                    RoleSessionName='bouncer-execution',
                    DurationSeconds=900  # 15 分鐘
                )
                creds = assumed['Credentials']

                # 設定環境變數讓 awscli 使用這些 credentials
                original_env = {
                    'AWS_ACCESS_KEY_ID': os.environ.get('AWS_ACCESS_KEY_ID'),
                    'AWS_SECRET_ACCESS_KEY': os.environ.get('AWS_SECRET_ACCESS_KEY'),
                    'AWS_SESSION_TOKEN': os.environ.get('AWS_SESSION_TOKEN'),
                }
                os.environ['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
                os.environ['AWS_SECRET_ACCESS_KEY'] = creds['SecretAccessKey']
                os.environ['AWS_SESSION_TOKEN'] = creds['SessionToken']

            except Exception as e:
                return f'❌ Assume role 失敗: {str(e)}'

        # 捕獲 stdout/stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()

        try:
            from awscli.clidriver import create_clidriver
            driver = create_clidriver()

            # 禁用 pager
            os.environ['AWS_PAGER'] = ''

            exit_code = driver.main(cli_args)

            stdout_output = sys.stdout.getvalue()
            stderr_output = sys.stderr.getvalue()

        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

            # 還原環境變數
            if assume_role_arn and original_env:
                for key, value in original_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        output = stdout_output or stderr_output or ''

        if exit_code == 0:
            if not output.strip():
                output = '✅ 命令執行成功（無輸出）'
        else:
            if not output.strip():
                output = f'❌ 命令失敗 (exit code: {exit_code})'

        return output  # 不截斷，讓呼叫端用 store_paged_output 處理

    except ImportError:
        return '❌ awscli 模組未安裝'
    except ValueError as e:
        return f'❌ 命令格式錯誤: {str(e)}'
    except Exception as e:
        return f'❌ 執行錯誤: {str(e)}'
