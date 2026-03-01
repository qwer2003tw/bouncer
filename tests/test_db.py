"""
tests/test_db.py — 測試 db.py 中所有 _LazyTable 正確初始化 + reset_tables 清除所有
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws
import boto3

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ---------------------------------------------------------------------------
# Helper: create all required tables in moto
# ---------------------------------------------------------------------------

def _create_all_tables(dynamodb):
    dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    dynamodb.create_table(
        TableName='bouncer-accounts',
        KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    dynamodb.create_table(
        TableName='bouncer-projects',
        KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    dynamodb.create_table(
        TableName='bouncer-deploy-history',
        KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'deploy_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    dynamodb.create_table(
        TableName='bouncer-deploy-locks',
        KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    dynamodb.create_table(
        TableName='bouncer-command-history',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLazyTableInit:
    """_LazyTable 延遲初始化測試"""

    def test_table_lazy_no_boto3_at_import(self):
        """import db 不應立即建立 boto3 連線"""
        # Clean import
        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        import db
        # All tables should have _table = None (not yet initialized)
        assert db.table._table is None
        assert db.accounts_table._table is None
        assert db.deployer_projects_table._table is None
        assert db.deployer_history_table._table is None
        assert db.deployer_locks_table._table is None
        assert db.sequence_history_table._table is None

    @mock_aws
    def test_table_initializes_on_access(self):
        """_LazyTable 在第一次存取時初始化"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        _create_all_tables(dynamodb)

        import db
        # Reset all caches to force re-init
        db.reset_tables()

        # Access main table
        real_table = db.table._get()
        assert real_table is not None
        assert db.table._table is not None

    @mock_aws
    def test_all_lazy_tables_initialized(self):
        """所有 _LazyTable 都能正確初始化"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        _create_all_tables(dynamodb)

        import db
        db.reset_tables()

        tables = [
            db.table,
            db.accounts_table,
            db.deployer_projects_table,
            db.deployer_history_table,
            db.deployer_locks_table,
            db.sequence_history_table,
        ]

        for lazy_table in tables:
            real = lazy_table._get()
            assert real is not None, f"{lazy_table} failed to initialize"
            assert lazy_table._table is not None

    @mock_aws
    def test_env_var_override(self):
        """環境變數可以覆蓋預設 table 名稱"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        os.environ['PROJECTS_TABLE'] = 'my-custom-projects'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        dynamodb.create_table(
            TableName='my-custom-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )

        import db
        db.deployer_projects_table._reset()

        real = db.deployer_projects_table._get()
        assert real.name == 'my-custom-projects'

        # Cleanup
        del os.environ['PROJECTS_TABLE']


class TestResetTables:
    """reset_tables() 清除所有快取測試"""

    @mock_aws
    def test_reset_tables_clears_all(self):
        """reset_tables() 應該清除所有 _LazyTable 快取"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        _create_all_tables(dynamodb)

        import db

        # Initialize all tables
        db.table._get()
        db.accounts_table._get()
        db.deployer_projects_table._get()
        db.deployer_history_table._get()
        db.deployer_locks_table._get()
        db.sequence_history_table._get()

        # All should be initialized
        assert db.table._table is not None
        assert db.accounts_table._table is not None
        assert db.deployer_projects_table._table is not None
        assert db.deployer_history_table._table is not None
        assert db.deployer_locks_table._table is not None
        assert db.sequence_history_table._table is not None

        # reset_tables() clears them all
        db.reset_tables()

        assert db.table._table is None
        assert db.accounts_table._table is None
        assert db.deployer_projects_table._table is None
        assert db.deployer_history_table._table is None
        assert db.deployer_locks_table._table is None
        assert db.sequence_history_table._table is None

    @mock_aws
    def test_reset_tables_allows_reinit(self):
        """reset 後應該可以再次初始化"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        _create_all_tables(dynamodb)

        import db

        t1 = db.table._get()
        db.reset_tables()
        t2 = db.table._get()

        # Both should be non-None (even if they're different objects)
        assert t1 is not None
        assert t2 is not None


class TestGetTableHelpers:
    """get_table() / get_accounts_table() 便利函式測試"""

    @mock_aws
    def test_get_table_returns_main_table(self):
        """get_table() 應返回 main approval requests table"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        _create_all_tables(dynamodb)

        import db
        db.reset_tables()

        t = db.get_table()
        assert t is not None

    @mock_aws
    def test_get_accounts_table_returns_accounts_table(self):
        """get_accounts_table() 應返回 accounts table"""
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'

        for mod in list(sys.modules.keys()):
            if mod in ('db', 'constants'):
                del sys.modules[mod]

        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        _create_all_tables(dynamodb)

        import db
        db.reset_tables()

        t = db.get_accounts_table()
        assert t is not None
