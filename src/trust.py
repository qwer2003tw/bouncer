"""
Bouncer - Trust Session 模組
處理信任時段的建立、查詢、撤銷和自動批准判斷

信任匹配基於 trust_scope + account_id：
- trust_scope 是呼叫端提供的穩定識別符（如 session key）
- source 僅用於顯示，不參與信任匹配
"""
import time
import hashlib
from typing import Optional, Dict

import boto3


from constants import (
    TABLE_NAME,
    TRUST_SESSION_ENABLED, TRUST_SESSION_DURATION, TRUST_SESSION_MAX_COMMANDS,
    TRUST_EXCLUDED_SERVICES, TRUST_EXCLUDED_ACTIONS, TRUST_EXCLUDED_FLAGS,
    TRUST_UPLOAD_MAX_BYTES_PER_FILE,
    TRUST_UPLOAD_MAX_BYTES_TOTAL, TRUST_UPLOAD_BLOCKED_EXTENSIONS,
)

__all__ = [
    'get_trust_session',
    'create_trust_session',
    'revoke_trust_session',
    'increment_trust_command_count',
    'increment_trust_upload_count',
    'is_trust_excluded',
    'should_trust_approve',
    'should_trust_approve_upload',
]

# DynamoDB - lazy init
_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource('dynamodb')
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def get_trust_session(trust_scope: str, account_id: str) -> Optional[Dict]:
    """
    查詢有效的信任時段

    Args:
        trust_scope: 信任範圍識別符（session key 等）
        account_id: AWS 帳號 ID

    Returns:
        信任時段記錄，或 None
    """
    if not TRUST_SESSION_ENABLED or not trust_scope:
        return None

    # 用 trust_scope 算出 trust_id 直接 get（不用 scan）
    scope_hash = hashlib.sha256(trust_scope.encode()).hexdigest()[:16]
    trust_id = f"trust-{scope_hash}-{account_id}"

    now = int(time.time())

    try:
        response = _get_table().get_item(Key={'request_id': trust_id})
        item = response.get('Item')

        if not item:
            return None

        # 驗證未過期
        if int(item.get('expires_at', 0)) <= now:
            return None

        # 驗證類型
        if item.get('type') != 'trust_session':
            return None

        return item

    except Exception as e:
        print(f"Trust session check error: {e}")
        return None


def create_trust_session(trust_scope: str, account_id: str, approved_by: str,
                         source: str = '', max_uploads: int = 0) -> str:
    """
    建立信任時段

    Args:
        trust_scope: 信任範圍識別符（session key 等）
        account_id: AWS 帳號 ID
        approved_by: 批准者 ID
        source: 顯示用來源描述（不參與匹配）
        max_uploads: 信任期間最大上傳次數（0=不允許信任上傳）

    Returns:
        trust_id
    """
    scope_hash = hashlib.sha256(trust_scope.encode()).hexdigest()[:16]
    trust_id = f"trust-{scope_hash}-{account_id}"

    now = int(time.time())
    expires_at = now + TRUST_SESSION_DURATION

    item = {
        'request_id': trust_id,
        'type': 'trust_session',
        'trust_scope': trust_scope,
        'source': source or trust_scope,  # GSI 不接受空字串
        'account_id': account_id,
        'approved_by': approved_by,
        'created_at': now,
        'expires_at': expires_at,
        'command_count': 0,
        'max_uploads': max_uploads,
        'upload_count': 0,
        'upload_bytes_total': 0,
        'ttl': expires_at
    }

    _get_table().put_item(Item=item)
    return trust_id


def revoke_trust_session(trust_id: str) -> bool:
    """
    撤銷信任時段

    Args:
        trust_id: 信任時段 ID

    Returns:
        是否成功
    """
    try:
        _get_table().delete_item(Key={'request_id': trust_id})
        return True
    except Exception as e:
        print(f"Revoke trust session error: {e}")
        return False


def increment_trust_command_count(trust_id: str) -> int:
    """
    原子性增加信任時段的命令計數（SEC-007）

    使用 DynamoDB conditional update 確保並發安全：
    - 只有在 command_count 未超限且 session 仍有效時才增加
    - ConditionalCheckFailedException → return 0 (拒絕)

    Returns:
        新的計數值，或 0（條件不滿足）
    """
    now = int(time.time())
    try:
        response = _get_table().update_item(
            Key={'request_id': trust_id},
            UpdateExpression='SET command_count = if_not_exists(command_count, :zero) + :one',
            ConditionExpression='command_count < :max AND #status = :active AND expires_at > :now',
            ExpressionAttributeNames={
                '#status': 'type',  # 'type' = 'trust_session' is our status indicator
            },
            ExpressionAttributeValues={
                ':zero': 0,
                ':one': 1,
                ':max': TRUST_SESSION_MAX_COMMANDS,
                ':active': 'trust_session',
                ':now': now,
            },
            ReturnValues='UPDATED_NEW'
        )
        return int(response.get('Attributes', {}).get('command_count', 0))
    except _get_table().meta.client.exceptions.ConditionalCheckFailedException:
        print(f"Trust command count conditional update failed for {trust_id} (limit or expired)")
        return 0
    except Exception as e:
        print(f"Increment trust command count error: {e}")
        return 0


def is_trust_excluded(command: str) -> bool:
    """
    檢查命令是否被 Trust Session 排除（高危命令）

    Args:
        command: AWS CLI 命令

    Returns:
        True 如果命令被排除，False 如果可以信任
    """
    cmd_lower = command.lower()

    # 檢查是否是高危服務
    for service in TRUST_EXCLUDED_SERVICES:
        if f'aws {service} ' in cmd_lower or f'aws {service}\t' in cmd_lower:
            return True

    # 檢查是否是高危操作
    for action in TRUST_EXCLUDED_ACTIONS:
        if action in cmd_lower:
            return True

    # 檢查是否有危險旗標
    for flag in TRUST_EXCLUDED_FLAGS:
        if flag in cmd_lower:
            return True

    return False


def should_trust_approve(command: str, trust_scope: str, account_id: str) -> tuple:
    """
    檢查是否應該透過信任時段自動批准

    Args:
        command: AWS CLI 命令
        trust_scope: 信任範圍識別符
        account_id: AWS 帳號 ID

    Returns:
        (should_approve: bool, trust_session: dict or None, reason: str)
    """
    if not TRUST_SESSION_ENABLED or not trust_scope:
        return False, None, "Trust session disabled or no trust_scope"

    # 檢查是否有有效的信任時段
    session = get_trust_session(trust_scope, account_id)
    if not session:
        return False, None, "No active trust session"

    # 檢查命令計數
    if session.get('command_count', 0) >= TRUST_SESSION_MAX_COMMANDS:
        return False, session, f"Trust session command limit reached ({TRUST_SESSION_MAX_COMMANDS})"

    # 使用統一的排除檢查
    if is_trust_excluded(command):
        return False, session, "Command excluded from trust"

    # 計算剩餘時間
    remaining = int(session.get('expires_at', 0)) - int(time.time())
    if remaining <= 0:
        return False, None, "Trust session expired"

    return True, session, f"Trust session active ({remaining}s remaining)"


def _is_upload_filename_safe(filename: str) -> bool:
    """
    檢查檔名是否安全（無 path traversal、null bytes 等）

    Args:
        filename: 檔案名稱

    Returns:
        True 如果安全
    """
    if not filename:
        return False
    # null bytes
    if '\x00' in filename:
        return False
    # path traversal
    if '..' in filename:
        return False
    # absolute paths or directory separators
    if '/' in filename or '\\' in filename:
        return False
    return True


def _is_upload_extension_blocked(filename: str) -> bool:
    """
    檢查檔案副檔名是否在黑名單

    Args:
        filename: 檔案名稱

    Returns:
        True 如果副檔名被封鎖
    """
    lower = filename.lower()
    for ext in TRUST_UPLOAD_BLOCKED_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def should_trust_approve_upload(trust_scope: str, account_id: str,
                                filename: str, content_size: int) -> tuple:
    """
    檢查是否應該透過信任時段自動批准上傳

    Args:
        trust_scope: 信任範圍識別符
        account_id: AWS 帳號 ID
        filename: 上傳檔案名稱
        content_size: 檔案大小（bytes）

    Returns:
        (should_approve: bool, trust_session: dict or None, reason: str)
    """
    if not TRUST_SESSION_ENABLED or not trust_scope:
        return False, None, "Trust session disabled or no trust_scope"

    # 檔名安全檢查
    if not _is_upload_filename_safe(filename):
        return False, None, "Filename contains unsafe characters"

    # 副檔名黑名單
    if _is_upload_extension_blocked(filename):
        return False, None, f"File extension blocked: {filename}"

    # 查詢信任時段
    session = get_trust_session(trust_scope, account_id)
    if not session:
        return False, None, "No active trust session"

    # 過期檢查
    remaining = int(session.get('expires_at', 0)) - int(time.time())
    if remaining <= 0:
        return False, None, "Trust session expired"

    max_uploads = int(session.get('max_uploads', 0))
    if max_uploads <= 0:
        return False, session, "Trust session upload not enabled"

    # 上傳次數檢查
    upload_count = int(session.get('upload_count', 0))
    if upload_count >= max_uploads:
        return False, session, f"Upload quota exhausted ({upload_count}/{max_uploads})"

    # 單檔大小檢查
    if content_size > TRUST_UPLOAD_MAX_BYTES_PER_FILE:
        return False, session, f"File too large: {content_size} > {TRUST_UPLOAD_MAX_BYTES_PER_FILE}"

    # 累計 bytes 檢查
    upload_bytes_total = int(session.get('upload_bytes_total', 0))
    if upload_bytes_total + content_size > TRUST_UPLOAD_MAX_BYTES_TOTAL:
        return False, session, "Total upload bytes would exceed limit"

    return True, session, f"Trust upload approved ({upload_count + 1}/{max_uploads})"


def increment_trust_upload_count(trust_id: str, content_size: int) -> bool:
    """
    原子性增加信任時段的上傳計數和字節計數

    使用 DynamoDB conditional update 確保並發安全。

    Args:
        trust_id: 信任時段 ID
        content_size: 本次上傳的字節數

    Returns:
        是否成功（False = 條件不滿足，如 quota 已滿或過期）
    """
    now = int(time.time())
    try:
        _get_table().update_item(
            Key={'request_id': trust_id},
            UpdateExpression=(
                'SET upload_count = if_not_exists(upload_count, :zero) + :one, '
                'upload_bytes_total = if_not_exists(upload_bytes_total, :zero) + :size'
            ),
            ConditionExpression=(
                'upload_count < max_uploads '
                'AND expires_at > :now'
            ),
            ExpressionAttributeValues={
                ':zero': 0,
                ':one': 1,
                ':size': content_size,
                ':now': now,
            },
        )
        return True
    except _get_table().meta.client.exceptions.ConditionalCheckFailedException:
        print(f"Trust upload conditional update failed for {trust_id}")
        return False
    except Exception as e:
        print(f"Increment trust upload count error: {e}")
        return False
