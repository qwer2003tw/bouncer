"""
Bouncer - Grant Session 模組
處理批次權限授予（Grant Session）的建立、查詢、批准、撤銷和命令匹配

Grant Session 允許 Agent 預先申請一批命令的執行權限，
經人工審批後，Agent 可以在 TTL 內自動執行已授權的命令。
"""
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

__all__ = [
    'normalize_command',
    'create_grant_request',
    'get_grant_session',
    'approve_grant',
    'deny_grant',
    'revoke_grant',
    'is_command_in_grant',
    'try_use_grant_command',
    'get_grant_status',
]


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
        print(f"[GRANT] normalize_command error: {e}")
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
        print(f"[GRANT] create_grant_request error: {e}")
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
            print(f"[GRANT] risk scoring error: {e}")
            # Fail-open for risk scoring: treat as grantable
            detail['risk_score'] = 0

    except Exception as e:
        print(f"[GRANT] precheck error for command '{command[:100]}': {e}")
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
        print(f"[GRANT] get_grant_session error: {e}")
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
        print(f"[GRANT] approve_grant error: {e}")
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
        print(f"[GRANT] deny_grant error: {e}")
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
        print(f"[GRANT] revoke_grant error: {e}")
        return False


def is_command_in_grant(normalized_cmd: str, grant: Dict) -> bool:
    """檢查正規化命令是否在 Grant 授權清單中

    使用 exact match（正規化後比較）。

    Args:
        normalized_cmd: 已正規化的命令
        grant: Grant session dict

    Returns:
        命令是否在授權清單
    """
    try:
        granted_commands = grant.get('granted_commands', [])
        return normalized_cmd in granted_commands
    except Exception as e:
        print(f"[GRANT] is_command_in_grant error: {e}")
        return False


def try_use_grant_command(
    grant_id: str,
    normalized_cmd: str,
    allow_repeat: bool = False,
) -> bool:
    """原子性標記命令已使用（DynamoDB conditional update）

    防並發：使用 ConditionExpression 確保原子性。

    Args:
        grant_id: Grant ID
        normalized_cmd: 已正規化的命令
        allow_repeat: 是否允許重複執行

    Returns:
        True 如果成功標記, False 如果已用過或並發衝突
    """
    try:
        if allow_repeat:
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
        print(f"[GRANT] try_use_grant_command ClientError: {e}")
        return False
    except Exception as e:
        print(f"[GRANT] try_use_grant_command error: {e}")
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
        print(f"[GRANT] get_grant_status error: {e}")
        return None
