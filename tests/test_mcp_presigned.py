"""
Bouncer - mcp_presigned.py 測試
覆蓋 Presigned URL 上傳功能
"""

import json
import sys
import os
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
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def mcp_presigned_module(mock_dynamodb):
    """載入 mcp_presigned 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'

    # 清除模組
    modules_to_clear = [
        'mcp_presigned', 'db', 'constants', 'utils', 'rate_limit',
        'notifications', 'aws_clients'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]


    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import mcp_presigned
    yield mcp_presigned

    sys.path.pop(0)


# ============================================================================
# Tests for mcp_tool_request_presigned
# ============================================================================

def test_request_presigned_missing_filename(mcp_presigned_module):
    """測試 mcp_tool_request_presigned 缺少 filename 參數"""
    req_id = 'test-presigned-001'
    arguments = {
        'content_type': 'application/json',
        'reason': 'Testing',
        'source': 'test-agent'
    }

    result = mcp_presigned_module.mcp_tool_request_presigned(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'filename' in data['error']


def test_request_presigned_invalid_expires_in(mcp_presigned_module):
    """測試 mcp_tool_request_presigned 無效的 expires_in"""
    req_id = 'test-presigned-002'
    arguments = {
        'filename': 'test.json',
        'content_type': 'application/json',
        'reason': 'Testing',
        'source': 'test-agent',
        'expires_in': 5000  # 超過 MAX_EXPIRES_IN (3600)
    }

    result = mcp_presigned_module.mcp_tool_request_presigned(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'exceeds maximum' in data['error']


@patch('mcp_presigned.check_rate_limit')
@patch('mcp_presigned.send_presigned_notification')
@patch('mcp_presigned.get_s3_client')
def test_request_presigned_success(mock_s3, mock_notify, mock_rate, mcp_presigned_module, mock_dynamodb):
    """測試 mcp_tool_request_presigned 成功生成 presigned URL"""
    # Mock S3 client
    mock_s3_instance = MagicMock()
    mock_s3_instance.generate_presigned_url.return_value = 'https://s3.example.com/presigned-url'
    mock_s3.return_value = mock_s3_instance

    req_id = 'test-presigned-003'
    arguments = {
        'filename': 'test.json',
        'content_type': 'application/json',
        'reason': 'Testing upload',
        'source': 'test-agent',
        'expires_in': 900
    }

    result = mcp_presigned_module.mcp_tool_request_presigned(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'ready'
    assert 'presigned_url' in data
    assert data['presigned_url'] == 'https://s3.example.com/presigned-url'
    assert 'request_id' in data
    assert 'expires_at' in data

    # 驗證 rate limit 檢查
    mock_rate.assert_called_once_with('test-agent')

    # 驗證 notification
    mock_notify.assert_called_once()


# ============================================================================
# Tests for mcp_tool_request_presigned_batch
# ============================================================================

def test_request_presigned_batch_missing_files(mcp_presigned_module):
    """測試 mcp_tool_request_presigned_batch 缺少 files 參數"""
    req_id = 'test-batch-001'
    arguments = {
        'reason': 'Testing',
        'source': 'test-agent'
    }

    result = mcp_presigned_module.mcp_tool_request_presigned_batch(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'files' in data['error']


def test_request_presigned_batch_too_many_files(mcp_presigned_module):
    """測試 mcp_tool_request_presigned_batch 超過最大檔案數"""
    req_id = 'test-batch-002'
    files = [
        {'filename': f'file{i}.txt', 'content_type': 'text/plain'}
        for i in range(51)  # 超過 _BATCH_MAX_FILES (50)
    ]
    arguments = {
        'files': files,
        'reason': 'Testing',
        'source': 'test-agent'
    }

    result = mcp_presigned_module.mcp_tool_request_presigned_batch(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'exceeds maximum' in data['error'] or '50' in data['error']


@patch('mcp_presigned.check_rate_limit')
@patch('mcp_presigned.send_presigned_batch_notification')
@patch('mcp_presigned.get_s3_client')
def test_request_presigned_batch_success(mock_s3, mock_notify, mock_rate, mcp_presigned_module, mock_dynamodb):
    """測試 mcp_tool_request_presigned_batch 成功生成多個 presigned URLs"""
    # Mock S3 client
    mock_s3_instance = MagicMock()
    mock_s3_instance.generate_presigned_url.return_value = 'https://s3.example.com/presigned-url'
    mock_s3.return_value = mock_s3_instance

    req_id = 'test-batch-003'
    files = [
        {'filename': 'file1.txt', 'content_type': 'text/plain'},
        {'filename': 'file2.json', 'content_type': 'application/json'},
        {'filename': 'file3.csv', 'content_type': 'text/csv'}
    ]
    arguments = {
        'files': files,
        'reason': 'Testing batch upload',
        'source': 'test-agent',
        'expires_in': 900
    }

    result = mcp_presigned_module.mcp_tool_request_presigned_batch(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'ready'
    assert 'batch_id' in data
    assert data['file_count'] == 3
    assert len(data['files']) == 3
    assert all('presigned_url' in f for f in data['files'])

    # 驗證 rate limit 檢查
    mock_rate.assert_called_once_with('test-agent')

    # 驗證 notification
    mock_notify.assert_called_once()


def test_request_presigned_batch_invalid_file_entry(mcp_presigned_module):
    """測試 mcp_tool_request_presigned_batch 無效的 file entry"""
    req_id = 'test-batch-004'
    files = [
        {'filename': 'file1.txt', 'content_type': 'text/plain'},
        {'filename': 'file2.txt'},  # 缺少 content_type
    ]
    arguments = {
        'files': files,
        'reason': 'Testing',
        'source': 'test-agent'
    }

    result = mcp_presigned_module.mcp_tool_request_presigned_batch(req_id, arguments)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'content_type' in data['error']
