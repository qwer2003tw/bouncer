"""
Bouncer - mcp_grant.py 測試
覆蓋 Grant Session MCP Tools 的核心功能
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock

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
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
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
def mcp_grant_module(mock_dynamodb):
    """載入 mcp_grant 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'mcp_grant', 'db', 'constants', 'utils', 'commands',
        'accounts', 'grant', 'notifications', 'telegram', 'paging'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

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

    import mcp_grant
    yield mcp_grant

    sys.path.pop(0)


# ============================================================================
# Tests for mcp_tool_request_grant
# ============================================================================

@patch('mcp_grant.send_grant_request_notification')
def test_request_grant_success(mock_notify, mcp_grant_module):
    """測試 mcp_tool_request_grant 成功建立 grant 請求"""
    req_id = 'test-mcp-001'
    arguments = {
        'commands': ['aws s3 ls', 'aws s3 cp s3://bucket/file .'],
        'reason': 'Deploy to production',
        'source': 'test-agent',
        'account': '111111111111',
        'ttl_minutes': 30,
        'allow_repeat': False
    }

    result = mcp_grant_module.mcp_tool_request_grant(req_id, arguments)

    # 驗證回應格式
    body = json.loads(result['body'])
    assert 'result' in body
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'pending_approval'
    assert 'grant_request_id' in data
    assert 'summary' in data
    assert 'expires_in' in data

    # 驗證 notification 被呼叫
    mock_notify.assert_called_once()


def test_request_grant_missing_commands(mcp_grant_module):
    """測試 mcp_tool_request_grant 缺少 commands 參數"""
    req_id = 'test-mcp-002'
    arguments = {
        'reason': 'Testing',
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_request_grant(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'commands' in body['error']['message']


def test_request_grant_missing_reason(mcp_grant_module):
    """測試 mcp_tool_request_grant 缺少 reason 參數"""
    req_id = 'test-mcp-003'
    arguments = {
        'commands': ['aws s3 ls'],
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_request_grant(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'reason' in body['error']['message']


def test_request_grant_missing_source(mcp_grant_module):
    """測試 mcp_tool_request_grant 缺少 source 參數"""
    req_id = 'test-mcp-004'
    arguments = {
        'commands': ['aws s3 ls'],
        'reason': 'Testing'
    }

    result = mcp_grant_module.mcp_tool_request_grant(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'source' in body['error']['message']


def test_request_grant_invalid_account(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_request_grant 無效帳號"""
    req_id = 'test-mcp-005'
    arguments = {
        'commands': ['aws s3 ls'],
        'reason': 'Testing',
        'source': 'test-agent',
        'account': '999999999999'  # 不存在的帳號
    }

    result = mcp_grant_module.mcp_tool_request_grant(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert '999999999999' in data['error']


# ============================================================================
# Tests for mcp_tool_grant_status
# ============================================================================

def test_grant_status_success(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_grant_status 成功查詢狀態"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-001'

    # 建立 grant session
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test-agent',
        'granted_commands': ['s3:list_objects_v2'],
        'ttl_minutes': 30,
        'expires_at': int(time.time()) + 1800,
        'created_at': int(time.time())
    })

    req_id = 'test-mcp-006'
    arguments = {
        'grant_id': grant_id,
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_status(req_id, arguments)

    # 驗證回應
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert 'status' in data
    assert data['status'] == 'active'


def test_grant_status_not_found(mcp_grant_module):
    """測試 mcp_tool_grant_status 查詢不存在的 grant"""
    req_id = 'test-mcp-007'
    arguments = {
        'grant_id': 'nonexistent-grant',
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_status(req_id, arguments)

    # 應回傳 not found
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert 'error' in data
    assert 'not found' in data['error'].lower()


def test_grant_status_source_mismatch(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_grant_status source 不匹配"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-002'

    # 建立 grant session
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'agent-A',
        'granted_commands': ['s3:list_objects_v2'],
        'created_at': int(time.time())
    })

    req_id = 'test-mcp-008'
    arguments = {
        'grant_id': grant_id,
        'source': 'agent-B'  # 不同的 source
    }

    result = mcp_grant_module.mcp_tool_grant_status(req_id, arguments)

    # 應回傳 not found（不洩漏 grant 存在）
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert 'error' in data


def test_grant_status_missing_grant_id(mcp_grant_module):
    """測試 mcp_tool_grant_status 缺少 grant_id"""
    req_id = 'test-mcp-009'
    arguments = {
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_status(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'grant_id' in body['error']['message']


# ============================================================================
# Tests for mcp_tool_revoke_grant
# ============================================================================

def test_revoke_grant_success(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_revoke_grant 成功撤銷"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-003'

    # 建立 grant session
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test-agent',
        'granted_commands': ['s3:list_objects_v2'],
        'created_at': int(time.time())
    })

    req_id = 'test-mcp-010'
    arguments = {
        'grant_id': grant_id
    }

    result = mcp_grant_module.mcp_tool_revoke_grant(req_id, arguments)

    # 驗證回應
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['success'] is True
    assert '撤銷' in data['message'] or 'revoked' in data['message'].lower()

    # 驗證 DDB 狀態
    item = table.get_item(Key={'request_id': grant_id})['Item']
    assert item['status'] == 'revoked'


def test_revoke_grant_not_found(mcp_grant_module):
    """測試 mcp_tool_revoke_grant 撤銷不存在的 grant"""
    req_id = 'test-mcp-011'
    arguments = {
        'grant_id': 'nonexistent-grant'
    }

    result = mcp_grant_module.mcp_tool_revoke_grant(req_id, arguments)

    # 驗證回應（revoke 是幂等操作，即使不存在也回傳 success）
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['success'] is True


def test_revoke_grant_missing_grant_id(mcp_grant_module):
    """測試 mcp_tool_revoke_grant 缺少 grant_id"""
    req_id = 'test-mcp-012'
    arguments = {}

    result = mcp_grant_module.mcp_tool_revoke_grant(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'grant_id' in body['error']['message']


# ============================================================================
# Tests for mcp_tool_grant_execute
# ============================================================================

@patch('mcp_grant.execute_boto3_native')
@patch('mcp_grant.store_paged_output')
@patch('mcp_grant.send_grant_execute_notification')
@patch('mcp_grant.log_decision')
def test_grant_execute_success(mock_log, mock_notify, mock_store, mock_exec, mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_grant_execute 成功執行"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-004'

    # 建立 active grant session
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test-agent',
        'account_id': '111111111111',
        'granted_commands': ['s3:list_objects_v2', 'ec2:describe_instances'],
        'allow_repeat': False,
        'used_commands': {},
        'total_executions': 0,
        'max_total_executions': 100,
        'expires_at': int(time.time()) + 1800,
        'created_at': int(time.time())
    })

    # Mock 執行結果
    mock_exec.return_value = '{"Buckets": []}'
    mock_store.return_value = MagicMock(paged=False, result='{"Buckets": []}')

    req_id = 'test-mcp-013'
    arguments = {
        'grant_id': grant_id,
        'aws': {
            'service': 's3',
            'operation': 'list_objects_v2',
            'params': {'Bucket': 'test-bucket'}
        },
        'source': 'test-agent',
        'reason': 'List files'
    }

    result = mcp_grant_module.mcp_tool_grant_execute(req_id, arguments)

    # 驗證回應
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'grant_executed'
    assert data['grant_id'] == grant_id

    # 驗證 execute_boto3_native 被呼叫
    mock_exec.assert_called_once()


def test_grant_execute_missing_grant_id(mcp_grant_module):
    """測試 mcp_tool_grant_execute 缺少 grant_id"""
    req_id = 'test-mcp-014'
    arguments = {
        'aws': {
            'service': 's3',
            'operation': 'list_objects_v2',
            'params': {}
        },
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_execute(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602


def test_grant_execute_grant_not_found(mcp_grant_module):
    """測試 mcp_tool_grant_execute grant 不存在"""
    req_id = 'test-mcp-015'
    arguments = {
        'grant_id': 'nonexistent-grant',
        'aws': {
            'service': 's3',
            'operation': 'list_objects_v2',
            'params': {}
        },
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_execute(req_id, arguments)

    # 驗證回應
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'grant_not_found'


def test_grant_execute_grant_expired(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_grant_execute grant 已過期"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-005'

    # 建立過期的 grant session
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test-agent',
        'account_id': '111111111111',
        'granted_commands': ['s3:list_objects_v2'],
        'expires_at': int(time.time()) - 100,  # 已過期
        'created_at': int(time.time()) - 2000
    })

    req_id = 'test-mcp-016'
    arguments = {
        'grant_id': grant_id,
        'aws': {
            'service': 's3',
            'operation': 'list_objects_v2',
            'params': {}
        },
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_execute(req_id, arguments)

    # 驗證回應
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'grant_expired'


def test_grant_execute_command_not_in_grant(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_grant_execute 命令不在 grant 白名單中"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-006'

    # 建立 grant session，只允許 s3:list_objects_v2
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test-agent',
        'account_id': '111111111111',
        'granted_commands': ['s3:list_objects_v2'],  # 只允許這個
        'allow_repeat': False,
        'used_commands': {},
        'expires_at': int(time.time()) + 1800,
        'created_at': int(time.time())
    })

    req_id = 'test-mcp-017'
    arguments = {
        'grant_id': grant_id,
        'aws': {
            'service': 'ec2',
            'operation': 'describe_instances',  # 不在白名單
            'params': {}
        },
        'source': 'test-agent'
    }

    result = mcp_grant_module.mcp_tool_grant_execute(req_id, arguments)

    # 驗證回應
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'command_not_in_grant'


def test_grant_execute_source_mismatch(mcp_grant_module, mock_dynamodb):
    """測試 mcp_tool_grant_execute source 不匹配"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    grant_id = 'grant-007'

    # 建立 grant session
    table.put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'agent-A',  # source: agent-A
        'account_id': '111111111111',
        'granted_commands': ['s3:list_objects_v2'],
        'expires_at': int(time.time()) + 1800,
        'created_at': int(time.time())
    })

    req_id = 'test-mcp-018'
    arguments = {
        'grant_id': grant_id,
        'aws': {
            'service': 's3',
            'operation': 'list_objects_v2',
            'params': {}
        },
        'source': 'agent-B'  # 不同的 source
    }

    result = mcp_grant_module.mcp_tool_grant_execute(req_id, arguments)

    # 驗證回應（不洩漏 grant 存在）
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'grant_not_found'
