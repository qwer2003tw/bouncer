"""OTP (One-Time Password) module for high-risk command second-factor verification."""

import hmac
import random
import string
import time
from typing import Optional

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
import db as _db

logger = Logger(service="bouncer")

OTP_TTL = 300  # 5 minutes
OTP_MAX_ATTEMPTS = 3
OTP_LENGTH = 6

# Use _db.table directly - no wrapper needed (unified in db.py)


def generate_otp() -> str:
    """Generate a cryptographically random 6-digit OTP."""
    return ''.join(random.SystemRandom().choices(string.digits, k=OTP_LENGTH))


def create_otp_record(request_id: str, user_id: str, otp_code: str, message_id: int = 0) -> None:
    """Store OTP record in DynamoDB with TTL.

    Args:
        request_id: Original approval request ID
        user_id: Telegram user ID
        otp_code: Generated OTP code
        message_id: Telegram message ID of the approval request (for updating after verification)
    """
    table = _db.table
    now = int(time.time())
    table.put_item(Item={
        'request_id': f'otp#{request_id}',
        'otp_code': otp_code,
        'user_id': user_id,
        'original_request_id': request_id,
        'message_id': message_id,
        'attempts': 0,
        'created_at': now,
        'ttl': now + OTP_TTL,
        'type': 'otp_pending',
    })
    logger.info("OTP record created", extra={"src_module": "otp", "operation": "create_otp_record", "request_id": request_id, "user_id": user_id})


def get_pending_otp(user_id: str) -> Optional[dict]:
    """Find the most recent pending OTP for a user using GSI Query.

    Queries user-id-created-index GSI for otp# records belonging to user_id that haven't expired.
    Returns None if no pending OTP found.
    """
    table = _db.table
    now = int(time.time())
    all_items = []
    query_kwargs = {
        'IndexName': 'user-id-created-index',
        'KeyConditionExpression': 'user_id = :uid',
        'FilterExpression': 'begins_with(request_id, :prefix) AND #ttl > :now AND #type = :t',
        'ExpressionAttributeValues': {
            ':uid': user_id,
            ':prefix': 'otp#',
            ':now': now,
            ':t': 'otp_pending',
        },
        'ExpressionAttributeNames': {'#ttl': 'ttl', '#type': 'type'},
        'ScanIndexForward': False,  # newest first
    }

    try:
        # Query by user_id using GSI with pagination
        while True:
            result = table.query(**query_kwargs)
            all_items.extend(result.get('Items', []))
            last_key = result.get('LastEvaluatedKey')
            if not last_key:
                break
            query_kwargs['ExclusiveStartKey'] = last_key

        if not all_items:
            logger.info("No pending OTP found for user", extra={"src_module": "otp", "operation": "get_pending_otp", "user_id": user_id, "found": False})
            return None
        # Return most recently created (already sorted by ScanIndexForward=False)
        otp = all_items[0]
        logger.info("Found pending OTP", extra={"src_module": "otp", "operation": "get_pending_otp", "user_id": user_id, "found": True, "request_id": otp.get('original_request_id')})
        return otp
    except ClientError as e:
        logger.error("Failed to query OTP records: %s", e, extra={"src_module": "otp", "operation": "get_pending_otp", "user_id": user_id, "error": str(e)})
        return None


def validate_otp(request_id: str, provided_code: str) -> tuple[bool, str]:
    """Validate OTP code. Returns (success, message).

    On success: marks record as used.
    On failure: increments attempts. If max attempts reached, marks as failed.
    """
    table = _db.table
    otp_key = f'otp#{request_id}'
    now = int(time.time())

    try:
        item = table.get_item(Key={'request_id': otp_key}).get('Item')
    except ClientError as e:
        logger.error("Failed to get OTP record: %s", e)
        return False, "系統錯誤，請重試"

    if not item:
        return False, "OTP 不存在或已過期"

    if item.get('type') != 'otp_pending':
        if item.get('type') == 'otp_failed':
            return False, f"OTP 嘗試次數超過上限（{OTP_MAX_ATTEMPTS}次），請重新審批"
        return False, "OTP 已使用或已失效"

    if int(item.get('ttl', 0)) < now:
        return False, "OTP 已過期，請重新審批"

    attempts = int(item.get('attempts', 0))
    if attempts >= OTP_MAX_ATTEMPTS:
        return False, f"OTP 嘗試次數超過上限（{OTP_MAX_ATTEMPTS}次），請重新審批"

    if not hmac.compare_digest(str(item.get('otp_code', '')), str(provided_code)):
        # Increment attempts
        table.update_item(
            Key={'request_id': otp_key},
            UpdateExpression='SET attempts = :a',
            ExpressionAttributeValues={':a': attempts + 1},
        )
        remaining = OTP_MAX_ATTEMPTS - attempts - 1
        if remaining == 0:
            table.update_item(
                Key={'request_id': otp_key},
                UpdateExpression='SET #type = :t',
                ExpressionAttributeNames={'#type': 'type'},
                ExpressionAttributeValues={':t': 'otp_failed'},
            )
            return False, "OTP 錯誤，已超過上限。請重新審批"
        return False, f"OTP 錯誤，還剩 {remaining} 次機會"

    # Success: mark as used
    table.update_item(
        Key={'request_id': otp_key},
        UpdateExpression='SET #type = :t',
        ExpressionAttributeNames={'#type': 'type'},
        ExpressionAttributeValues={':t': 'otp_used'},
    )
    logger.info("OTP validated successfully", extra={"src_module": "otp", "operation": "validate_otp", "request_id": request_id})
    return True, "OTP 驗證成功"
