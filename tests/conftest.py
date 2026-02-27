"""
Bouncer v2.0.0 測試
包含 MCP JSON-RPC 測試 + 原有 REST API 測試
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

# Moto for AWS mocking
from moto import mock_aws
import boto3


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="module")
def mock_dynamodb():
    """建立 mock DynamoDB 表（含 GSI）- module scope，只建立一次"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        # Main approval-requests table
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
                {'AttributeName': 'source', 'AttributeType': 'S'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'status-created-index',
                    'KeySchema': [
                        {'AttributeName': 'status', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                },
                {
                    'IndexName': 'source-created-index',
                    'KeySchema': [
                        {'AttributeName': 'source', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()

        # Deployer tables (shared by all deployer test classes)
        dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        dynamodb.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                {'AttributeName': 'project_id', 'AttributeType': 'S'},
                {'AttributeName': 'started_at', 'AttributeType': 'N'}
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'project-time-index',
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            }],
            BillingMode='PAY_PER_REQUEST'
        )
        dynamodb.create_table(
            TableName='bouncer-deploy-locks',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )

        # Accounts table
        dynamodb.create_table(
            TableName='bouncer-accounts',
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )

        # S3 buckets needed for upload staging (P1-2 fix: content stored in S3, not DDB)
        s3 = boto3.client('s3', region_name='us-east-1')
        for bucket_name in [
            'bouncer-uploads-111111111111',
            'bouncer-uploads-222222222222',
            'legacy-bucket',
        ]:
            s3.create_bucket(Bucket=bucket_name)

        yield dynamodb


@pytest.fixture(scope="module")
def app_module(mock_dynamodb):
    """載入 app 模組並注入 mock - module scope，只載入一次"""
    # 設定環境變數
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['MCP_MAX_WAIT'] = '5'  # 測試用短時間
    
    # 重新載入模組（包括新模組）
    for mod in ['app', 'telegram', 'paging', 'trust', 'commands', 'notifications', 'db',
                'callbacks', 'mcp_tools', 'mcp_execute', 'mcp_upload', 'mcp_admin',
                'accounts', 'rate_limit', 'smart_approval',
                'constants', 'utils', 'risk_scorer', 'template_scanner',
                'src.app', 'src.telegram', 'src.paging', 'src.trust', 'src.commands']:
        if mod in sys.modules:
            del sys.modules[mod]
    
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
    import app
    
    # 注入 mock table
    import db
    import accounts
    app.table = mock_dynamodb.Table('clawdbot-approval-requests')
    app.dynamodb = mock_dynamodb
    db.table = app.table
    db.accounts_table = app.accounts_table if hasattr(app, 'accounts_table') else mock_dynamodb.Table('bouncer-accounts')
    
    # Skip real Telegram API calls in tests
    accounts._bot_commands_initialized = True
    
    yield app


_ALL_TABLE_KEYS = {
    'clawdbot-approval-requests': ['request_id'],
    'bouncer-projects': ['project_id'],
    'bouncer-deploy-history': ['deploy_id'],
    'bouncer-deploy-locks': ['project_id'],
    'bouncer-accounts': ['account_id'],
}


@pytest.fixture(autouse=True)
def _cleanup_tables(mock_dynamodb):
    """每個測試後清除所有表中的資料，避免測試間資料洩漏

    在每個測試執行前也重新注入 db module 的 table references，
    防止跨 test file 的 sys.modules 清除造成 db 重新 import 時指向真實 AWS。
    """
    # Re-inject db/accounts references before each test (guard against cross-file sys.modules pollution)
    # Other test files (test_ddb_400kb_fix.py, test_grant.py) delete sys.modules['db'],
    # which causes db/accounts to be re-imported with real boto3 when interleaved via pytest-randomly.
    try:
        import db as _db
        _db.table = mock_dynamodb.Table('clawdbot-approval-requests')
        _db.accounts_table = mock_dynamodb.Table('bouncer-accounts')
    except Exception:
        pass
    try:
        import accounts as _accounts
        # Reset the lazy-init cache so _get_accounts_table() returns the moto-backed table
        _accounts._accounts_table = mock_dynamodb.Table('bouncer-accounts')
    except Exception:
        pass
    yield
    for table_name, key_attrs in _ALL_TABLE_KEYS.items():
        try:
            table = mock_dynamodb.Table(table_name)
            scan = table.scan(
                ProjectionExpression=','.join(f'#k{i}' for i in range(len(key_attrs))),
                ExpressionAttributeNames={f'#k{i}': k for i, k in enumerate(key_attrs)}
            )
            items = scan.get('Items', [])
            if items:
                with table.batch_writer() as batch:
                    for item in items:
                        batch.delete_item(Key=item)
        except Exception:
            pass

