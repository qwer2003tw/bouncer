"""Silent notification rules management.

Allows users to silence auto-approved notifications for specific (source, service:action) combinations.
"""

import os
import time
import uuid
import boto3
from typing import Optional
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

TABLE_NAME = os.environ.get('SILENT_RULES_TABLE', 'bouncer-silent-rules-prod')


def _get_table():
    """Get DynamoDB table resource."""
    return boto3.resource('dynamodb').Table(TABLE_NAME)


def make_source_action_key(source: str, service: str, action: str) -> str:
    """Create composite key: source|service:action

    Args:
        source: Source identifier (e.g., 'clawdbot', 'admin')
        service: AWS service (e.g., 'ec2', 's3')
        action: AWS action (e.g., 'describe-instances', 'list-buckets')

    Returns:
        Composite key string
    """
    return f"{source}|{service}:{action}"


def is_silenced(source: str, service: str, action: str) -> Optional[dict]:
    """Check if a (source, service:action) combination is silenced.

    Args:
        source: Source identifier
        service: AWS service
        action: AWS action

    Returns:
        The rule dict if silenced and not expired, None otherwise
    """
    table = _get_table()
    key = make_source_action_key(source, service, action)

    try:
        resp = table.query(
            IndexName='source-action-index',
            KeyConditionExpression='source_action = :sa',
            ExpressionAttributeValues={':sa': key},
            Limit=1
        )
    except Exception as e:
        logger.warning("Failed to query silent rules", extra={
            "src_module": "silent_rules",
            "operation": "is_silenced",
            "error": str(e),
            "source": source,
            "service": service,
            "action": action
        }, exc_info=True)
        return None

    items = resp.get('Items', [])
    if not items:
        return None

    rule = items[0]

    # Check expiry
    expires = rule.get('expires_at')
    if expires and int(time.time()) > int(expires):
        return None

    # Update hit count (best-effort, don't fail if this fails)
    try:
        table.update_item(
            Key={'rule_id': rule['rule_id']},
            UpdateExpression='SET hit_count = hit_count + :inc, last_triggered_at = :now',
            ExpressionAttributeValues={':inc': 1, ':now': int(time.time())}
        )
    except Exception as e:
        logger.warning("Failed to update hit count for silent rule", extra={
            "src_module": "silent_rules",
            "operation": "update_hit_count",
            "rule_id": rule.get('rule_id'),
            "error": str(e)
        })

    return rule


def create_rule(
    source: str,
    service: str,
    action: str,
    created_by: str,
    expires_at: Optional[int] = None
) -> dict:
    """Create a new silent rule.

    Args:
        source: Source identifier
        service: AWS service
        action: AWS action
        created_by: User ID who created this rule
        expires_at: Optional Unix timestamp when rule expires

    Returns:
        The created rule dict
    """
    table = _get_table()
    rule_id = f"sr-{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    item = {
        'rule_id': rule_id,
        'source': source,
        'service': service,
        'action': action,
        'source_action': make_source_action_key(source, service, action),
        'created_at': now,
        'created_by': created_by,
        'expires_at': expires_at,
        'hit_count': 0,
        'last_triggered_at': None,
    }

    try:
        table.put_item(Item=item)
        logger.info("Created silent rule", extra={
            "src_module": "silent_rules",
            "operation": "create_rule",
            "rule_id": rule_id,
            "source": source,
            "service": service,
            "action": action,
            "created_by": created_by
        })
    except Exception as e:
        logger.error("Failed to create silent rule", extra={
            "src_module": "silent_rules",
            "operation": "create_rule",
            "error": str(e),
            "source": source,
            "service": service,
            "action": action
        }, exc_info=True)
        raise

    return item


def list_rules() -> list:
    """List all active (non-expired) rules.

    Returns:
        List of active rule dicts
    """
    table = _get_table()
    try:
        resp = table.scan()
        now = int(time.time())
        return [
            r for r in resp.get('Items', [])
            if not r.get('expires_at') or int(r['expires_at']) > now
        ]
    except Exception as e:
        logger.error("Failed to list silent rules", extra={
            "src_module": "silent_rules",
            "operation": "list_rules",
            "error": str(e)
        }, exc_info=True)
        return []


def revoke_rule(rule_id: str) -> bool:
    """Delete a rule by ID.

    Args:
        rule_id: Rule ID to delete

    Returns:
        True if successful
    """
    table = _get_table()
    try:
        table.delete_item(Key={'rule_id': rule_id})
        logger.info("Revoked silent rule", extra={
            "src_module": "silent_rules",
            "operation": "revoke_rule",
            "rule_id": rule_id
        })
        return True
    except Exception as e:
        logger.error("Failed to revoke silent rule", extra={
            "src_module": "silent_rules",
            "operation": "revoke_rule",
            "rule_id": rule_id,
            "error": str(e)
        }, exc_info=True)
        return False


def revoke_all() -> int:
    """Delete all rules.

    Returns:
        Count of rules deleted
    """
    table = _get_table()
    try:
        rules = table.scan(ProjectionExpression='rule_id').get('Items', [])
        for r in rules:
            table.delete_item(Key={'rule_id': r['rule_id']})
        logger.info("Revoked all silent rules", extra={
            "src_module": "silent_rules",
            "operation": "revoke_all",
            "count": len(rules)
        })
        return len(rules)
    except Exception as e:
        logger.error("Failed to revoke all silent rules", extra={
            "src_module": "silent_rules",
            "operation": "revoke_all",
            "error": str(e)
        }, exc_info=True)
        return 0
