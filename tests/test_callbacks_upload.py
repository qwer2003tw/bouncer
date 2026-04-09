"""
Bouncer - callbacks_upload.py 測試
覆蓋上傳審批 callback 的核心邏輯
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock

from moto import mock_aws
import boto3
from botocore.exceptions import ClientError


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dynamodb():
    """建立 mock DynamoDB 表和 S3 buckets"""
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

        # 建立 S3 buckets
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='bouncer-uploads-111111111111')
        s3.create_bucket(Bucket='test-target-bucket')

        yield dynamodb


@pytest.fixture
def callbacks_upload_module(mock_dynamodb):
    """載入 callbacks_upload 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'callbacks_upload', 'callbacks', 'db', 'constants', 'utils',
        'telegram', 'trust', 'metrics', 'mcp_upload', 'aws_clients'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import callbacks_upload
    yield callbacks_upload

    sys.path.pop(0)


# ============================================================================
# Tests for handle_upload_callback (single file)
# ============================================================================

@patch('callbacks_upload.execute_upload')
@patch('callbacks_upload.update_message')
@patch('callbacks_upload.answer_callback')
@patch('callbacks_upload.emit_metric')
def test_upload_callback_approve_success(mock_emit, mock_answer, mock_update, mock_exec, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_callback approve 成功上傳"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'upload-req-001'

    # 建立上傳請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload',
        'bucket': 'test-target-bucket',
        'key': 'uploads/test.txt',
        'content_size': 1024,
        'source': 'test-agent',
        'reason': 'Upload test file',
        'context': 'testing',
        'account_id': '111111111111',
        'account_name': 'Test',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    # Mock 執行結果
    mock_exec.return_value = {
        'success': True,
        's3_url': 's3://test-target-bucket/uploads/test.txt'
    }

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['ok'] is True

    # 驗證 execute_upload 被呼叫
    mock_exec.assert_called_once_with(request_id, 'user123')

    # 驗證 update_message 顯示成功
    mock_update.assert_called_once()
    call_args = mock_update.call_args[0][1]
    assert '✅' in call_args


@patch('callbacks_upload.execute_upload')
@patch('callbacks_upload.update_message')
@patch('callbacks_upload.answer_callback')
def test_upload_callback_approve_failure(mock_answer, mock_update, mock_exec, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_callback approve 上傳失敗"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'upload-req-002'

    # 建立上傳請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload',
        'bucket': 'test-target-bucket',
        'key': 'uploads/test.txt',
        'content_size': 1024,
        'source': 'test-agent',
        'reason': 'Upload test file',
        'account_id': '111111111111',
        'account_name': 'Test',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    # Mock 執行失敗
    mock_exec.return_value = {
        'success': False,
        'error': 'S3 access denied'
    }

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 update_message 顯示失敗
    call_args = mock_update.call_args[0][1]
    assert '❌' in call_args
    assert 'S3 access denied' in call_args


@patch('callbacks_upload.update_message')
@patch('callbacks_upload.answer_callback')
@patch('callbacks_upload.emit_metric')
def test_upload_callback_deny(mock_emit, mock_answer, mock_update, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_callback deny 拒絕上傳"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'upload-req-003'

    # 建立上傳請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload',
        'bucket': 'test-target-bucket',
        'key': 'uploads/test.txt',
        'content_size': 1024,
        'source': 'test-agent',
        'reason': 'Upload test file',
        'account_id': '111111111111',
        'account_name': 'Test',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_callback(
        action='deny',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 顯示拒絕
    mock_answer.assert_called_once()
    assert '❌' in mock_answer.call_args[0][1]

    # 驗證 DDB 狀態
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'denied'


@patch('callbacks_upload.answer_callback')
@patch('callbacks_upload.update_message')
def test_upload_callback_expired(mock_update, mock_answer, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_callback 處理過期請求"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'upload-req-004'

    # 建立過期請求
    expired_time = int(time.time()) - 100
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload',
        'bucket': 'test-target-bucket',
        'key': 'uploads/test.txt',
        'content_size': 1024,
        'source': 'test-agent',
        'reason': 'Upload test file',
        'account_id': '111111111111',
        'created_at': expired_time - 600,
        'ttl': expired_time  # 已過期
    })

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示過期
    mock_answer.assert_called_once()
    assert '過期' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for handle_upload_batch_callback
# ============================================================================

@patch('callbacks_upload.get_s3_client')
@patch('callbacks_upload.update_message')
@patch('callbacks_upload.answer_callback')
@patch('callbacks_upload.emit_metric')
def test_upload_batch_callback_success(mock_emit, mock_answer, mock_update, mock_s3, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_batch_callback 批量上傳成功"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'batch-upload-001'

    # 準備測試檔案
    files_manifest = [
        {'filename': 'file1.txt', 's3_key': 'staging/file1.txt', 'size': 100, 'content_type': 'text/plain'},
        {'filename': 'file2.txt', 's3_key': 'staging/file2.txt', 'size': 200, 'content_type': 'text/plain'}
    ]

    # 建立批量上傳請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload_batch',
        'bucket': 'test-target-bucket',
        'file_count': 2,
        'total_size': 300,
        'files': json.dumps(files_manifest),
        'source': 'test-agent',
        'reason': 'Batch upload',
        'account_id': '111111111111',
        'account_name': 'Test',
        'trust_scope': '',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    # Mock S3 clients
    mock_staging = MagicMock()
    mock_target = MagicMock()
    mock_s3.side_effect = [mock_staging, mock_target]

    # Mock S3 get_object
    mock_staging.get_object.side_effect = [
        {'Body': MagicMock(read=lambda: b'content1')},
        {'Body': MagicMock(read=lambda: b'content2')}
    ]

    # Mock head_object for verification
    mock_target.head_object.side_effect = [
        {'ContentLength': 100},
        {'ContentLength': 200}
    ]

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_batch_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 S3 put_object 被呼叫
    assert mock_target.put_object.call_count == 2

    # 驗證 DDB 更新
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'approved'
    assert item['uploaded_count'] == 2


@patch('callbacks_upload.update_message')
@patch('callbacks_upload.answer_callback')
def test_upload_batch_callback_parse_error(mock_answer, mock_update, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_batch_callback 解析 manifest 失敗"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'batch-upload-002'

    # 建立請求，但 files 格式錯誤
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload_batch',
        'bucket': 'test-target-bucket',
        'file_count': 2,
        'total_size': 300,
        'files': 'invalid json',  # 無效 JSON
        'source': 'test-agent',
        'reason': 'Batch upload',
        'account_id': '111111111111',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_batch_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 500

    # 驗證 answer_callback 提示錯誤
    mock_answer.assert_called_once()
    assert '解析失敗' in mock_answer.call_args[0][1]


@patch('callbacks_upload.update_message')
@patch('callbacks_upload.answer_callback')
@patch('callbacks_upload.emit_metric')
def test_upload_batch_callback_deny(mock_emit, mock_answer, mock_update, callbacks_upload_module, mock_dynamodb):
    """測試 handle_upload_batch_callback deny 拒絕批量上傳"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'batch-upload-003'

    # 建立批量上傳請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload_batch',
        'bucket': 'test-target-bucket',
        'file_count': 3,
        'total_size': 500,
        'files': json.dumps([]),
        'source': 'test-agent',
        'reason': 'Batch upload',
        'account_id': '111111111111',
        'account_name': 'Test',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_upload_module.handle_upload_batch_callback(
        action='deny',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 DDB 狀態
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'denied'


# ============================================================================
# Tests for helper functions
# ============================================================================

def test_parse_callback_files_manifest_success(callbacks_upload_module):
    """測試 _parse_callback_files_manifest 成功解析"""
    files_manifest = [
        {'filename': 'test.txt', 's3_key': 'staging/test.txt', 'size': 100}
    ]
    item = {
        'files': json.dumps(files_manifest)
    }

    result = callbacks_upload_module._parse_callback_files_manifest(item, 'cb123')

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]['filename'] == 'test.txt'


@patch('callbacks_upload.answer_callback')
def test_parse_callback_files_manifest_invalid_json(mock_answer, callbacks_upload_module):
    """測試 _parse_callback_files_manifest 處理無效 JSON"""
    item = {
        'files': 'not valid json'
    }

    result = callbacks_upload_module._parse_callback_files_manifest(item, 'cb123')

    # 應回傳錯誤 response
    assert isinstance(result, dict)
    assert result['statusCode'] == 500


@patch('callbacks_upload.get_s3_client')
def test_setup_callback_s3_clients_success(mock_s3, callbacks_upload_module):
    """測試 _setup_callback_s3_clients 成功建立 clients"""
    mock_staging = MagicMock()
    mock_target = MagicMock()
    mock_s3.side_effect = [mock_staging, mock_target]

    result = callbacks_upload_module._setup_callback_s3_clients(
        assume_role=None,
        table=MagicMock(),
        request_id='test-req',
        user_id='user123',
        message_id=12345
    )

    # 應回傳 tuple
    assert isinstance(result, tuple)
    assert len(result) == 2


@patch('callbacks_upload.get_s3_client')
@patch('callbacks_upload.update_message')
def test_setup_callback_s3_clients_error(mock_update, mock_s3, callbacks_upload_module, mock_dynamodb):
    """測試 _setup_callback_s3_clients 處理 S3 連線錯誤"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')

    # Mock S3 client 失敗
    mock_s3.side_effect = ClientError(
        {'Error': {'Code': 'AccessDenied', 'Message': 'Access denied'}},
        'GetObject'
    )

    result = callbacks_upload_module._setup_callback_s3_clients(
        assume_role=None,
        table=table,
        request_id='test-req',
        user_id='user123',
        message_id=12345
    )

    # 應回傳錯誤 response
    assert isinstance(result, dict)
    assert result['statusCode'] == 500
