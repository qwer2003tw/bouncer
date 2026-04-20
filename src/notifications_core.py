"""Core notification utilities and infrastructure.

Extracted from notifications.py (Sprint 92, #272).
Contains:
- NotificationResult class
- Throttling logic
- Message sending primitives
- Post-notification DynamoDB/EventBridge setup
"""

import time
from typing import NamedTuple, Optional

from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger
import telegram as _telegram

logger = Logger(service="bouncer")

# Notification throttling (sprint24-003)
# Track last notification time to prevent spam from consecutive auto-approved commands
_last_notification_time = {}
NOTIFICATION_THROTTLE_SECONDS = 60


class NotificationResult(NamedTuple):
    """Result of sending a Telegram approval notification.

    Attributes:
        ok:         True if the Telegram API accepted the message.
        message_id: The Telegram ``message_id`` of the sent message, or None
                    if the send failed or the API response was unexpected.

    Backward-compatibility note:
        Callers that do ``if result:`` or ``if not result:`` continue to work
        because ``NamedTuple`` instances are truthy when ``ok`` is True, but
        — unlike a plain bool — only if converted explicitly.  Callers that
        relied on bare ``bool(result)`` should use ``result.ok`` instead.
        The ``bool()`` of a NamedTuple is *always* True (non-empty tuple), so
        callers using ``if not notified:`` **must** be updated to ``if not notified.ok:``.
    """
    ok: bool
    message_id: Optional[int]


def _should_throttle_notification(notification_type: str) -> bool:
    """Check if we should throttle a notification based on recent activity.

    Args:
        notification_type: Type of notification (e.g., 'auto_approve', 'trust_approve')

    Returns:
        True if notification should be skipped (throttled), False if it should be sent
    """
    global _last_notification_time

    current_time = time.time()
    last_time = _last_notification_time.get(notification_type, 0)

    if current_time - last_time < NOTIFICATION_THROTTLE_SECONDS:
        logger.info("Throttling %s notification (last sent %.1fs ago)", notification_type, current_time - last_time, extra={"src_module": "notifications", "operation": "throttle_check", "notification_type": notification_type})
        return True

    _last_notification_time[notification_type] = current_time
    return False


def _escape_markdown(text):
    return _telegram.escape_markdown(text)


def _send_message(text, keyboard=None) -> dict:
    """Send a Telegram message and return the API response dict.

    Returns the raw API response (``{'ok': True, ...}`` on success, ``{}`` on
    any failure so callers can check ``result.get('ok')``).
    """
    return _telegram.send_telegram_message(text, keyboard)


def _send_message_silent(text, keyboard=None):
    _telegram.send_telegram_message_silent(text, keyboard)


# ============================================================================
# Post-notification Setup (sprint7-002)
# ============================================================================

def post_notification_setup(
    request_id: str,
    telegram_message_id: int,
    expires_at: int,
) -> None:
    """Store ``telegram_message_id`` in DynamoDB and schedule expiry cleanup.

    This function is called after a Telegram approval message has been sent
    successfully.  It performs two non-critical side-effects:

    1. Persists ``telegram_message_id`` on the DynamoDB item so the cleanup
       handler can later remove the inline keyboard.
    2. Creates an EventBridge Scheduler one-time schedule to invoke the
       ``cleanup_expired`` Lambda path at ``expires_at``.

    Both operations are best-effort: failures are logged but never propagated
    to the caller (the approval request has already been created).

    Args:
        request_id:         The Bouncer request ID (DynamoDB primary key).
        telegram_message_id: The ``message_id`` returned by the Telegram API.
        expires_at:         Unix timestamp when the request expires (DynamoDB TTL).
    """
    # 1. Store telegram_message_id in DynamoDB
    try:
        from db import table as _table

        _table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET telegram_message_id = :mid",
            ExpressionAttributeValues={":mid": telegram_message_id},
        )
        logger.info("Stored telegram_message_id=%s for request %s", telegram_message_id, request_id, extra={"src_module": "notifications", "operation": "post_notification_setup", "request_id": request_id, "message_id": telegram_message_id})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to store telegram_message_id for %s: %s", request_id, exc, extra={"src_module": "notifications", "operation": "post_notification_setup", "request_id": request_id, "error": str(exc)})

    # 2. Schedule EventBridge expiry trigger (embed telegram_message_id as fallback)
    try:
        from scheduler_service import get_scheduler_service

        svc = get_scheduler_service()
        svc.create_expiry_schedule(
            request_id=request_id,
            expires_at=expires_at,
            telegram_message_id=telegram_message_id,
        )
    except ClientError as exc:
        logger.exception("Failed to create expiry schedule for %s: %s", request_id, exc, extra={"src_module": "notifications", "operation": "post_notification_setup", "request_id": request_id, "error": str(exc)})


def _store_notification_snapshot(request_id: str, text: str, message_id: int) -> None:
    """Store Telegram notification text snapshot for UIUX analysis.

    Allows future analysis of: notification clarity, approve rate by text length,
    information density vs response time.

    Non-fatal: failures are silently swallowed.
    """
    from db import table as _table
    try:
        _table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET notification_text = :t, notification_length = :l, notification_message_id = :m',
            ExpressionAttributeValues={
                ':t': text[:2000],  # truncate for DDB item size
                ':l': len(text),
                ':m': message_id or 0,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to emit notification metric", exc_info=True)
