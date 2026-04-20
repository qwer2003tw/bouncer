"""DynamoDB table references — single source of truth.

Import from here instead of app.py to avoid circular dependencies.
Lazy init: boto3 resources are NOT created at import time to avoid
moto mock isolation issues in tests and reduce cold-start OOM risk.
"""

import os
import logging
import boto3
from botocore.exceptions import ClientError
from constants import TABLE_NAME, ACCOUNTS_TABLE_NAME, DEFAULT_REGION

logger = logging.getLogger(__name__)


class _LazyTable:
    """Proxy for a DynamoDB Table that initialises boto3 on first access.

    This avoids creating real AWS connections at module import time, which
    breaks moto mocks when the mock context isn't active yet.
    """

    def __init__(self, table_name_env: str, default_table_name: str):
        self._table_name_env = table_name_env
        self._default_table_name = default_table_name
        self._table = None

    def _get(self):
        if self._table is None:
            dynamodb = boto3.resource('dynamodb', region_name=DEFAULT_REGION)
            table_name = os.environ.get(self._table_name_env, self._default_table_name)
            self._table = dynamodb.Table(table_name)
        return self._table

    def _reset(self):
        """Reset cached table — call this in test teardown after moto context exits."""
        self._table = None

    # Proxy all attribute access to the real table object
    def __getattr__(self, name):
        return getattr(self._get(), name)

    def __repr__(self):
        return f'<_LazyTable {self._table_name_env}>'


# Module-level table references (lazy — no boto3 call at import time)
table = _LazyTable('TABLE_NAME', TABLE_NAME)
accounts_table = _LazyTable('ACCOUNTS_TABLE_NAME', ACCOUNTS_TABLE_NAME)

# Deployer tables
deployer_projects_table = _LazyTable('PROJECTS_TABLE', 'bouncer-projects')
deployer_history_table = _LazyTable('HISTORY_TABLE', 'bouncer-deploy-history')
deployer_locks_table = _LazyTable('LOCKS_TABLE', 'bouncer-deploy-locks')

# Sequence analyzer history table
sequence_history_table = _LazyTable('COMMAND_HISTORY_TABLE', 'bouncer-command-history')


def get_table():
    """Return the main DynamoDB table (initialises on first call)."""
    return table._get()


def get_accounts_table():
    """Return the accounts DynamoDB table (initialises on first call)."""
    return accounts_table._get()


def reset_tables():
    """Reset all cached table references. Use in test teardown."""
    table._reset()
    accounts_table._reset()
    deployer_projects_table._reset()
    deployer_history_table._reset()
    deployer_locks_table._reset()
    sequence_history_table._reset()


# DDB operation helpers — reduce boilerplate for common patterns

def safe_put_item(table_ref, item: dict, **kwargs) -> bool:
    """Put item with standardized error handling.

    Args:
        table_ref: DynamoDB table object or _LazyTable reference
        item: Item dict to put
        **kwargs: Additional kwargs for put_item (e.g., ConditionExpression)

    Returns:
        True on success, False on error
    """
    try:
        table_ref.put_item(Item=item, **kwargs)
        return True
    except ClientError as e:
        logger.exception("DDB put_item failed: %s", e)
        return False


def safe_get_item(table_ref, key: dict) -> dict | None:
    """Get item with standardized error handling.

    Args:
        table_ref: DynamoDB table object or _LazyTable reference
        key: Key dict for get_item

    Returns:
        Item dict if found, None if not found or on error
    """
    try:
        response = table_ref.get_item(Key=key)
        return response.get('Item')
    except ClientError as e:
        logger.exception("DDB get_item failed: %s", e)
        return None


def safe_update_item(
    table_ref,
    key: dict,
    update_expr: str,
    expr_values: dict,
    expr_names: dict | None = None,
    **kwargs
) -> bool:
    """Update item with standardized error handling.

    Args:
        table_ref: DynamoDB table object or _LazyTable reference
        key: Key dict for update_item
        update_expr: UpdateExpression string
        expr_values: ExpressionAttributeValues dict
        expr_names: ExpressionAttributeNames dict (optional)
        **kwargs: Additional kwargs (e.g., ConditionExpression)

    Returns:
        True on success, False on error
    """
    try:
        params = {
            'Key': key,
            'UpdateExpression': update_expr,
            'ExpressionAttributeValues': expr_values,
        }
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names
        params.update(kwargs)
        table_ref.update_item(**params)
        return True
    except ClientError as e:
        logger.exception("DDB update_item failed: %s", e)
        return False
