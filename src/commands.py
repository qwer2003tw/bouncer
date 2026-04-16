"""
Bouncer - 命令分類與執行模組
處理 AWS CLI 命令的分類（blocked/dangerous/auto-approve）和執行
"""
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import boto3
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from constants import BLOCKED_PATTERNS, DANGEROUS_PATTERNS, AUTO_APPROVE_PREFIXES, DEFAULT_REGION

logger = Logger(service="bouncer")

# Lock: Lambda warm start 下 os.environ credential swap 必須 atomic
_execute_lock = threading.Lock()

__all__ = [
    'is_blocked',
    'get_block_reason',
    'is_dangerous',
    'is_auto_approve',
    'execute_command',
    'execute_boto3_native',
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
    results: list[SubCommandResult] = field(default_factory=list)
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
    def sub_commands(self) -> list[str]:
        return [r.command for r in self.results]


def _normalize_whitespace(command: str) -> str:
    """正規化命令中的空白（多空格 → 單空格、strip 前後空白）"""
    return re.sub(r'\s+', ' ', command).strip()


def _split_chain(command: str) -> list[str]:
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

    parts: list[str] = []
    current: list[str] = []
    i = 0
    n = len(command)

    OPEN_BRACKETS = {'(', '[', '{'}
    CLOSE_MAP = {'(': ')', '[': ']', '{': '}'}
    bracket_stack: list[str] = []

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
    """檢查命令是否使用 file:// 或 fileb:// 協議讀取本地檔案

    例外：--cli-input-json file:// 是 AWS CLI 的合法用法，允許通過。
    """
    if 'file://' not in cmd_lower and 'fileb://' not in cmd_lower:
        return False
    # --cli-input-json file:// 是 AWS CLI 官方 workaround，允許
    if '--cli-input-json' in cmd_lower and 'file://' in cmd_lower:
        return False
    return True


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
    except ValueError:
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
                    _executor: Optional[Callable] = None,
                    cli_input_json: dict = None) -> str:
    """執行 AWS CLI 命令，支援 && 串接（thread-safe via _execute_lock）

    Args:
        command: AWS CLI 命令（可包含 && 串接多個子命令）
        assume_role_arn: 可選，要 assume 的 role ARN
        _executor: 可選，用於測試的子命令執行器
                   callable(sub_cmd, assume_role_arn) -> str
                   預設為 None（使用 _execute_locked）
        cli_input_json: 可選，將此 dict 寫入 tempfile 並以 --cli-input-json file:// 傳入 AWS CLI

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
                return _executor(sub_cmds[0], assume_role_arn, cli_input_json)
            return _execute_locked(sub_cmds[0], assume_role_arn, cli_input_json=cli_input_json)

    # Multi-command chain: execute sequentially, stop at first failure
    chain_result = _execute_chain(sub_cmds, assume_role_arn, _executor, cli_input_json)
    return chain_result.combined_output if chain_result.all_succeeded else _chain_failure_output(chain_result)



def generate_eks_token(cluster_name: str, region: str = None, assume_role_arn: str = None) -> str:
    """Generate EKS kubectl token (k8s-aws-v1.* format) via STS presigned URL.

    Uses SigV4QueryAuth to include x-k8s-aws-id header in the signed URL.
    Returns ExecCredential JSON string.
    """
    import base64
    import datetime
    import json as _json
    import urllib.parse
    from botocore.awsrequest import AWSRequest
    from botocore.auth import SigV4QueryAuth
    from botocore.credentials import Credentials as BotoCreds

    try:
        if assume_role_arn:
            sts_base = boto3.client('sts', region_name=region)
            assumed = sts_base.assume_role(
                RoleArn=assume_role_arn,
                RoleSessionName='bouncer-eks-token',
                DurationSeconds=900,
            )
            creds_data = assumed['Credentials']
            frozen = BotoCreds(
                access_key=creds_data['AccessKeyId'],
                secret_key=creds_data['SecretAccessKey'],
                token=creds_data['SessionToken'],
            )
        else:
            import boto3 as _boto3
            session = _boto3.Session(region_name=region)
            frozen = session.get_credentials().get_frozen_credentials()

        # Build STS GetCallerIdentity presigned URL with x-k8s-aws-id signed header
        params = urllib.parse.urlencode({'Action': 'GetCallerIdentity', 'Version': '2011-06-15'})
        url = f'https://sts.{region}.amazonaws.com/?{params}'
        req = AWSRequest(method='GET', url=url, headers={'x-k8s-aws-id': cluster_name})
        SigV4QueryAuth(frozen, 'sts', region, expires=60).add_auth(req)
        presigned_url = req.prepare().url

        token = 'k8s-aws-v1.' + base64.urlsafe_b64encode(presigned_url.encode()).decode().rstrip('=')
        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=14)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        cred = {
            'apiVersion': 'client.authentication.k8s.io/v1beta1',
            'kind': 'ExecCredential',
            'status': {
                'token': token,
                'expirationTimestamp': expiry,
            },
        }
        return _json.dumps(cred)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        return f'\u274c EKS token \u751f\u6210\u5931\u6557: {code}: {msg}'
    except Exception as e:  # noqa: BLE001
        return f'\u274c EKS token \u751f\u6210\u5931\u6557: {str(e)}'


def execute_boto3_native(
    service: str,
    operation: str,
    params: dict,
    region: str = None,
    assume_role_arn: str = None,
) -> str:
    """Execute AWS API call directly via boto3 (no awscli dependency).

    Args:
        service: boto3 service name (e.g. 'eks', 's3', 'ec2')
        operation: boto3 method name in snake_case (e.g. 'create_cluster')
        params: boto3 kwargs dict
        region: AWS region (default: AWS_DEFAULT_REGION env var)
        assume_role_arn: optional IAM role to assume

    Returns:
        JSON string of the response, or error message starting with '❌'
    """
    import json

    region = region or DEFAULT_REGION

    # Build boto3 client with optional assume role
    if assume_role_arn:
        try:
            sts = boto3.client('sts')
            assumed = sts.assume_role(
                RoleArn=assume_role_arn,
                RoleSessionName='bouncer-native-execution',
                DurationSeconds=900,
            )
            creds = assumed['Credentials']
            client = boto3.client(
                service,
                region_name=region,
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretAccessKey'],
                aws_session_token=creds['SessionToken'],
            )
        except ClientError as e:
            return f'❌ Assume role 失敗: {str(e)}'
    else:
        client = boto3.client(service, region_name=region)

    # Auto-normalize service name to lowercase (e.g. EC2 → ec2, DynamoDB → dynamodb)
    service = service.lower()

    # Auto-convert PascalCase to snake_case (e.g. DescribeInstances → describe_instances)
    import re as _re
    if not hasattr(client, operation) and operation[0].isupper():
        snake = _re.sub(r'(?<!^)(?=[A-Z])', '_', operation).lower()
        if hasattr(client, snake):
            operation = snake

    # Validate service and operation exist
    if not hasattr(client, operation):
        return f'❌ 不支援的操作: {service}.{operation}'

    # Execute
    try:
        method = getattr(client, operation)
        response = method(**params)
        # Remove ResponseMetadata (not useful to user)
        response.pop('ResponseMetadata', None)
        return json.dumps(response, default=str, indent=2) if response else '⚠️ 命令執行完成（無輸出，請確認結果）'
    except ClientError as e:
        return f'❌ AWS API 錯誤: {e.response["Error"]["Code"]}: {e.response["Error"]["Message"]}'
    except Exception as e:  # noqa: BLE001
        return f'❌ 執行錯誤: {str(e)}'


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


def _execute_chain(sub_cmds: list[str], assume_role_arn: Optional[str],
                   _executor: Optional[Callable] = None,
                   cli_input_json: dict = None) -> CommandChainResult:
    """Execute a list of sub-commands sequentially with && semantics."""
    chain = CommandChainResult()

    for idx, cmd in enumerate(sub_cmds):
        with _execute_lock:
            if _executor is not None:
                output = _executor(cmd, assume_role_arn, cli_input_json)
            else:
                output = _execute_locked(cmd, assume_role_arn, cli_input_json=cli_input_json)

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


def _run_aws_subprocess(cli_args: list, env_override: dict = None, timeout: int = 55) -> tuple:
    """Run aws CLI command via subprocess (awscli v2 Lambda Layer or system aws).

    Returns (exit_code, stdout, stderr).
    Lambda Layer path: /opt/bin/aws (ARM64 awscli v2 layer)
    Fallback: system 'aws' command (local dev / CI)
    """
    import subprocess as _subprocess

    aws_binary = '/opt/bin/aws'
    if not os.path.exists(aws_binary):
        aws_binary = 'aws'

    run_env = os.environ.copy()
    run_env['AWS_PAGER'] = ''
    if env_override:
        run_env.update(env_override)

    try:
        result = _subprocess.run(
            [aws_binary] + cli_args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
        return result.returncode, result.stdout, result.stderr
    except _subprocess.TimeoutExpired:
        return 1, '', f'命令執行超時（{timeout}秒）'
    except FileNotFoundError:
        return 1, '', (
            '❌ bouncer_execute 已停用（awscli 已從 Lambda 移除）。\n'
            '請改用 bouncer_execute_native（boto3 native 格式）：\n'
            '  mcporter call bouncer bouncer_execute_native --args \'{"aws":{"service":"...","operation":"...","params":{}},"bouncer":{"reason":"...","trust_scope":"...","source":"..."}}\'\n'
            '詳見 README 或 TOOLS.md。'
        )


def _execute_locked(command: str, assume_role_arn: str = None,
                    cli_input_json: dict = None) -> str:
    """execute_command 的實作，必須在 _execute_lock 持有時呼叫。"""
    import tempfile as _tempfile

    _cli_input_tmp = None
    try:
        # Write cli_input_json to tempfile if provided
        if cli_input_json is not None:
            with _tempfile.NamedTemporaryFile(
                mode='w', suffix='.json', delete=False, dir='/tmp', prefix='bouncer_cli_'  # nosec B108
            ) as _f:
                import json as _json
                _json.dump(cli_input_json, _f, ensure_ascii=False)
                _cli_input_tmp = _f.name
            command = command + f' --cli-input-json file://{_cli_input_tmp}'

        # 解析命令字串為 argv list
        args = aws_cli_split(command)

        if not args or args[0] != 'aws':
            return '❌ 只能執行 aws CLI 命令'

        # 移除 'aws' 前綴，subprocess 需要 'aws' 作為第一個參數但不包含在 cli_args 裡
        cli_args = args[1:]

        # Guard: awscli v1 global flags that conflict with subcommand params.
        # '--version' is a known conflict: 'eks create-cluster --version 1.32'
        # triggers the global --version flag (prints awscli version, sys.exit(0))
        # instead of being parsed as the EKS Kubernetes version parameter.
        # Callers must use the correct subcommand-specific flag names, e.g.:
        # - EKS: --kubernetes-version (not --version)
        # See: awscli/argparser.py MainArgParser._build() version action.
        _AWSCLI_GLOBAL_NO_VALUE_FLAGS = frozenset({
            '--version', '--debug', '--no-verify-ssl', '--no-paginate',
            '--no-sign-request',
        })
        _conflicting = [f for f in cli_args if f in _AWSCLI_GLOBAL_NO_VALUE_FLAGS
                        and cli_args.index(f) > 0]  # not at position 0 = likely subcommand param
        if '--version' in _conflicting:
            return (
                '❌ `--version` 與 awscli v1 全域 flag 衝突，無法作為 subcommand 參數使用。\n'
                '請改用正確的 subcommand 參數名，例如：\n'
                '  EKS: `--kubernetes-version <版本號>`（不是 `--version`）'
            )

        # 準備環境變數覆蓋（包含 assume role credentials）
        env_override = {}

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

                # 設定環境變數覆蓋讓 aws CLI 使用這些 credentials
                env_override = {
                    'AWS_ACCESS_KEY_ID': creds['AccessKeyId'],
                    'AWS_SECRET_ACCESS_KEY': creds['SecretAccessKey'],
                    'AWS_SESSION_TOKEN': creds['SessionToken'],
                }

            except ClientError as e:
                return f'❌ Assume role 失敗: {str(e)}'

        # 執行 aws CLI 命令（透過 subprocess）
        exit_code, stdout_output, stderr_output = _run_aws_subprocess(
            cli_args,
            env_override=env_override if env_override else None,
            timeout=55
        )

        output = stdout_output or stderr_output or ''

        if exit_code == 0:
            if not output.strip():
                output = '⚠️ 命令執行完成（無輸出，請確認結果）'
        else:
            # 顯示完整原始輸出（和直接跑 CLI 一樣），加上 exit code 提示
            if output.strip():
                output = f'{output}\n\n(exit code: {exit_code})'
            else:
                output = f'(exit code: {exit_code})'

        return output  # 不截斷，讓呼叫端用 store_paged_output 處理

    except ValueError as e:
        return f'❌ 命令格式錯誤: {str(e)}'
    except Exception as e:  # noqa: BLE001 — fail-closed AWS CLI execution wrapper
        return f'❌ 執行錯誤: {str(e)}'
    finally:
        # Cleanup tempfile
        if _cli_input_tmp:
            try:
                import os as _os
                _os.unlink(_cli_input_tmp)
            except Exception:  # noqa: BLE001
                logger.debug("Failed to delete temporary CLI input file", exc_info=True)
