"""
Bouncer - 命令分類與執行模組
處理 AWS CLI 命令的分類（blocked/dangerous/auto-approve）和執行
"""
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from io import StringIO
from typing import Callable, List, Optional

import boto3

from constants import BLOCKED_PATTERNS, DANGEROUS_PATTERNS, AUTO_APPROVE_PREFIXES

# Lock: Lambda warm start 下 os.environ credential swap 必須 atomic
_execute_lock = threading.Lock()

__all__ = [
    'is_blocked',
    'get_block_reason',
    'is_dangerous',
    'is_auto_approve',
    'execute_command',
    'aws_cli_split',
    '_split_chain',
    '_is_failed_output',
    '_normalize_whitespace',
    '_has_dangerous_flag',
    '_DANGEROUS_FLAG_MAP',
    'check_lambda_env_update',
    'LAMBDA_ENV_WARN_MSG',
    'CommandChainResult',
    'SubCommandResult',
]


# ---------------------------------------------------------------------------
# Command Chain Types
# ---------------------------------------------------------------------------

@dataclass
class SubCommandResult:
    """Execution result for a single sub-command in a && chain."""
    command: str
    output: str
    exit_code: int  # 0 = success, non-zero = failure (inferred from output)

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class CommandChainResult:
    """Aggregated result for a && chain execution.

    Attributes:
        results:   per-sub-command results in execution order
        stopped_at: index of the first failed sub-command (None = all succeeded)
    """
    results: List[SubCommandResult] = field(default_factory=list)
    stopped_at: Optional[int] = None

    @property
    def all_succeeded(self) -> bool:
        return self.stopped_at is None

    @property
    def combined_output(self) -> str:
        """Concatenate outputs of executed sub-commands."""
        parts = []
        for r in self.results:
            parts.append(r.output)
        return '\n'.join(p for p in parts if p)

    @property
    def sub_commands(self) -> List[str]:
        return [r.command for r in self.results]


def _normalize_whitespace(command: str) -> str:
    """正規化命令中的空白（多空格 → 單空格、strip 前後空白）"""
    return re.sub(r'\s+', ' ', command).strip()


def _split_chain(command: str) -> List[str]:
    """Split a command string on unquoted ``&&`` operators.

    Understands shell-style quoting (single/double quotes, backticks) and
    bracket nesting (``{}``, ``[]``, ``()``) so that ``&&`` inside quoted
    strings or JSON/JMESPath structures is NOT treated as a separator.

    Empty segments (e.g. ``cmd1 && && cmd2``) are silently dropped.

    Examples
    --------
    >>> _split_chain('aws s3 ls && aws sts get-caller-identity')
    ['aws s3 ls', 'aws sts get-caller-identity']

    >>> _split_chain('aws logs filter-log-events --filter-pattern "foo && bar"')
    ['aws logs filter-log-events --filter-pattern "foo && bar"']

    >>> _split_chain('aws s3 ls')
    ['aws s3 ls']

    >>> _split_chain('')
    []

    Returns
    -------
    list[str]
        Non-empty sub-command strings, each stripped of leading/trailing
        whitespace.  An empty/whitespace-only input returns ``[]``.
    """
    command = command.strip()
    if not command:
        return []

    parts: List[str] = []
    current: List[str] = []
    i = 0
    n = len(command)

    OPEN_BRACKETS = {'(', '[', '{'}
    CLOSE_MAP = {'(': ')', '[': ']', '{': '}'}
    bracket_stack: List[str] = []

    while i < n:
        c = command[i]

        # --- Quoted string: consume everything up to the matching quote ---
        if c in ('"', "'") and not bracket_stack:
            quote = c
            current.append(c)
            i += 1
            while i < n:
                sc = command[i]
                current.append(sc)
                if sc == '\\' and i + 1 < n:
                    current.append(command[i + 1])
                    i += 2
                    continue
                if sc == quote:
                    i += 1
                    break
                i += 1
            continue

        # --- Backtick: consume until matching backtick (JMESPath literals) ---
        if c == '`' and not bracket_stack:
            current.append(c)
            i += 1
            while i < n and command[i] != '`':
                current.append(command[i])
                i += 1
            if i < n:
                current.append(command[i])
                i += 1
            continue

        # --- Bracket open: push onto stack, no && splitting inside ---
        if c in OPEN_BRACKETS:
            bracket_stack.append(c)
            current.append(c)
            i += 1
            continue

        # --- Bracket close ---
        if bracket_stack and c == CLOSE_MAP.get(bracket_stack[-1], ''):
            bracket_stack.pop()
            current.append(c)
            i += 1
            continue

        # --- && operator (only when not nested) ---
        if c == '&' and not bracket_stack and i + 1 < n and command[i + 1] == '&':
            seg = ''.join(current).strip()
            if seg:
                parts.append(seg)
            current = []
            i += 2
            continue

        # --- Regular character ---
        current.append(c)
        i += 1

    seg = ''.join(current).strip()
    if seg:
        parts.append(seg)
    return parts


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
    # 特殊檢查：lambda update-function-configuration --environment Variables={}
    level, reason = check_lambda_env_update(command)
    if level == 'BLOCKED':
        return reason
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


LAMBDA_ENV_WARN_MSG = "⚠️ 此命令會覆蓋所有環境變數！請確認已備份現有設定。"


def check_lambda_env_update(command: str) -> tuple[str | None, str | None]:
    """
    檢查 lambda update-function-configuration --environment 命令。

    Returns:
        (level, message) where level is:
          - 'BLOCKED': --environment Variables={} (清空 env vars)
          - 'DANGEROUS': --environment Variables={...} (有值但會覆蓋)
          - None: 命令不符合此 pattern
    """
    cmd_lower = _normalize_whitespace(command).lower()

    # 必須是 lambda update-function-configuration
    if 'lambda update-function-configuration' not in cmd_lower:
        return None, None

    # 必須包含 --environment
    if '--environment' not in cmd_lower:
        return None, None

    # 找出 Variables={...} 的部分
    # 使用 re 找 Variables= 後面的 JSON-like 值
    # 可以是 Variables={} 或 Variables={"KEY":"VALUE",...}
    import re
    # 抓取 Variables=... 的部分（可能有空格）
    match = re.search(r'variables\s*=\s*(\{[^}]*\})', cmd_lower)
    if match:
        variables_value = match.group(1).strip()
        # {} 或空的 JSON object → BLOCKED
        if variables_value == '{}' or re.match(r'^\{\s*\}$', variables_value):
            return 'BLOCKED', '此命令會清空所有環境變數（Variables={}）！這是破壞性操作，已被封鎖。'
        else:
            # 有值的 --environment Variables={...} → DANGEROUS
            return 'DANGEROUS', LAMBDA_ENV_WARN_MSG

    return None, None


def is_dangerous(command: str) -> bool:
    """Layer 2: 檢查命令是否是高危操作（需特殊審批）"""
    cmd_lower = _normalize_whitespace(command).lower()

    # 特殊檢查：lambda update-function-configuration --environment
    level, _ = check_lambda_env_update(command)
    if level == 'DANGEROUS':
        return True

    return any(pattern in cmd_lower for pattern in DANGEROUS_PATTERNS)


def _is_safe_s3_cp(command: str) -> bool:
    """
    檢查 `aws s3 cp` 命令是否安全（只允許 S3→local download）。

    S3→S3 copy（destination 以 s3:// 開頭）有 cross-bucket exfiltration 風險，
    不允許自動批准。只有 S3→local（destination 是本地路徑）才允許自動批准。

    Returns:
        True  → S3→local download，可自動批准
        False → S3→S3 copy 或無法解析，需人工審批
    """
    try:
        argv = aws_cli_split(_normalize_whitespace(command))
    except Exception:
        return False

    # 收集位置參數（非 --flag 開頭）
    # argv 格式：['aws', 's3', 'cp', <source>, <destination>, ...]
    positional = [arg for arg in argv if not arg.startswith('-')]

    # 需要找到 'aws', 's3', 'cp' 後的 source 與 destination
    try:
        cp_idx = next(
            i for i, arg in enumerate(positional)
            if i >= 2 and positional[i - 2].lower() == 'aws'
            and positional[i - 1].lower() == 's3'
            and arg.lower() == 'cp'
        )
    except StopIteration:
        return False

    params = positional[cp_idx + 1:]  # source, destination, ...
    if len(params) < 2:
        return False

    destination = params[1]
    # 如果 destination 是 s3:// 開頭 → S3→S3 copy → 不安全
    if destination.lower().startswith('s3://'):
        return False

    return True


# 帶有危險旗標時需要人工審批的 prefix → flags 映射
# 注意：prefix 必須與 AUTO_APPROVE_PREFIXES 中的項目完全相同
_DANGEROUS_FLAG_MAP: dict[str, list[str]] = {
    'aws ssm get-parameter': ['--with-decryption'],
}


def _has_dangerous_flag(cmd_lower: str, matched_prefix: str) -> bool:
    """
    檢查命令是否包含對應 prefix 的危險旗標。

    當 AUTO_APPROVE_PREFIXES 中的某個 prefix 匹配時，
    若命令中同時存在 _DANGEROUS_FLAG_MAP 中列出的旗標，
    則該命令需要人工審批（回傳 True）。

    Args:
        cmd_lower: 已正規化並轉小寫的命令字串
        matched_prefix: 從 AUTO_APPROVE_PREFIXES 中匹配到的 prefix

    Returns:
        True 表示命令含有危險旗標（不應自動批准）
    """
    for prefix, flags in _DANGEROUS_FLAG_MAP.items():
        # 使用 startswith 檢查 prefix，與 AUTO_APPROVE_PREFIXES 邏輯一致
        if cmd_lower.startswith(prefix):
            if any(flag in cmd_lower for flag in flags):
                return True
    return False


def is_auto_approve(command: str) -> bool:
    """Layer 3: 檢查命令是否可自動批准"""
    cmd_lower = _normalize_whitespace(command).lower()
    matched = next((prefix for prefix in AUTO_APPROVE_PREFIXES if cmd_lower.startswith(prefix)), None)
    if matched is None:
        return False

    # P1-4 安全修復：aws s3 cp s3: 前綴匹配時，進一步檢查
    # 是否為 S3→S3 copy（cross-bucket exfiltration 風險）
    if cmd_lower.startswith('aws s3 cp s3:'):
        return _is_safe_s3_cp(command)

    # P1-5 安全修復：即使前綴在白名單，若命令含有危險旗標，仍需人工審批
    if _has_dangerous_flag(cmd_lower, matched):
        return False

    return True


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


def execute_command(command: str, assume_role_arn: str = None,
                    _executor: Optional[Callable] = None) -> str:
    """執行 AWS CLI 命令，支援 && 串接（thread-safe via _execute_lock）

    Args:
        command: AWS CLI 命令（可包含 && 串接多個子命令）
        assume_role_arn: 可選，要 assume 的 role ARN
        _executor: 可選，用於測試的子命令執行器
                   callable(sub_cmd, assume_role_arn) -> str
                   預設為 None（使用 _execute_locked）

    Returns:
        命令輸出（成功或錯誤訊息）。
        對於 && 串接命令，遵守 shell && 語意：停在第一個失敗的子命令。
    """
    sub_cmds = _split_chain(command)

    # 沒有任何有效子命令
    if not sub_cmds:
        return '❌ 只能執行 aws CLI 命令'

    # Fast path: single command, behaviour unchanged
    if len(sub_cmds) == 1:
        with _execute_lock:
            if _executor is not None:
                return _executor(sub_cmds[0], assume_role_arn)
            return _execute_locked(sub_cmds[0], assume_role_arn)

    # Multi-command chain: execute sequentially, stop at first failure
    chain_result = _execute_chain(sub_cmds, assume_role_arn, _executor)
    return chain_result.combined_output if chain_result.all_succeeded else _chain_failure_output(chain_result)


def _is_failed_output(output: str) -> bool:
    """判斷子命令輸出是否代表執行失敗。

    失敗判斷：
    - 以 ❌ 開頭（命令驗證失敗、awscli 未安裝等）
    - 包含 "(exit code:" 且 exit code 不是 0
      （_execute_locked 在失敗時加上 "(exit code: N)" 尾綴）
    - 輸出以 'usage:' 開頭（CLI usage error）
    """
    if output.startswith('❌'):
        return True
    if output.strip().startswith('usage:'):
        return True
    m = re.search(r'\(exit code:\s*(\d+)\)', output)
    if m and m.group(1) != '0':
        return True
    return False


def _chain_failure_output(chain_result: CommandChainResult) -> str:
    """Format output for a failed chain execution."""
    lines = []
    for idx, sub in enumerate(chain_result.results):
        if idx < chain_result.stopped_at:
            lines.append(sub.output)
        else:
            # The failing command
            lines.append(sub.output)
            if idx + 1 < len(chain_result.sub_commands):
                remaining = chain_result.sub_commands[idx + 1:]
                skipped = ' && '.join(remaining)
                lines.append(f'\n⚠️ 子命令失敗，跳過後續命令：{skipped}')
            break
    return '\n'.join(p for p in lines if p)


def _execute_chain(sub_cmds: List[str], assume_role_arn: Optional[str],
                   _executor: Optional[Callable] = None) -> CommandChainResult:
    """Execute a list of sub-commands sequentially with && semantics."""
    chain = CommandChainResult()

    for idx, cmd in enumerate(sub_cmds):
        with _execute_lock:
            if _executor is not None:
                output = _executor(cmd, assume_role_arn)
            else:
                output = _execute_locked(cmd, assume_role_arn)

        failed = _is_failed_output(output)
        exit_code = 1 if failed else 0

        chain.results.append(SubCommandResult(
            command=cmd,
            output=output,
            exit_code=exit_code,
        ))

        if failed:
            chain.stopped_at = idx
            # Append placeholder results for skipped commands so callers know
            for remaining_cmd in sub_cmds[idx + 1:]:
                chain.results.append(SubCommandResult(
                    command=remaining_cmd,
                    output='',   # not executed
                    exit_code=-1,  # skipped sentinel
                ))
            break

    return chain


def _execute_locked(command: str, assume_role_arn: str = None) -> str:
    """execute_command 的實作，必須在 _execute_lock 持有時呼叫。"""
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
            # 顯示完整原始輸出（和直接跑 CLI 一樣），加上 exit code 提示
            if output.strip():
                output = f'{output}\n\n(exit code: {exit_code})'
            else:
                output = f'(exit code: {exit_code})'

        return output  # 不截斷，讓呼叫端用 store_paged_output 處理

    except ImportError:
        return '❌ awscli 模組未安裝'
    except ValueError as e:
        return f'❌ 命令格式錯誤: {str(e)}'
    except Exception as e:
        return f'❌ 執行錯誤: {str(e)}'
