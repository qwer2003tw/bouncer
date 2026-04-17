"""
Bouncer - mcp_admin.py 測試
覆蓋帳號管理功能
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch

from moto import mock_aws
import boto3


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dynamodb():
    """建立 mock DynamoDB 表"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

        # Main table
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'type', 'AttributeType': 'S'},
                {'AttributeName': 'expires_at', 'AttributeType': 'N'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'type-expires-at-index',
                    'KeySchema': [
                        {'AttributeName': 'type', 'KeyType': 'HASH'},
                        {'AttributeName': 'expires_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                },
                {
                    'IndexName': 'status-created-index',
                    'KeySchema': [
                        {'AttributeName': 'status', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()

        # Accounts table
        accounts_table = dynamodb.create_table(
            TableName='bouncer-accounts',
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        accounts_table.wait_until_exists()

        yield dynamodb


@pytest.fixture
def mcp_admin_module(mock_dynamodb):
    """載入 mcp_admin 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'mcp_admin', 'db', 'constants', 'utils', 'accounts',
        'trust', 'notifications', 'paging'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]


    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.accounts_table = mock_dynamodb.Table('bouncer-accounts')
    db.dynamodb = mock_dynamodb

    # 初始化預設帳號
    db.accounts_table.put_item(Item={
        'account_id': '111111111111',
        'name': 'Default',
        'role_arn': None,
        'is_default': True,
        'enabled': True,
        'created_at': int(time.time())
    })

    import mcp_admin
    yield mcp_admin

    sys.path.pop(0)


# ============================================================================
# Tests for mcp_tool_status
# ============================================================================

def test_status_missing_request_id(mcp_admin_module):
    """測試 mcp_tool_status 缺少 request_id 參數"""
    req_id = 'test-status-001'
    arguments = {}

    result = mcp_admin_module.mcp_tool_status(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'request_id' in body['error']['message']


def test_status_not_found(mcp_admin_module):
    """測試 mcp_tool_status 查詢不存在的 request"""
    req_id = 'test-status-002'
    arguments = {
        'request_id': 'nonexistent-req'
    }

    result = mcp_admin_module.mcp_tool_status(req_id, arguments)

    # 應回傳 not found 錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert 'error' in data
    assert 'not found' in data['error'].lower()


def test_status_success(mcp_admin_module, mock_dynamodb):
    """測試 mcp_tool_status 成功查詢"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'req-status-001'

    # 建立測試請求
    table.put_item(Item={
        'request_id': request_id,
        'status': 'approved',
        'action': 'execute',
        'source': 'test-agent',
        'created_at': int(time.time())
    })

    req_id = 'test-status-003'
    arguments = {
        'request_id': request_id
    }

    result = mcp_admin_module.mcp_tool_status(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'approved'
    assert data['request_id'] == request_id


# ============================================================================
# Tests for mcp_tool_trust_status
# ============================================================================

def test_trust_status_no_active_sessions(mcp_admin_module):
    """測試 mcp_tool_trust_status 沒有活躍的信任時段"""
    req_id = 'test-trust-001'
    arguments = {}

    result = mcp_admin_module.mcp_tool_trust_status(req_id, arguments)

    # 應回傳成功，但 sessions 為空
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['active_sessions'] == 0
    assert len(data['sessions']) == 0


def test_trust_status_with_active_sessions(mcp_admin_module, mock_dynamodb):
    """測試 mcp_tool_trust_status 有活躍的信任時段"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())
    trust_id = 'trust-001'

    # 建立信任時段
    table.put_item(Item={
        'request_id': trust_id,
        'type': 'trust_session',
        'status': 'active',
        'source': 'test-agent',
        'account_id': '111111111111',
        'expires_at': now + 1800,  # 30 分鐘後過期
        'command_count': 5,
        'approved_by': 'admin',
        'created_at': now
    })

    req_id = 'test-trust-002'
    arguments = {}

    result = mcp_admin_module.mcp_tool_trust_status(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['active_sessions'] == 1
    assert len(data['sessions']) == 1
    assert data['sessions'][0]['trust_id'] == trust_id
    assert data['sessions'][0]['source'] == 'test-agent'


# ============================================================================
# Tests for mcp_tool_trust_revoke
# ============================================================================

def test_trust_revoke_missing_trust_id(mcp_admin_module):
    """測試 mcp_tool_trust_revoke 缺少 trust_id 參數"""
    req_id = 'test-revoke-001'
    arguments = {}

    result = mcp_admin_module.mcp_tool_trust_revoke(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'trust_id' in body['error']['message']


@patch('mcp_admin.revoke_trust_session', return_value=True)
def test_trust_revoke_success(mock_revoke, mcp_admin_module):
    """測試 mcp_tool_trust_revoke 成功撤銷"""
    req_id = 'test-revoke-002'
    arguments = {
        'trust_id': 'trust-001'
    }

    result = mcp_admin_module.mcp_tool_trust_revoke(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['success'] is True
    assert '撤銷' in data['message']

    # 驗證 revoke_trust_session 被呼叫
    mock_revoke.assert_called_once_with('trust-001')


# ============================================================================
# Tests for mcp_tool_add_account
# ============================================================================

def test_add_account_invalid_account_id(mcp_admin_module):
    """測試 mcp_tool_add_account 無效的 account_id"""
    req_id = 'test-add-001'
    arguments = {
        'account_id': 'invalid',  # 不是 12 位數字
        'name': 'Test Account',
        'role_arn': 'arn:aws:iam::222222222222:role/TestRole',
        'source': 'test-agent'
    }

    result = mcp_admin_module.mcp_tool_add_account(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert '帳號' in data['error'] or 'account_id' in data['error'] or '數字' in data['error']


@patch('mcp_admin.send_account_approval_request')
def test_add_account_pending_approval(mock_send, mcp_admin_module, mock_dynamodb):
    """測試 mcp_tool_add_account 建立審批請求"""
    req_id = 'test-add-002'
    arguments = {
        'account_id': '222222222222',
        'name': 'Test Account',
        'role_arn': 'arn:aws:iam::222222222222:role/TestRole',
        'source': 'test-agent'
    }

    result = mcp_admin_module.mcp_tool_add_account(req_id, arguments)

    # 應回傳 pending_approval
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'pending_approval'
    assert 'request_id' in data

    # 驗證 Telegram 通知被呼叫
    mock_send.assert_called_once()


# ============================================================================
# Tests for mcp_tool_list_accounts
# ============================================================================

def test_list_accounts_success(mcp_admin_module, mock_dynamodb):
    """測試 mcp_tool_list_accounts 成功列出帳號"""
    req_id = 'test-list-001'
    arguments = {}

    result = mcp_admin_module.mcp_tool_list_accounts(req_id, arguments)

    # 應回傳成功，至少有預設帳號
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert 'accounts' in data
    assert len(data['accounts']) >= 1
    assert data['default_account'] == '111111111111'


# ============================================================================
# Tests for mcp_tool_list_pending
# ============================================================================

def test_list_pending_empty(mcp_admin_module):
    """測試 mcp_tool_list_pending 空結果"""
    req_id = 'test-pending-001'
    arguments = {}

    result = mcp_admin_module.mcp_tool_list_pending(req_id, arguments)

    # 應回傳成功，但沒有待審批請求
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['pending_count'] == 0
    assert len(data['requests']) == 0


def test_list_pending_with_data(mcp_admin_module, mock_dynamodb):
    """測試 mcp_tool_list_pending 有待審批請求"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立待審批請求
    table.put_item(Item={
        'request_id': 'pending-001',
        'status': 'pending',
        'action': 'execute',
        'command': 'aws s3 ls',
        'source': 'test-agent',
        'account_id': '111111111111',
        'created_at': now - 300
    })

    req_id = 'test-pending-002'
    arguments = {}

    result = mcp_admin_module.mcp_tool_list_pending(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['pending_count'] == 1
    assert len(data['requests']) == 1
    assert data['requests'][0]['request_id'] == 'pending-001'
