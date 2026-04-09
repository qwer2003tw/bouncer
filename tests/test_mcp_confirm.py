"""
Bouncer - mcp_confirm.py 測試
覆蓋 Frontend deploy confirm 功能
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
def mock_s3():
    """建立 mock S3"""
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        # 建立測試 bucket
        s3.create_bucket(Bucket='bouncer-staging')
        yield s3


@pytest.fixture
def mcp_confirm_module(mock_dynamodb, mock_s3):
    """載入 mcp_confirm 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['STAGING_BUCKET'] = 'bouncer-staging'

    # 清除模組
    modules_to_clear = [
        'mcp_confirm', 'db', 'constants', 'utils', 'aws_clients'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import mcp_confirm
    yield mcp_confirm

    sys.path.pop(0)


# ============================================================================
# Tests for handle_confirm_upload
# ============================================================================

def test_confirm_upload_missing_batch_id(mcp_confirm_module):
    """測試 handle_confirm_upload 缺少 batch_id 參數"""
    params = {
        '_req_id': 'test-confirm-001',
        'files': [
            {'s3_key': '2026-04-01/batch-abc123/file1.txt'}
        ]
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'batch_id' in data['error']


def test_confirm_upload_invalid_batch_id(mcp_confirm_module):
    """測試 handle_confirm_upload 無效的 batch_id 格式"""
    params = {
        '_req_id': 'test-confirm-002',
        'batch_id': 'invalid-batch-id',  # 不符合 batch-{12 hex chars} 格式
        'files': [
            {'s3_key': '2026-04-01/batch-abc123/file1.txt'}
        ]
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'batch_id' in data['error'] or 'format' in data['error']


def test_confirm_upload_missing_files(mcp_confirm_module):
    """測試 handle_confirm_upload 缺少 files 參數"""
    params = {
        '_req_id': 'test-confirm-003',
        'batch_id': 'batch-abc123def456'
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'files' in data['error']


def test_confirm_upload_too_many_files(mcp_confirm_module):
    """測試 handle_confirm_upload 超過最大檔案數"""
    files = [
        {'s3_key': f'2026-04-01/batch-abc123/file{i}.txt'}
        for i in range(51)  # 超過 _CONFIRM_MAX_FILES (50)
    ]
    params = {
        '_req_id': 'test-confirm-004',
        'batch_id': 'batch-abc123def456',
        'files': files
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳錯誤
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['status'] == 'error'
    assert 'exceeds maximum' in data['error'] or '50' in data['error']


@patch('mcp_confirm.get_s3_client')
def test_confirm_upload_files_not_found(mock_s3_client, mcp_confirm_module, mock_dynamodb):
    """測試 handle_confirm_upload 檔案不存在"""
    # Mock S3 list_objects_v2 回傳空結果
    mock_s3_instance = MagicMock()
    mock_s3_instance.list_objects_v2.return_value = {
        'Contents': []  # 沒有檔案
    }
    mock_s3_client.return_value = mock_s3_instance

    batch_id = 'batch-abc123def456'
    s3_keys = [
        f'2026-04-01/{batch_id}/file1.txt',
        f'2026-04-01/{batch_id}/file2.txt'
    ]
    params = {
        '_req_id': 'test-confirm-005',
        'batch_id': batch_id,
        'files': [{'s3_key': k} for k in s3_keys]
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳 verified=False，所有檔案都在 missing
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['batch_id'] == batch_id
    assert data['verified'] is False
    assert len(data['missing']) == 2
    assert all(k in data['missing'] for k in s3_keys)


@patch('mcp_confirm.get_s3_client')
def test_confirm_upload_all_files_found(mock_s3_client, mcp_confirm_module, mock_dynamodb):
    """測試 handle_confirm_upload 所有檔案都存在"""
    batch_id = 'batch-abc123def456'
    s3_keys = [
        f'2026-04-01/{batch_id}/file1.txt',
        f'2026-04-01/{batch_id}/file2.txt'
    ]

    # Mock S3 list_objects_v2 回傳所有檔案
    mock_s3_instance = MagicMock()
    mock_s3_instance.list_objects_v2.return_value = {
        'Contents': [{'Key': k} for k in s3_keys],
        'IsTruncated': False
    }
    mock_s3_client.return_value = mock_s3_instance

    params = {
        '_req_id': 'test-confirm-006',
        'batch_id': batch_id,
        'files': [{'s3_key': k} for k in s3_keys]
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳 verified=True，missing 為空
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['batch_id'] == batch_id
    assert data['verified'] is True
    assert len(data['missing']) == 0
    assert len(data['results']) == 2
    assert all(r['exists'] for r in data['results'])

    # 驗證 DynamoDB audit record 寫入
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    item = table.get_item(Key={'request_id': f'CONFIRM#{batch_id}'}).get('Item')
    assert item is not None
    assert item['status'] == 'verified'
    assert item['verified'] is True


@patch('mcp_confirm.get_s3_client')
def test_confirm_upload_partial_files_found(mock_s3_client, mcp_confirm_module, mock_dynamodb):
    """測試 handle_confirm_upload 部分檔案存在"""
    batch_id = 'batch-abc123def456'
    s3_keys = [
        f'2026-04-01/{batch_id}/file1.txt',
        f'2026-04-01/{batch_id}/file2.txt',
        f'2026-04-01/{batch_id}/file3.txt'
    ]

    # Mock S3 list_objects_v2 只回傳前兩個檔案
    mock_s3_instance = MagicMock()
    mock_s3_instance.list_objects_v2.return_value = {
        'Contents': [{'Key': k} for k in s3_keys[:2]],
        'IsTruncated': False
    }
    mock_s3_client.return_value = mock_s3_instance

    params = {
        '_req_id': 'test-confirm-007',
        'batch_id': batch_id,
        'files': [{'s3_key': k} for k in s3_keys]
    }

    result = mcp_confirm_module.handle_confirm_upload(params)

    # 應回傳 verified=False，missing 包含 file3.txt
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)
    assert data['batch_id'] == batch_id
    assert data['verified'] is False
    assert len(data['missing']) == 1
    assert s3_keys[2] in data['missing']

    # 驗證 DynamoDB audit record 寫入
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    item = table.get_item(Key={'request_id': f'CONFIRM#{batch_id}'}).get('Item')
    assert item is not None
    assert item['status'] == 'incomplete'
    assert item['verified'] is False
    assert item['missing_count'] == 1
