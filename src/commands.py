"""
Bouncer - 命令分類與執行模組
處理 AWS CLI 命令的分類（blocked/dangerous/auto-approve）和執行
"""
import os
import re
import sys
from io import StringIO

import boto3


from constants import BLOCKED_PATTERNS, DANGEROUS_PATTERNS, AUTO_APPROVE_PREFIXES

__all__ = [
    'is_blocked',
    'get_block_reason',
    'is_dangerous',
    'is_auto_approve',
    'execute_command',
    'aws_cli_split',
    '_normalize_whitespace',
]


def _normalize_whitespace(command: str) -> str:
    """正規化命令中的空白（多空格 → 單空格、strip 前後空白）"""
    return re.sub(r'\s+', ' ', command).strip()


def is_blocked(command: str) -> bool:
    """Layer 1: 檢查命令是否在黑名單（絕對禁止）"""
    reason = get_block_reason(command)
    return reason is not None


def get_block_reason(command: str) -> str | None:
    """檢查命令是否被封鎖，回傳封鎖原因或 None"""
    cmd_normalized = _normalize_whitespace(command)
    # 移除 --query 參數內容（JMESPath 語法可能包含反引號）
    cmd_sanitized = re.sub(r"--query\s+['\"].*?['\"]", "--query REDACTED", cmd_normalized)
    cmd_sanitized = re.sub(r"--query\s+[^\s'\"]+", "--query REDACTED", cmd_sanitized)
    cmd_lower = cmd_sanitized.lower()
    # 檢查危險旗標
    flag = _get_blocked_flag(cmd_lower)
    if flag:
        return f"危險旗標: {flag.strip()}"
    # 檢查 file:// 協議
    if _has_file_protocol(cmd_lower):
        return "禁止使用 file:// 或 fileb:// 協議（本地檔案讀取風險）"
    # 檢查封鎖 pattern
    for pattern in BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return f"封鎖規則: {pattern}"
    return None


def _get_blocked_flag(cmd_lower: str) -> str | None:
    """檢查命令是否包含危險的全域旗標，回傳匹配的旗標或 None"""
    blocked_flags = [
        '--endpoint-url ',   # 重定向 API 請求到外部
        '--profile ',        # 切換到未授權的 AWS profile
        '--no-verify-ssl',   # 禁用 SSL 驗證（MITM 風險）
        '--ca-bundle ',      # 使用自訂 CA 證書
    ]
    for flag in blocked_flags:
        if flag in cmd_lower:
            return flag
    return None


def _has_file_protocol(cmd_lower: str) -> bool:
    """檢查命令是否使用 file:// 或 fileb:// 協議讀取本地檔案"""
    return 'file://' in cmd_lower or 'fileb://' in cmd_lower


def is_dangerous(command: str) -> bool:
    """Layer 2: 檢查命令是否是高危操作（需特殊審批）"""
    cmd_lower = _normalize_whitespace(command).lower()
    return any(pattern in cmd_lower for pattern in DANGEROUS_PATTERNS)


def is_auto_approve(command: str) -> bool:
    """Layer 3: 檢查命令是否可自動批准"""
    cmd_lower = _normalize_whitespace(command).lower()
    return any(cmd_lower.startswith(prefix) for prefix in AUTO_APPROVE_PREFIXES)


def aws_cli_split(command: str) -> list:
    """
    把 AWS CLI 命令字串拆成 argv list。

    不依賴 shell 語法（shlex），理解 AWS CLI 常見結構：
    - 引號字串："..." 或 '...'（去引號，空引號 → 空字串 token）
    - JSON/陣列：{...} 或 [...]（巢狀安全）
    - 函數/JMESPath：(...)（巢狀安全）
    - 反引號：`...`（保留，JMESPath 字面值）

    空格是分隔符，除非在上述結構內部。
    """
    tokens = []
    i = 0
    n = len(command)

    OPEN_BRACKETS = {'(', '[', '{'}
    CLOSE_MAP = {'(': ')', '[': ']', '{': '}'}

    while i < n:
        # 跳過空白
        if command[i] == ' ':
            i += 1
            continue

        # 開始收集一個 token
        token_parts = []
        has_content = False  # 追蹤是否有任何內容（包括空引號）

        while i < n and command[i] != ' ':
            c = command[i]

            # 引號：收集到配對引號，去引號
            if c in ('"', "'"):
                has_content = True
                quote = c
                i += 1
                part = []
                while i < n and command[i] != quote:
                    if command[i] == '\\' and i + 1 < n and command[i + 1] == quote:
                        part.append(command[i + 1])
                        i += 2
                    else:
                        part.append(command[i])
                        i += 1
                if i < n:
                    i += 1  # 跳過結尾引號
                token_parts.append(''.join(part))
                continue

            # 反引號：收集到配對反引號（保留反引號本身，JMESPath 語法）
            if c == '`':
                has_content = True
                part = [c]
                i += 1
                while i < n and command[i] != '`':
                    part.append(command[i])
                    i += 1
                if i < n:
                    part.append(command[i])
                    i += 1
                token_parts.append(''.join(part))
                continue

            # 開括號 { [ (：用堆疊追蹤配對（支援巢狀混合括號）
            if c in OPEN_BRACKETS:
                has_content = True
                stack = [c]
                part = [c]
                i += 1

                while i < n and stack:
                    cc = command[i]
                    part.append(cc)

                    if cc in ('"', "'"):
                        # 字串：跳到配對引號
                        q = cc
                        i += 1
                        while i < n:
                            sc = command[i]
                            part.append(sc)
                            if sc == '\\' and i + 1 < n:
                                part.append(command[i + 1])
                                i += 2
                                continue
                            if sc == q:
                                i += 1
                                break
                            i += 1
                        continue
                    elif cc == '`':
                        # 反引號內容
                        i += 1
                        while i < n and command[i] != '`':
                            part.append(command[i])
                            i += 1
                        if i < n:
                            part.append(command[i])
                            i += 1
                        continue
                    elif cc in OPEN_BRACKETS:
                        stack.append(cc)
                    elif stack and cc == CLOSE_MAP.get(stack[-1]):
                        stack.pop()
                        if not stack:
                            i += 1
                            break

                    i += 1
                token_parts.append(''.join(part))
                continue

            # 普通字元
            has_content = True
            token_parts.append(c)
            i += 1

        if has_content:
            tokens.append(''.join(token_parts))

    return tokens


def execute_command(command: str, assume_role_arn: str = None) -> str:
    """執行 AWS CLI 命令

    Args:
        command: AWS CLI 命令
        assume_role_arn: 可選，要 assume 的 role ARN

    Returns:
        命令輸出（成功或錯誤訊息）
    """

    try:
        # 解析命令字串為 argv list
        args = aws_cli_split(command)

        if not args or args[0] != 'aws':
            return '❌ 只能執行 aws CLI 命令'

        # 移除 'aws' 前綴，awscli.clidriver 不需要它
        cli_args = args[1:]

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
