"""
Bouncer v2.0.0 測試
包含 MCP JSON-RPC 測試 + 原有 REST API 測試
"""

import json
import sys
import os
import time
import pytest

# Ensure src/ is in path for all workers (required for pytest-xdist)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))
import importlib
from unittest.mock import patch, MagicMock
from decimal import Decimal

# Moto for AWS mocking
from moto import mock_aws
import boto3


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="function")
def mock_dynamodb():
    """建立 mock DynamoDB 表（含 GSI）- function scope，每個測試獨立隔離"""
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
                {'AttributeName': 'type', 'AttributeType': 'S'},
                {'AttributeName': 'expires_at', 'AttributeType': 'N'},
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
                },
                {
                    'IndexName': 'type-expires-at-index',
                    'KeySchema': [
                        {'AttributeName': 'type', 'KeyType': 'HASH'},
                        {'AttributeName': 'expires_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                },
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


@pytest.fixture(scope="function")
def app_module(mock_dynamodb):
    """載入 app 模組並注入 mock - function scope，每個測試獨立隔離"""
    # 設定環境變數
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['MCP_MAX_WAIT'] = '5'  # 測試用短時間

    # Workaround for boto3 + moto + Python 3.12 deepcopy RecursionError
    import copy
    _original_deepcopy = copy.deepcopy
    def _safe_deepcopy(x, memo=None, _nil=[]):
        try:
            return _original_deepcopy(x, memo, _nil)
        except RecursionError:
            # Return shallow copy as fallback (test environment only)
            import warnings
            warnings.warn("deepcopy RecursionError, using shallow copy", RuntimeWarning)
            try:
                return copy.copy(x)
            except:
                return x
    copy.deepcopy = _safe_deepcopy

    # 重新載入模組（包括新模組）
    for mod in ['app', 'telegram', 'paging', 'trust', 'commands', 'notifications', 'db',
                'callbacks', 'mcp_tools', 'mcp_execute', 'mcp_upload', 'mcp_admin',
                'accounts', 'rate_limit', 'smart_approval',
                'constants', 'utils', 'risk_scorer', 'template_scanner',
                'scheduler_service',
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

    # Mock send_message_with_entities (added in entities Phase 2, Sprint 13)
    # so that tests predating this change still pass.
    # Save + restore to avoid polluting test_entities_phase1.py.
    import telegram as _tg
    from unittest.mock import MagicMock
    _orig_smwe = getattr(_tg, 'send_message_with_entities', None)
    if not hasattr(_tg, 'send_message_with_entities') or not isinstance(_tg.send_message_with_entities, MagicMock):
        _tg.send_message_with_entities = MagicMock(
            return_value={'ok': True, 'result': {'message_id': 99999}}
        )

    yield app

    # Restore so subsequent test modules can patch _telegram_request normally.
    if _orig_smwe is not None:
        _tg.send_message_with_entities = _orig_smwe
    elif hasattr(_tg, 'send_message_with_entities'):
        del _tg.send_message_with_entities

    # Restore original deepcopy
    copy.deepcopy = _original_deepcopy


_ALL_TABLE_KEYS = {
    'clawdbot-approval-requests': ['request_id'],
    'bouncer-projects': ['project_id'],
    'bouncer-deploy-history': ['deploy_id'],
    'bouncer-deploy-locks': ['project_id'],
    'bouncer-accounts': ['account_id'],
}


@pytest.fixture(autouse=True)
def _cleanup_tables(request):
    """每個測試後清除所有表中的資料，避免測試間資料洩漏

    在每個測試執行前也重新注入 db module 的 table references，
    防止跨 test file 的 sys.modules 清除造成 db 重新 import 時指向真實 AWS。
    只在有 mock_dynamodb fixture 的測試中才執行（其他測試自己管理 fixture）。
    """
    # 只在有 mock_dynamodb fixture 的測試中才執行
    if 'mock_dynamodb' not in request.fixturenames:
        yield
        return

    mock_dynamodb = request.getfixturevalue('mock_dynamodb')
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
    try:
        import mcp_upload as _mcp_upload
        # mcp_upload uses `from db import table` — re-inject after db reset
        _mcp_upload.table = mock_dynamodb.Table('clawdbot-approval-requests')
    except Exception:
        pass
    try:
        import mcp_execute as _mcp_execute
        if hasattr(_mcp_execute, 'table'):
            _mcp_execute.table = mock_dynamodb.Table('clawdbot-approval-requests')
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



# ============================================================================
# Mock Pollution Prevention (Sprint 20 #92)
# ============================================================================

@pytest.fixture(autouse=True)
def reset_paging_module_bindings():
    """Reset paging module function bindings after each test.

    paging.py uses `from telegram import send_telegram_message_silent` which
    creates a local binding. When tests patch `telegram.send_telegram_message_silent`
    at module/class level, the binding may become stale. This fixture reloads
    the paging module after each test to restore clean bindings.
    """
    yield
    try:
        import paging
        importlib.reload(paging)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_telegram_commands_module():
    """Reset telegram_commands module after each test to prevent cross-test pollution.

    Some tests delete and re-import telegram_commands, which can leave the module
    in an inconsistent state. This fixture ensures the module is properly reloaded
    after each test to restore clean bindings and prevent mock pollution.
    """
    yield
    try:
        # Stop all active patches to prevent stale mocks
        from unittest.mock import _patch
        for p in list(_patch._active_patches):
            try:
                p.stop()
            except Exception:
                pass

        # Reload telegram_commands, notifications, and related modules to ensure clean state
        # test_sprint39_ux.py reloads notifications, which can affect telegram_commands
        modules_to_reload = ['telegram_commands', 'notifications', 'telegram', 'constants']
        for mod_name in modules_to_reload:
            if mod_name in sys.modules:
                try:
                    mod = sys.modules[mod_name]
                    importlib.reload(mod)
                except Exception:
                    # If reload fails, delete the module
                    try:
                        del sys.modules[mod_name]
                    except Exception:
                        pass
    except Exception:
        pass
