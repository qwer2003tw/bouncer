"""
Bouncer - mcp_query_logs.py 測試
覆蓋 CloudWatch Logs 查詢功能
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
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'type', 'AttributeType': 'S'},
                {'AttributeName': 'expires_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'type-expires-at-index',
                    'KeySchema': [
                        {'AttributeName': 'type', 'KeyType': 'HASH'},
                        {'AttributeName': 'expires_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def mcp_query_logs_module(mock_dynamodb):
    """載入 mcp_query_logs 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'mcp_query_logs', 'db', 'constants', 'utils', 'accounts',
        'telegram', 'notifications'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import mcp_query_logs
    yield mcp_query_logs

    sys.path.pop(0)


# ============================================================================
# Tests for mcp_tool_query_logs
# ============================================================================

def test_query_logs_missing_log_group(mcp_query_logs_module):
    """測試 mcp_tool_query_logs 缺少 log_group 參數"""
    req_id = 'test-query-001'
    arguments = {
        'query': 'fields @timestamp, @message',
        'start_time': '-1h',
        'end_time': 'now'
    }

    result = mcp_query_logs_module.mcp_tool_query_logs(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'log_group' in body['error']['message']


def test_query_logs_invalid_log_group_prefix(mcp_query_logs_module):
    """測試 mcp_tool_query_logs 無效的 log_group prefix"""
    req_id = 'test-query-002'
    arguments = {
        'log_group': '/invalid/prefix/test',  # 不在 ALLOWED_LOG_GROUP_PREFIXES
        'query': 'fields @timestamp, @message',
        'start_time': '-1h',
        'end_time': 'now'
    }

    result = mcp_query_logs_module.mcp_tool_query_logs(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert '前綴' in body['error']['message'] or 'prefix' in body['error']['message'].lower()


def test_query_logs_time_range_validation(mcp_query_logs_module):
    """測試 mcp_tool_query_logs 時間範圍驗證"""
    req_id = 'test-query-003'
    arguments = {
        'log_group': '/aws/lambda/test',
        'query': 'fields @timestamp, @message',
        'start_time': int(time.time()),  # start > end
        'end_time': int(time.time()) - 3600
    }

    result = mcp_query_logs_module.mcp_tool_query_logs(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'start_time' in body['error']['message']


@patch('mcp_query_logs.send_telegram_message')
def test_query_logs_not_in_allowlist(mock_send_tg, mcp_query_logs_module, mock_dynamodb):
    """測試 mcp_tool_query_logs log group 不在 allowlist，應發送審批請求"""
    req_id = 'test-query-004'
    log_group = '/aws/lambda/test-function'

    arguments = {
        'log_group': log_group,
        'query': 'fields @timestamp, @message',
        'start_time': '-1h',
        'end_time': 'now'
    }

    with patch('mcp_query_logs._verify_log_group_exists', return_value=(True, '')):
        result = mcp_query_logs_module.mcp_tool_query_logs(req_id, arguments)

    # 應回傳 pending_approval
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'pending_approval'
    assert 'request_id' in data
    assert data['log_group'] == log_group

    # 驗證 Telegram 通知被呼叫
    mock_send_tg.assert_called_once()


# ============================================================================
# Tests for mcp_tool_logs_allowlist
# ============================================================================

def test_logs_allowlist_list_empty(mcp_query_logs_module):
    """測試 mcp_tool_logs_allowlist list action (空名單)"""
    req_id = 'test-allowlist-001'
    arguments = {
        'action': 'list',
        'account': '111111111111'
    }

    result = mcp_query_logs_module.mcp_tool_logs_allowlist(req_id, arguments)

    # 應回傳成功，count=0
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['count'] == 0
    assert data['account_id'] == '111111111111'
    assert isinstance(data['entries'], list)


def test_logs_allowlist_add_missing_log_group(mcp_query_logs_module):
    """測試 mcp_tool_logs_allowlist add action 缺少 log_group"""
    req_id = 'test-allowlist-002'
    arguments = {
        'action': 'add',
        'account': '111111111111'
    }

    result = mcp_query_logs_module.mcp_tool_logs_allowlist(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'log_group' in body['error']['message']


@patch('mcp_query_logs._verify_log_group_exists', return_value=(True, ''))
def test_logs_allowlist_add_success(mock_verify, mcp_query_logs_module, mock_dynamodb):
    """測試 mcp_tool_logs_allowlist add action 成功"""
    req_id = 'test-allowlist-003'
    log_group = '/aws/lambda/test-function'
    arguments = {
        'action': 'add',
        'account': '111111111111',
        'log_group': log_group,
        'source': 'test-user'
    }

    result = mcp_query_logs_module.mcp_tool_logs_allowlist(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'added'
    assert data['log_group'] == log_group

    # 驗證 DynamoDB 寫入
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    key = f'LOGS_ALLOWLIST#111111111111#{log_group}'
    item = table.get_item(Key={'request_id': key}).get('Item')
    assert item is not None
    assert item['log_group'] == log_group


def test_logs_allowlist_remove_success(mcp_query_logs_module, mock_dynamodb):
    """測試 mcp_tool_logs_allowlist remove action"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    log_group = '/aws/lambda/test-function'
    key = f'LOGS_ALLOWLIST#111111111111#{log_group}'

    # 先加入 allowlist
    table.put_item(Item={
        'request_id': key,
        'type': 'logs_allowlist',
        'account_id': '111111111111',
        'log_group': log_group,
        'created_at': int(time.time()),
        'expires_at': 0
    })

    req_id = 'test-allowlist-004'
    arguments = {
        'action': 'remove',
        'account': '111111111111',
        'log_group': log_group
    }

    result = mcp_query_logs_module.mcp_tool_logs_allowlist(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'removed'

    # 驗證 DynamoDB 刪除
    item = table.get_item(Key={'request_id': key}).get('Item')
    assert item is None


def test_logs_allowlist_unknown_action(mcp_query_logs_module):
    """測試 mcp_tool_logs_allowlist 未知的 action"""
    req_id = 'test-allowlist-005'
    arguments = {
        'action': 'invalid_action',
        'account': '111111111111'
    }

    result = mcp_query_logs_module.mcp_tool_logs_allowlist(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    assert body['error']['code'] == -32602
    assert 'Unknown action' in body['error']['message']


# ============================================================================
# Tests for execute_log_insights — regression test for #385
# ============================================================================

def test_execute_log_insights_eventual_consistency(mcp_query_logs_module):
    """測試 execute_log_insights 處理 AWS eventual consistency

    Regression test for #385: AWS may return status=Complete before results
    are populated. The function should poll again if results are empty but
    statistics show recordsMatched > 0.
    """
    from unittest.mock import MagicMock

    # Mock logs client
    mock_logs_client = MagicMock()

    # start_query returns query_id
    mock_logs_client.start_query.return_value = {'queryId': 'test-query-123'}

    # get_query_results returns Complete with empty results first,
    # then with results populated on second call
    mock_logs_client.get_query_results.side_effect = [
        # First call: status=Complete but results empty, recordsMatched=3
        {
            'status': 'Complete',
            'results': [],
            'statistics': {'recordsMatched': 3, 'recordsScanned': 100, 'bytesScanned': 1024}
        },
        # Second call: results now populated
        {
            'status': 'Complete',
            'results': [
                [{'field': '@timestamp', 'value': '2026-04-18 12:00:00'},
                 {'field': '@message', 'value': 'test log 1'}],
                [{'field': '@timestamp', 'value': '2026-04-18 12:01:00'},
                 {'field': '@message', 'value': 'test log 2'}],
                [{'field': '@timestamp', 'value': '2026-04-18 12:02:00'},
                 {'field': '@message', 'value': 'test log 3'}],
            ],
            'statistics': {'recordsMatched': 3, 'recordsScanned': 100, 'bytesScanned': 1024}
        },
    ]

    # Patch _get_logs_client to return our mock
    with patch('mcp_query_logs._get_logs_client', return_value=mock_logs_client):
        result = mcp_query_logs_module.execute_log_insights(
            log_group='/aws/lambda/test',
            query_with_limit='fields @timestamp, @message | limit 100',
            start_time=int(time.time()) - 3600,
            end_time=int(time.time()),
            region='us-east-1',
            account_id='111111111111'
        )

    # Should have polled twice (once in main loop, once in eventual consistency handler)
    assert mock_logs_client.get_query_results.call_count == 2

    # Should return complete status with results
    assert result['status'] == 'complete'
    assert result['records_matched'] == 3
    assert len(result['results']) == 3
    assert result['results'][0]['@message'] == 'test log 1'
    assert result['statistics']['records_matched'] == 3


def test_execute_log_insights_eventual_consistency_timeout(mcp_query_logs_module):
    """測試 execute_log_insights 當 results 永遠不來時的超時處理

    If status=Complete and recordsMatched > 0 but results never populate
    within MAX_RESULTS_WAIT, should return empty results with statistics.
    """
    from unittest.mock import MagicMock

    mock_logs_client = MagicMock()
    mock_logs_client.start_query.return_value = {'queryId': 'test-query-456'}

    # Always return Complete with empty results but recordsMatched > 0
    mock_logs_client.get_query_results.return_value = {
        'status': 'Complete',
        'results': [],
        'statistics': {'recordsMatched': 5, 'recordsScanned': 200, 'bytesScanned': 2048}
    }

    with patch('mcp_query_logs._get_logs_client', return_value=mock_logs_client):
        result = mcp_query_logs_module.execute_log_insights(
            log_group='/aws/lambda/test',
            query_with_limit='fields @timestamp, @message | limit 100',
            start_time=int(time.time()) - 3600,
            end_time=int(time.time()),
            region='us-east-1',
            account_id='111111111111'
        )

    # Should have polled multiple times (trying to get results)
    assert mock_logs_client.get_query_results.call_count >= 2

    # Should return complete status but with empty results
    # Statistics should still show recordsMatched=5
    assert result['status'] == 'complete'
    assert result['records_matched'] == 0  # no results returned
    assert result['statistics']['records_matched'] == 5  # but stats say 5 matched
