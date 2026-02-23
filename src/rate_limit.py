"""
Bouncer - Rate Limiting 模組
處理請求頻率限制和 pending 請求上限
"""
import time

import boto3


from constants import (
    TABLE_NAME,
    RATE_LIMIT_ENABLED, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX_REQUESTS,
    MAX_PENDING_PER_SOURCE,
)

__all__ = [
    'RateLimitExceeded',
    'PendingLimitExceeded',
    'check_rate_limit',
]

# DynamoDB - lazy init
_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource('dynamodb')
        _table = dynamodb.Table(TABLE_NAME)
    return _table


class RateLimitExceeded(Exception):
    """Rate limit 超出例外"""
    pass


class PendingLimitExceeded(Exception):
    """Pending limit 超出例外"""
    pass


def check_rate_limit(source: str) -> None:
    """
    檢查 source 的請求頻率

    Args:
        source: 請求來源識別

    Raises:
        RateLimitExceeded: 如果超出頻率限制
        PendingLimitExceeded: 如果 pending 請求過多
    """
    if not RATE_LIMIT_ENABLED:
        return

    if not source:
        source = "__anonymous__"

    now = int(time.time())
    window_start = now - RATE_LIMIT_WINDOW

    try:
        # 查詢此 source 在時間視窗內的審批請求數
        response = _get_table().query(
            IndexName='source-created-index',
            KeyConditionExpression='#src = :source AND created_at >= :window_start',
            FilterExpression='#st IN (:pending, :approved, :denied)',
            ExpressionAttributeNames={
                '#src': 'source',
                '#st': 'status'
            },
            ExpressionAttributeValues={
                ':source': source,
                ':window_start': window_start,
                ':pending': 'pending_approval',
                ':approved': 'approved',
                ':denied': 'denied'
            },
            Select='COUNT'
        )

        recent_count = response.get('Count', 0)

        if recent_count >= RATE_LIMIT_MAX_REQUESTS:
            raise RateLimitExceeded(
                f"Rate limit exceeded: {recent_count}/{RATE_LIMIT_MAX_REQUESTS} "
                f"requests in last {RATE_LIMIT_WINDOW}s"
            )

        # 查詢 pending 請求數
        pending_response = _get_table().query(
            IndexName='source-created-index',
            KeyConditionExpression='#src = :source',
            FilterExpression='#st = :pending',
            ExpressionAttributeNames={
                '#src': 'source',
                '#st': 'status'
            },
            ExpressionAttributeValues={
                ':source': source,
                ':pending': 'pending_approval'
            },
            Select='COUNT'
        )

        pending_count = pending_response.get('Count', 0)

        if pending_count >= MAX_PENDING_PER_SOURCE:
            raise PendingLimitExceeded(
                f"Pending limit exceeded: {pending_count}/{MAX_PENDING_PER_SOURCE} "
                f"pending requests"
            )

    except (RateLimitExceeded, PendingLimitExceeded):
        raise
    except Exception as e:
        # GSI 不存在或其他錯誤，記錄但不阻擋（fail-open）
        print(f"Rate limit check error (allowing): {e}")
