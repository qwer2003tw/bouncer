"""
Bouncer - Trust Session 模組
處理信任時段的建立、查詢、撤銷和自動批准判斷
"""
import time
import hashlib
from typing import Optional, Dict

import boto3

try:
    from constants import (
        TABLE_NAME,
        TRUST_SESSION_ENABLED, TRUST_SESSION_DURATION, TRUST_SESSION_MAX_COMMANDS,
        TRUST_EXCLUDED_SERVICES, TRUST_EXCLUDED_ACTIONS, TRUST_EXCLUDED_FLAGS,
    )
except ImportError:
    from src.constants import (
        TABLE_NAME,
        TRUST_SESSION_ENABLED, TRUST_SESSION_DURATION, TRUST_SESSION_MAX_COMMANDS,
        TRUST_EXCLUDED_SERVICES, TRUST_EXCLUDED_ACTIONS, TRUST_EXCLUDED_FLAGS,
    )

__all__ = [
    'get_trust_session',
    'create_trust_session',
    'revoke_trust_session',
    'increment_trust_command_count',
    'is_trust_excluded',
    'should_trust_approve',
]

# DynamoDB - lazy init
_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource('dynamodb')
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def get_trust_session(source: str, account_id: str) -> Optional[Dict]:
    """
    查詢有效的信任時段

    Args:
        source: 請求來源
        account_id: AWS 帳號 ID

    Returns:
        信任時段記錄，或 None
    """
    if not TRUST_SESSION_ENABLED or not source:
        return None

    now = int(time.time())

    try:
        response = _get_table().scan(
            FilterExpression='#type = :type AND #src = :source AND account_id = :account AND expires_at > :now',
            ExpressionAttributeNames={
                '#type': 'type',
                '#src': 'source'
            },
            ExpressionAttributeValues={
                ':type': 'trust_session',
                ':source': source,
                ':account': account_id,
                ':now': now
            }
        )

        items = response.get('Items', [])
        if items:
            return items[0]
        return None

    except Exception as e:
        print(f"Trust session check error: {e}")
        return None


def create_trust_session(source: str, account_id: str, approved_by: str) -> str:
    """
    建立信任時段

    Args:
        source: 請求來源
        account_id: AWS 帳號 ID
        approved_by: 批准者 ID

    Returns:
        trust_id
    """
    source_hash = hashlib.md5(source.encode(), usedforsecurity=False).hexdigest()[:8]
    trust_id = f"trust-{source_hash}-{account_id}"

    now = int(time.time())
    expires_at = now + TRUST_SESSION_DURATION

    item = {
        'request_id': trust_id,
        'type': 'trust_session',
        'source': source,
        'account_id': account_id,
        'approved_by': approved_by,
        'created_at': now,
        'expires_at': expires_at,
        'command_count': 0,
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
    增加信任時段的命令計數

    Returns:
        新的計數值
    """
    try:
        response = _get_table().update_item(
            Key={'request_id': trust_id},
            UpdateExpression='SET command_count = if_not_exists(command_count, :zero) + :one',
            ExpressionAttributeValues={
                ':zero': 0,
                ':one': 1
            },
            ReturnValues='UPDATED_NEW'
        )
        return response.get('Attributes', {}).get('command_count', 0)
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


def should_trust_approve(command: str, source: str, account_id: str) -> tuple:
    """
    檢查是否應該透過信任時段自動批准

    Args:
        command: AWS CLI 命令
        source: 請求來源
        account_id: AWS 帳號 ID

    Returns:
        (should_approve: bool, trust_session: dict or None, reason: str)
    """
    if not TRUST_SESSION_ENABLED or not source:
        return False, None, "Trust session disabled or no source"

    # 檢查是否有有效的信任時段
    session = get_trust_session(source, account_id)
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
