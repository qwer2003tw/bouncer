"""
Bouncer - Grant Session 模組
處理批次權限授予（Grant Session）的建立、查詢、批准、撤銷和命令匹配

Grant Session 允許 Agent 預先申請一批命令的執行權限，
經人工審批後，Agent 可以在 TTL 內自動執行已授權的命令。

Pattern Matching（Approach B — 積極）
--------------------------------------
granted_commands 中每一條可以是：
  1. 精確字串（原有行為）
  2. Glob pattern：支援 * 和 **（fnmatch 語意）
  3. Named placeholder：{uuid}, {date}, {any}, {bucket}, {key}, {name}, ...

Named placeholder 語意：
  {uuid}    → 12-36 位 hex 字元（含連字號，例如 UUID v4）
  {date}    → YYYY-MM-DD 格式日期
  {any}     → 任意非空白字元序列（不跨空格）
  {bucket}  → 任意非空白字元（S3 bucket/key 友善，不含空格）
  {key}     → 同 {any}，S3 key 語意（允許 / 及特殊字元）
  {name}    → 同 {any}，通用命名語意
  {<other>} → 任意非空白字元序列（與 {any} 相同）

** 在 S3 路徑等場合匹配任意字串（含空白、/），
*  匹配任意非空白字元序列（不含空格）。

範例：
  aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/*.html \\
      s3://ztp-files-dev-frontendbucket-nvvimv31xp3v/*.html
"""
import logging
import re
import secrets
import time
from typing import Optional, Dict, List, Any

from botocore.exceptions import ClientError

from db import table

from constants import (

    GRANT_MAX_TTL_MINUTES,
    GRANT_DEFAULT_TTL_MINUTES,
    GRANT_MAX_COMMANDS,
    GRANT_MAX_TOTAL_EXECUTIONS,
    GRANT_APPROVAL_TIMEOUT,
)

logger = logging.getLogger(__name__)

__all__ = [
    'normalize_command',
    'compile_pattern',
    'match_pattern',
    'create_grant_request',
    'get_grant_session',
    'approve_grant',
    'deny_grant',
    'revoke_grant',
    'is_command_in_grant',
    'try_use_grant_command',
    'get_grant_status',
]

# ---------------------------------------------------------------------------
# Named placeholder regex map
# Each value is a raw regex fragment (no anchors, no groups needed by user).
# ---------------------------------------------------------------------------
_PLACEHOLDER_PATTERNS: Dict[str, str] = {
    # UUID: hex chars + optional hyphens, 12-36 chars total
    'uuid':   r'[0-9a-f][0-9a-f\-]{10,34}[0-9a-f]',
    # ISO date: YYYY-MM-DD
    'date':   r'\d{4}-\d{2}-\d{2}',
    # Generic non-whitespace sequences
    'any':    r'\S+',
    'bucket': r'\S+',
    'key':    r'\S+',
    'name':   r'\S+',
}
_DEFAULT_PLACEHOLDER_RE = r'\S+'  # fallback for unknown placeholder names


def _is_pattern(s: str) -> bool:
    """判斷字串是否含有 pattern 語法（glob 萬用字元或 named placeholder）。"""
    return '*' in s or ('{' in s and '}' in s)


def compile_pattern(pattern: str) -> re.Pattern:
    """將 pattern 字串編譯為正規表示式物件。

    支援：
      - Named placeholder：{uuid}, {date}, {any}, {bucket}, {key}, {name},
        以及任意未知名稱（fallback 為 \\S+）
      - Glob wildcards：** 匹配任意字元（含空白、/）；* 匹配任意非空白序列

    Args:
        pattern: 原始 pattern 字串（應已正規化，即全小寫、空白壓縮）

    Returns:
        編譯後的 re.Pattern，使用全字串匹配（re.IGNORECASE）

    Raises:
        ValueError: 如果 pattern 長度超過上限、wildcard 過多、含不合法連續 wildcard，或 regex 編譯失敗
    """
    # ── 前置驗證（bouncer-sec-008：ReDoS prevention）────────────────────────
    if len(pattern) > 256:
        raise ValueError(
            f"Pattern 長度超過上限（256 字元），目前 {len(pattern)} 字元"
        )

    # 連續 3+ 個 star（必須在 wildcard 計數之前檢查）
    if '***' in pattern:
        raise ValueError("Pattern 含有不合法的連續 wildcard（***）")

    # 計算 wildcard 數量（排除 {placeholder} 內的 *）
    pattern_no_placeholders = re.sub(r'\{[^}]*\}', '', pattern)
    wildcard_count = pattern_no_placeholders.count('*')
    if wildcard_count > 10:
        raise ValueError(
            f"Pattern 含有過多 wildcard（{wildcard_count} 個，上限 10）"
        )
    # ── 前置驗證結束 ─────────────────────────────────────────────────────────

    # Step 1: 先把 named placeholder 替換為 sentinel，避免後續轉義干擾
    # 格式：{name}  → 只允許合法識別符（字母 + 底線）
    placeholder_re = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')

    parts: List[str] = []
    last_end = 0

    for m in placeholder_re.finditer(pattern):
        # 把 placeholder 前面的文字先 regex-escape，再處理 glob
        before = pattern[last_end:m.start()]
        parts.append(_glob_to_regex(before))
        # 把 placeholder 轉為對應 regex fragment
        name = m.group(1).lower()
        frag = _PLACEHOLDER_PATTERNS.get(name, _DEFAULT_PLACEHOLDER_RE)
        parts.append(f'(?:{frag})')
        last_end = m.end()

    # 剩餘尾部
    parts.append(_glob_to_regex(pattern[last_end:]))

    full_regex = ''.join(parts)
    try:
        return re.compile(f'^{full_regex}$', re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Pattern 編譯失敗：{e}") from e


def _glob_to_regex(text: str) -> str:
    """將 glob 語法片段（含 * 和 **）轉為 regex 字串（不含錨點）。

    處理順序：
      1. 先 re.escape 整個字串（保護所有特殊字元）
      2. 將 escaped ** → 匹配任意字元（含空白）
      3. 將 escaped *  → 匹配非空白序列

    Note: re.escape('**') == r'\\*\\*'，re.escape('*') == r'\\*'
    """
    if not text:
        return ''

    # Replace ** first (before single *), then *
    escaped = re.escape(text)
    # re.escape turns * into \* and ** into \*\*
    # Replace \*\* → .*  (match anything including spaces and slashes)
    escaped = escaped.replace(r'\*\*', '.*')
    # Replace remaining \* → \S* (match non-whitespace, 0 or more)
    escaped = escaped.replace(r'\*', r'\S*')
    return escaped


def match_pattern(pattern: str, normalized_cmd: str) -> bool:
    """判斷正規化命令是否符合 pattern。

    Pattern 會被 compile_pattern 編譯後與命令做全字串比對。
    如果 pattern 不含任何 pattern 語法，退化為 exact match（等同 ==）。

    Args:
        pattern: grant pattern（已正規化）
        normalized_cmd: 待比對的正規化命令

    Returns:
        True 如果匹配，False 否則
    """
    try:
        if not _is_pattern(pattern):
            return pattern == normalized_cmd
        compiled = compile_pattern(pattern)
        return bool(compiled.match(normalized_cmd))
    except Exception as e:
        logger.error(f"[GRANT] match_pattern error for pattern={pattern!r}: {e}")
        return False


def normalize_command(command: str) -> str:
    """正規化命令用於比對

    1. strip 前後空白
    2. 連續空白壓縮為單一空格
    3. 全部小寫

    Args:
        command: 原始 AWS CLI 命令

    Returns:
        正規化後的命令字串
    """
    try:
        cmd = command.strip()
        cmd = ' '.join(cmd.split())
        cmd = cmd.lower()
        return cmd
    except Exception as e:
        logger.error(f"[GRANT] normalize_command error: {e}")
        return command.strip().lower() if command else ''


def create_grant_request(
    commands: List[str],
    reason: str,
    source: str,
    account_id: str,
    ttl_minutes: int = None,
    allow_repeat: bool = False,
) -> Dict[str, Any]:
    """建立 Grant 請求

    對每個命令做完整預檢，分類為 grantable / requires_individual / blocked。
    結果存入 DynamoDB，狀態為 pending_approval。

    Args:
        commands: 命令清單
        reason: 申請原因
        source: 請求來源
        account_id: AWS 帳號 ID
        ttl_minutes: TTL（分鐘），預設 30，最大 60
        allow_repeat: 是否允許重複執行同一命令

    Returns:
        dict with grant_id, summary, commands_detail, etc.

    Raises:
        ValueError: 參數驗證失敗
    """
    try:
        # 參數驗證
        if not commands or len(commands) == 0:
            raise ValueError("commands 不能為空")
        if len(commands) > GRANT_MAX_COMMANDS:
            raise ValueError(f"commands 數量不能超過 {GRANT_MAX_COMMANDS}，目前 {len(commands)}")
        if not reason:
            raise ValueError("reason 不能為空")
        if not source:
            raise ValueError("source 不能為空")

        # TTL 驗證
        if ttl_minutes is None:
            ttl_minutes = GRANT_DEFAULT_TTL_MINUTES
        ttl_minutes = max(1, min(ttl_minutes, GRANT_MAX_TTL_MINUTES))

        # 生成 grant_id
        grant_id = f"grant_{secrets.token_hex(16)}"

        # 預檢每個命令
        commands_detail = []
        summary = {'total': len(commands), 'grantable': 0, 'requires_individual': 0, 'blocked': 0}

        for cmd in commands:
            normalized = normalize_command(cmd)
            detail = _precheck_command(cmd, normalized, reason, source, account_id)
            commands_detail.append(detail)
            summary[detail['category']] += 1

        now = int(time.time())
        approval_timeout_at = now + GRANT_APPROVAL_TIMEOUT

        # 存入 DynamoDB
        item = {
            'request_id': grant_id,
            'type': 'grant_session',
            'action': 'grant_session',
            'status': 'pending_approval',
            'source': source,
            'account_id': account_id,
            'reason': reason,
            'ttl_minutes': ttl_minutes,
            'commands_detail': commands_detail,
            'granted_commands': [],  # 批准後才填
            'used_commands': {},
            'total_executions': 0,
            'max_total_executions': GRANT_MAX_TOTAL_EXECUTIONS,
            'allow_repeat': allow_repeat,
            'created_at': now,
            'ttl': approval_timeout_at,  # DynamoDB TTL: 審批超時就過期
        }

        table.put_item(Item=item)

        return {
            'grant_id': grant_id,
            'status': 'pending_approval',
            'summary': summary,
            'commands_detail': commands_detail,
            'ttl_minutes': ttl_minutes,
            'allow_repeat': allow_repeat,
            'expires_in': GRANT_APPROVAL_TIMEOUT,
        }

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"[GRANT] create_grant_request error: {e}")
        raise


def _precheck_command(
    command: str,
    normalized: str,
    reason: str,
    source: str,
    account_id: str,
) -> Dict[str, Any]:
    """預檢單一命令，分類為 grantable / requires_individual / blocked

    Args:
        command: 原始命令
        normalized: 正規化後的命令
        reason: 申請原因
        source: 請求來源
        account_id: AWS 帳號 ID

    Returns:
        dict with command, normalized, category, risk_score, reason
    """
    detail = {
        'command': command,
        'normalized': normalized,
        'category': 'grantable',
        'risk_score': 0,
        'block_reason': None,
    }

    try:
        # 1. Compliance check
        try:
            from compliance_checker import check_compliance
            is_compliant, violation = check_compliance(command)
            if not is_compliant:
                detail['category'] = 'blocked'
                detail['block_reason'] = f"合規違規: {violation.rule_name}" if violation else "合規違規"
                return detail
        except ImportError:
            pass

        # 2. Blocked check
        from commands import is_blocked
        if is_blocked(command):
            detail['category'] = 'blocked'
            detail['block_reason'] = '在封鎖清單'
            return detail

        # 3. Trust excluded (高危)
        from trust import is_trust_excluded
        if is_trust_excluded(command):
            detail['category'] = 'requires_individual'
            detail['block_reason'] = '高危命令，需個別審批'
            return detail

        # 4. Risk score
        try:
            from risk_scorer import calculate_risk
            risk = calculate_risk(command, reason=reason, source=source, account_id=account_id)
            score = risk.score if hasattr(risk, 'score') else 0
            detail['risk_score'] = score
            if score >= 66:
                detail['category'] = 'requires_individual'
                detail['block_reason'] = f'風險分數 {score} >= 66'
        except Exception as e:
            logger.error(f"[GRANT] risk scoring error: {e}")
            # Fail-open for risk scoring: treat as grantable
            detail['risk_score'] = 0

    except Exception as e:
        logger.error(f"[GRANT] precheck error for command '{command[:100]}': {e}")
        # Fail-closed: 預檢失敗 → requires_individual
        detail['category'] = 'requires_individual'
        detail['block_reason'] = f'預檢失敗: {str(e)}'

    return detail


def get_grant_session(grant_id: str) -> Optional[Dict]:
    """查詢 Grant Session

    Args:
        grant_id: Grant ID

    Returns:
        Grant session dict, or None
    """
    try:
        if not grant_id:
            return None
        result = table.get_item(Key={'request_id': grant_id})
        item = result.get('Item')
        if item and item.get('type') == 'grant_session':
            return item
        return None
    except Exception as e:
        logger.error(f"[GRANT] get_grant_session error: {e}")
        return None


def approve_grant(grant_id: str, approved_by: str, mode: str = 'all') -> Optional[Dict]:
    """批准 Grant Session

    TTL 從批准時算起。

    Args:
        grant_id: Grant ID
        approved_by: 批准者 user_id
        mode: 'all' 批准所有可授權命令, 'safe_only' 只批准 grantable 的

    Returns:
        Updated grant dict, or None on failure
    """
    try:
        grant = get_grant_session(grant_id)
        if not grant:
            return None

        if grant.get('status') != 'pending_approval':
            return None

        commands_detail = grant.get('commands_detail', [])

        # 決定哪些命令被批准
        if mode == 'all':
            granted = [d['normalized'] for d in commands_detail if d['category'] in ('grantable', 'requires_individual')]
        else:  # safe_only
            granted = [d['normalized'] for d in commands_detail if d['category'] == 'grantable']

        now = int(time.time())
        ttl_minutes = int(grant.get('ttl_minutes', GRANT_DEFAULT_TTL_MINUTES))
        expires_at = now + ttl_minutes * 60

        table.update_item(
            Key={'request_id': grant_id},
            UpdateExpression=(
                'SET #status = :status, approved_by = :approver, approved_at = :now, '
                'granted_commands = :granted, expires_at = :expires, '
                'approval_mode = :mode, #ttl = :ttl_val'
            ),
            ExpressionAttributeNames={
                '#status': 'status',
                '#ttl': 'ttl',
            },
            ExpressionAttributeValues={
                ':status': 'active',
                ':approver': approved_by,
                ':now': now,
                ':granted': granted,
                ':expires': expires_at,
                ':mode': mode,
                ':ttl_val': expires_at,  # DynamoDB TTL: 到期自動清理
            },
        )

        # 回傳更新後的資訊
        grant['status'] = 'active'
        grant['approved_by'] = approved_by
        grant['approved_at'] = now
        grant['granted_commands'] = granted
        grant['expires_at'] = expires_at
        grant['approval_mode'] = mode
        return grant

    except Exception as e:
        logger.error(f"[GRANT] approve_grant error: {e}")
        return None


def deny_grant(grant_id: str) -> bool:
    """拒絕 Grant Session

    Args:
        grant_id: Grant ID

    Returns:
        是否成功
    """
    try:
        table.update_item(
            Key={'request_id': grant_id},
            UpdateExpression='SET #status = :status, denied_at = :now',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'denied',
                ':now': int(time.time()),
            },
        )
        return True
    except Exception as e:
        logger.error(f"[GRANT] deny_grant error: {e}")
        return False


def revoke_grant(grant_id: str) -> bool:
    """撤銷 Grant Session

    Args:
        grant_id: Grant ID

    Returns:
        是否成功
    """
    try:
        table.update_item(
            Key={'request_id': grant_id},
            UpdateExpression='SET #status = :status, revoked_at = :now',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'revoked',
                ':now': int(time.time()),
            },
        )
        return True
    except Exception as e:
        logger.error(f"[GRANT] revoke_grant error: {e}")
        return False


def is_command_in_grant(normalized_cmd: str, grant: Dict) -> bool:
    """檢查正規化命令是否在 Grant 授權清單中

    比對策略（Approach B — 積極）：
      1. 先做 O(1) exact match（向後相容，效能最佳）
      2. Exact match 不命中時，逐一對每條 granted pattern 做 match_pattern()
         - 支援 glob（* / **）及 named placeholder（{uuid}/{date}/{any}/...）

    Args:
        normalized_cmd: 已正規化的命令
        grant: Grant session dict

    Returns:
        命令是否在授權清單（包含 pattern 匹配）
    """
    try:
        granted_commands = grant.get('granted_commands', [])

        # Fast path: exact match
        if normalized_cmd in granted_commands:
            return True

        # Pattern match path
        for pat in granted_commands:
            if _is_pattern(pat) and match_pattern(pat, normalized_cmd):
                return True

        return False
    except Exception as e:
        logger.error(f"[GRANT] is_command_in_grant error: {e}")
        return False


def try_use_grant_command(
    grant_id: str,
    normalized_cmd: str,
    allow_repeat: bool = False,
) -> bool:
    """原子性標記命令已使用（DynamoDB conditional update）

    防並發：使用 ConditionExpression 確保原子性。

    SEC-009: allow_repeat=True 的危險命令限制最多執行 3 次。

    Args:
        grant_id: Grant ID
        normalized_cmd: 已正規化的命令
        allow_repeat: 是否允許重複執行

    Returns:
        True 如果成功標記, False 如果已用過或並發衝突
    """
    _DANGEROUS_REPEAT_LIMIT = 3

    try:
        if allow_repeat:
            # SEC-009: 危險命令檢查 — 即使 allow_repeat 也限制最多 3 次
            from commands import is_dangerous
            if is_dangerous(normalized_cmd):
                # 讀取目前計數
                try:
                    result = table.get_item(Key={'request_id': grant_id})
                    grant_item = result.get('Item', {})
                    used_commands = grant_item.get('used_commands', {})
                    current_count = int(used_commands.get(normalized_cmd, 0))
                    if current_count >= _DANGEROUS_REPEAT_LIMIT:
                        logger.warning(f"[GRANT][SEC-009] Dangerous command repeat limit reached: {normalized_cmd[:80]!r}")
                        return False
                except Exception as e:
                    logger.error(f"[GRANT][SEC-009] Failed to read repeat count: {e}")
                    return False

            # 允許重複：只增加計數 + 總次數
            table.update_item(
                Key={'request_id': grant_id},
                UpdateExpression=(
                    'SET used_commands.#cmd = if_not_exists(used_commands.#cmd, :zero) + :one, '
                    'total_executions = if_not_exists(total_executions, :zero) + :one'
                ),
                ConditionExpression='#status = :active AND total_executions < max_total_executions',
                ExpressionAttributeNames={
                    '#cmd': normalized_cmd,
                    '#status': 'status',
                },
                ExpressionAttributeValues={
                    ':zero': 0,
                    ':one': 1,
                    ':active': 'active',
                },
            )
        else:
            # 一次性：命令不能已存在於 used_commands
            table.update_item(
                Key={'request_id': grant_id},
                UpdateExpression=(
                    'SET used_commands.#cmd = :one, '
                    'total_executions = if_not_exists(total_executions, :zero) + :one'
                ),
                ConditionExpression=(
                    '#status = :active AND attribute_not_exists(used_commands.#cmd) '
                    'AND total_executions < max_total_executions'
                ),
                ExpressionAttributeNames={
                    '#cmd': normalized_cmd,
                    '#status': 'status',
                },
                ExpressionAttributeValues={
                    ':zero': 0,
                    ':one': 1,
                    ':active': 'active',
                },
            )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False  # 已用過或並發衝突
        logger.error(f"[GRANT] try_use_grant_command ClientError: {e}")
        return False
    except Exception as e:
        logger.error(f"[GRANT] try_use_grant_command error: {e}")
        return False


def get_grant_status(grant_id: str, source: str) -> Optional[Dict]:
    """查詢 Grant 狀態（含 source 驗證）

    Args:
        grant_id: Grant ID
        source: 請求來源（必須匹配才能查詢）

    Returns:
        Grant status dict, or None
    """
    try:
        grant = get_grant_session(grant_id)
        if not grant:
            return None

        # 驗證 source
        if grant.get('source') != source:
            return None

        now = int(time.time())
        status = grant.get('status', '')
        granted_commands = grant.get('granted_commands', [])
        used_commands = grant.get('used_commands', {})
        total_executions = int(grant.get('total_executions', 0))
        max_total = int(grant.get('max_total_executions', GRANT_MAX_TOTAL_EXECUTIONS))
        expires_at = int(grant.get('expires_at', 0))

        # 計算剩餘時間
        remaining_seconds = max(0, expires_at - now) if expires_at > 0 else 0

        # 如果已過期但 status 還是 active，標記為 expired
        if status == 'active' and remaining_seconds == 0:
            status = 'expired'

        return {
            'grant_id': grant_id,
            'status': status,
            'granted_count': len(granted_commands),
            'used_count': len(used_commands),
            'total_executions': total_executions,
            'max_total_executions': max_total,
            'remaining_seconds': remaining_seconds,
            'allow_repeat': grant.get('allow_repeat', False),
        }

    except Exception as e:
        logger.error(f"[GRANT] get_grant_status error: {e}")
        return None
