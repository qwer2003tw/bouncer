"""
Bouncer - mcp_history.py 測試
覆蓋部署歷史查詢功能
"""

import json
import sys
import os
import time
import pytest

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
                    'Projection': {'ProjectionType': 'INCLUDE', 'NonKeyAttributes': ['status']}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()

        yield dynamodb


@pytest.fixture
def mcp_history_module(mock_dynamodb):
    """載入 mcp_history 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'

    # 清除模組
    modules_to_clear = [
        'mcp_history', 'db', 'constants', 'utils'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]


    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import mcp_history
    yield mcp_history

    sys.path.pop(0)


# ============================================================================
# Tests for mcp_tool_history
# ============================================================================

def test_history_empty_results(mcp_history_module):
    """測試 mcp_tool_history 空結果"""
    req_id = 'test-history-001'
    arguments = {
        'limit': 10,
        'since_hours': 24
    }

    result = mcp_history_module.mcp_tool_history(req_id, arguments)

    # 應回傳成功，但 items 為空
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert 'items' in data
    assert len(data['items']) == 0
    assert data['filters_applied']['limit'] == 10
    assert data['filters_applied']['since_hours'] == 24


def test_history_with_status_filter(mcp_history_module, mock_dynamodb):
    """測試 mcp_tool_history 使用 status 過濾"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立測試資料
    table.put_item(Item={
        'request_id': 'req-001',
        'status': 'approved',
        'action': 'execute',
        'source': 'test-agent',
        'created_at': now - 3600,
        'approved_at': now - 3500
    })
    table.put_item(Item={
        'request_id': 'req-002',
        'status': 'denied',
        'action': 'execute',
        'source': 'test-agent',
        'created_at': now - 7200
    })

    req_id = 'test-history-002'
    arguments = {
        'status': 'approved',
        'limit': 10,
        'since_hours': 24
    }

    result = mcp_history_module.mcp_tool_history(req_id, arguments)

    # 應只回傳 approved 的記錄
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert len(data['items']) == 1
    assert data['items'][0]['status'] == 'approved'
    assert data['items'][0]['request_id'] == 'req-001'


def test_history_with_source_filter(mcp_history_module, mock_dynamodb):
    """測試 mcp_tool_history 使用 source 過濾"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立測試資料
    table.put_item(Item={
        'request_id': 'req-003',
        'status': 'approved',
        'action': 'execute',
        'source': 'agent-a',
        'created_at': now - 1000
    })
    table.put_item(Item={
        'request_id': 'req-004',
        'status': 'approved',
        'action': 'execute',
        'source': 'agent-b',
        'created_at': now - 2000
    })

    req_id = 'test-history-003'
    arguments = {
        'source': 'agent-a',
        'limit': 10,
        'since_hours': 24
    }

    result = mcp_history_module.mcp_tool_history(req_id, arguments)

    # 應只回傳 agent-a 的記錄
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert len(data['items']) >= 1
    assert all(item['source'] == 'agent-a' for item in data['items'])


def test_history_invalid_limit(mcp_history_module):
    """測試 mcp_tool_history 無效的 limit"""
    req_id = 'test-history-004'
    arguments = {
        'limit': 'invalid',
        'since_hours': 24
    }

    result = mcp_history_module.mcp_tool_history(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'limit' in body['error']['message']


# ============================================================================
# Tests for mcp_tool_stats
# ============================================================================

def test_stats_empty_results(mcp_history_module):
    """測試 mcp_tool_stats 空結果"""
    req_id = 'test-stats-001'
    arguments = {}

    result = mcp_history_module.mcp_tool_stats(req_id, arguments)

    # 應回傳成功，但 total_requests=0
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['window_hours'] == 24
    assert data['total_requests'] == 0
    assert data['summary']['approved'] == 0
    assert data['summary']['denied'] == 0


def test_stats_with_data(mcp_history_module, mock_dynamodb):
    """測試 mcp_tool_stats 有資料"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立測試資料
    items = [
        {'request_id': 'req-s1', 'status': 'approved', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 1000, 'approved_at': now - 900},
        {'request_id': 'req-s2', 'status': 'denied', 'action': 'execute', 'source': 'agent-b', 'created_at': now - 2000},
        {'request_id': 'req-s3', 'status': 'pending', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 3000},
        {'request_id': 'req-s4', 'status': 'approved', 'action': 'upload', 'source': 'agent-a', 'created_at': now - 4000, 'approved_at': now - 3900},
    ]
    for item in items:
        table.put_item(Item=item)

    req_id = 'test-stats-002'
    arguments = {}

    result = mcp_history_module.mcp_tool_stats(req_id, arguments)

    # 驗證統計結果
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['total_requests'] == 4
    assert data['summary']['approved'] == 2
    assert data['summary']['denied'] == 1
    assert data['summary']['pending'] == 1
    assert data['approval_rate'] is not None
    assert 'top_sources' in data
    assert 'by_status' in data


def test_stats_approval_rate_calculation(mcp_history_module, mock_dynamodb):
    """測試 mcp_tool_stats 審批率計算"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立測試資料：3 approved, 1 denied
    items = [
        {'request_id': 'req-r1', 'status': 'approved', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 1000},
        {'request_id': 'req-r2', 'status': 'approved', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 2000},
        {'request_id': 'req-r3', 'status': 'approved', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 3000},
        {'request_id': 'req-r4', 'status': 'denied', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 4000},
    ]
    for item in items:
        table.put_item(Item=item)

    req_id = 'test-stats-003'
    arguments = {}

    result = mcp_history_module.mcp_tool_stats(req_id, arguments)

    # 審批率應該是 3/4 = 0.75
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['approval_rate'] == 0.75
