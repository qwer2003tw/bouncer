"""DynamoDB table references — single source of truth.

Import from here instead of app.py to avoid circular dependencies.
Lazy init: boto3 resources are NOT created at import time to avoid
moto mock isolation issues in tests and reduce cold-start OOM risk.
"""

import os
import boto3
from constants import TABLE_NAME, ACCOUNTS_TABLE_NAME


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
            region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
            dynamodb = boto3.resource('dynamodb', region_name=region)
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
