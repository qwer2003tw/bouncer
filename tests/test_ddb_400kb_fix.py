"""Regression tests for P1-2: DynamoDB 400KB Bomb fix.

Verifies that file content is stored in S3 (not DynamoDB) for both single
and batch uploads, and that approve callbacks use s3.copy_object().
"""
import base64
import json
import os
import sys
import time
from unittest.mock import patch, MagicMock, call

import pytest
import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def aws_credentials():
    os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
    os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['AWS_SECURITY_TOKEN'] = 'testing'
    os.environ['AWS_SESSION_TOKEN'] = 'testing'
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'


@pytest.fixture
def mock_aws_resources(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='bouncer-test-requests',
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
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                },
                {
                    'IndexName': 'source-created-index',
                    'KeySchema': [
                        {'AttributeName': 'source', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                },
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        table.wait_until_exists()

        # Create the staging/upload S3 bucket
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='bouncer-uploads-111111111111')

        yield {'dynamodb': dynamodb, 'table': table, 's3': s3}


@pytest.fixture
def upload_module(mock_aws_resources):
    """Load mcp_upload with mocked dependencies."""
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'bouncer-test-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # Clear cached modules
    for mod in list(sys.modules.keys()):
        if mod in ('mcp_upload', 'db', 'accounts', 'trust', 'rate_limit',
                   'notifications', 'telegram', 'constants', 'utils',
                   'smart_approval', 'risk_scorer'):
            del sys.modules[mod]

    import db
    db.table = mock_aws_resources['table']

    import mcp_upload
    mcp_upload.table = mock_aws_resources['table']

    yield mcp_upload, mock_aws_resources['table'], mock_aws_resources['s3']


@pytest.fixture
def callbacks_module(mock_aws_resources):
    """Load callbacks with mocked dependencies."""
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'bouncer-test-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    for mod in list(sys.modules.keys()):
        if mod in ('callbacks', 'mcp_upload', 'db', 'accounts', 'trust',
                   'rate_limit', 'notifications', 'telegram', 'constants',
                   'utils', 'smart_approval', 'risk_scorer', 'paging',
                   'commands', 'metrics', 'grant'):
            del sys.modules[mod]

    import db
    db.table = mock_aws_resources['table']

    import mcp_upload
    mcp_upload.table = mock_aws_resources['table']

    import callbacks
    callbacks._db.table = mock_aws_resources['table']

    yield callbacks, mcp_upload, mock_aws_resources['table'], mock_aws_resources['s3']


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_content_b64(text: str = 'hello world') -> str:
    return base64.b64encode(text.encode()).decode()


# ===========================================================================
# Single Upload Tests
# ===========================================================================

class TestSingleUploadDDBFix:
    """DDB item must not contain raw base64 content for single-file uploads."""

    @patch('telegram.send_telegram_message')
    def test_ddb_item_has_no_content_field(self, mock_tg, upload_module):
        """DDB item 不含 content，含 content_s3_key"""
        mcp_upload, table, s3 = upload_module

        result = mcp_upload.mcp_tool_upload('req-1', {
            'filename': 'hello.txt',
            'content': _make_content_b64('hello world'),
            'reason': 'test',
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval'
        request_id = resp['request_id']

        # Check DDB item
        item = table.get_item(Key={'request_id': request_id})['Item']
        assert 'content' not in item, "DDB item must NOT contain raw base64 content"
        assert 'content_s3_key' in item, "DDB item must have content_s3_key"
        assert item['content_s3_key'].startswith('pending/')

    @patch('telegram.send_telegram_message')
    def test_content_staged_to_s3(self, mock_tg, upload_module):
        """Content 已上傳到 S3 pending/ 路徑"""
        mcp_upload, table, s3 = upload_module
        content_text = 'hello world content'

        result = mcp_upload.mcp_tool_upload('req-2', {
            'filename': 'test.txt',
            'content': _make_content_b64(content_text),
            'reason': 'test',
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        request_id = resp['request_id']

        item = table.get_item(Key={'request_id': request_id})['Item']
        s3_key = item['content_s3_key']

        # Verify the object exists in S3
        obj = s3.get_object(Bucket='bouncer-uploads-111111111111', Key=s3_key)
        assert obj['Body'].read() == content_text.encode()

    @patch('telegram.send_telegram_message')
    def test_s3_upload_failure_no_ddb_write(self, mock_tg, upload_module):
        """S3 upload 失敗 → DDB 不寫，回 error"""
        mcp_upload, table, s3 = upload_module

        with patch('boto3.client') as mock_boto:
            mock_s3_client = MagicMock()
            mock_s3_client.put_object.side_effect = Exception('S3 connection refused')
            mock_boto.return_value = mock_s3_client

            result = mcp_upload.mcp_tool_upload('req-3', {
                'filename': 'fail.txt',
                'content': _make_content_b64('data'),
                'reason': 'test',
            })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'error'
        assert 'S3' in resp['error'] or 'stage' in resp['error'].lower()

        # DDB should be empty (no put_item happened)
        scan = table.scan()
        assert len(scan['Items']) == 0, "DDB must have no items when S3 upload fails"


# ===========================================================================
# Batch Upload Tests
# ===========================================================================

class TestBatchUploadDDBFix:
    """files_manifest in DDB must not contain content_b64."""

    @patch('telegram.send_telegram_message')
    @patch('telegram.send_batch_upload_notification', create=True)
    @patch('notifications.send_batch_upload_notification')
    def test_manifest_has_no_content_b64(self, mock_notif, mock_tg2, mock_tg, upload_module):
        """批量上傳：DDB files_manifest 不含 content_b64"""
        mcp_upload, table, s3 = upload_module

        files = [
            {'filename': 'a.txt', 'content': _make_content_b64('file a'), 'content_type': 'text/plain'},
            {'filename': 'b.txt', 'content': _make_content_b64('file b'), 'content_type': 'text/plain'},
        ]

        result = mcp_upload.mcp_tool_upload_batch('req-batch-1', {
            'files': files,
            'reason': 'batch test',
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval'
        batch_id = resp['request_id']

        item = table.get_item(Key={'request_id': batch_id})['Item']
        manifest = json.loads(item['files'])
        for fm in manifest:
            assert 'content_b64' not in fm, f"manifest entry for {fm['filename']} must not have content_b64"
            assert 's3_key' in fm, f"manifest entry for {fm['filename']} must have s3_key"
            assert fm['s3_key'].startswith('pending/')

    @patch('telegram.send_telegram_message')
    @patch('notifications.send_batch_upload_notification')
    def test_batch_files_staged_to_s3(self, mock_notif, mock_tg, upload_module):
        """批量上傳：每個檔案都存到 S3 pending/"""
        mcp_upload, table, s3 = upload_module

        file_contents = {'alpha.txt': 'alpha content', 'beta.txt': 'beta content'}
        files = [
            {'filename': fn, 'content': _make_content_b64(fc), 'content_type': 'text/plain'}
            for fn, fc in file_contents.items()
        ]

        result = mcp_upload.mcp_tool_upload_batch('req-batch-2', {
            'files': files,
            'reason': 'batch s3 test',
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        batch_id = resp['request_id']

        item = table.get_item(Key={'request_id': batch_id})['Item']
        manifest = json.loads(item['files'])

        for fm in manifest:
            key = fm['s3_key']
            obj = s3.get_object(Bucket='bouncer-uploads-111111111111', Key=key)
            actual = obj['Body'].read()
            expected = file_contents[fm['filename']].encode()
            assert actual == expected, f"S3 content mismatch for {fm['filename']}"

    @patch('telegram.send_telegram_message')
    @patch('notifications.send_batch_upload_notification')
    def test_batch_s3_failure_rollback(self, mock_notif, mock_tg, upload_module):
        """批量上傳：S3 upload 失敗 → DDB 不寫，回 error"""
        mcp_upload, table, s3 = upload_module

        files = [
            {'filename': 'x.txt', 'content': _make_content_b64('x'), 'content_type': 'text/plain'},
        ]

        call_count = [0]
        def fail_on_second_put(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                raise Exception('S3 put_object failed')

        with patch('boto3.client') as mock_boto:
            mock_s3_client = MagicMock()
            mock_s3_client.put_object.side_effect = Exception('S3 unavailable')
            mock_boto.return_value = mock_s3_client

            result = mcp_upload.mcp_tool_upload_batch('req-batch-fail', {
                'files': files,
                'reason': 'test',
            })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'error'

        scan = table.scan()
        assert len(scan['Items']) == 0, "DDB must have no items when S3 upload fails"


# ===========================================================================
# Approve Callback Tests
# ===========================================================================

class TestApproveCallbackS3Copy:
    """execute_upload should use s3.copy_object, not put_object."""

    def test_single_approve_uses_copy_object(self, mock_aws_resources):
        """approve 時從 S3 讀取並 copy 到目標 bucket"""
        s3 = mock_aws_resources['s3']
        table = mock_aws_resources['table']

        # Create target bucket
        s3.create_bucket(Bucket='target-bucket')

        # Stage the file in pending/
        content = b'staged content'
        s3.put_object(
            Bucket='bouncer-uploads-111111111111',
            Key='pending/req-approve-1/hello.txt',
            Body=content,
            ContentType='text/plain',
        )

        # Write DDB item (new format: content_s3_key)
        table.put_item(Item={
            'request_id': 'req-approve-1',
            'action': 'upload',
            'bucket': 'target-bucket',
            'key': '2026-02-25/req-approve-1/hello.txt',
            'content_s3_key': 'pending/req-approve-1/hello.txt',
            'content_type': 'text/plain',
            'content_size': len(content),
            'account_id': '111111111111',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'ttl': int(time.time()) + 300,
        })

        # Re-import with correct mocked table
        for mod in list(sys.modules.keys()):
            if mod in ('mcp_upload', 'db'):
                del sys.modules[mod]
        import db
        db.table = table
        import mcp_upload
        mcp_upload.table = table

        result = mcp_upload.execute_upload('req-approve-1', 'approver-123')

        assert result['success'] is True

        # Verify copy arrived at target
        target_obj = s3.get_object(Bucket='target-bucket', Key='2026-02-25/req-approve-1/hello.txt')
        assert target_obj['Body'].read() == content

        # Verify staging object is deleted
        import botocore.exceptions
        with pytest.raises(Exception):
            s3.head_object(Bucket='bouncer-uploads-111111111111', Key='pending/req-approve-1/hello.txt')

        # DDB status updated
        item = table.get_item(Key={'request_id': 'req-approve-1'})['Item']
        assert item['status'] == 'approved'

    def test_batch_approve_uses_copy_object(self, mock_aws_resources):
        """批量 approve 時從 S3 copy 到目標 bucket"""
        s3 = mock_aws_resources['s3']
        table = mock_aws_resources['table']

        # Files already staged under pending/
        batch_id = 'batch-approve-1'
        file_contents = {'file1.txt': b'content 1', 'file2.txt': b'content 2'}
        s3_keys = {}
        for fn, fc in file_contents.items():
            key = f'pending/{batch_id}/{fn}'
            s3.put_object(
                Bucket='bouncer-uploads-111111111111',
                Key=key,
                Body=fc,
                ContentType='text/plain',
            )
            s3_keys[fn] = key

        files_manifest = [
            {
                'filename': fn,
                's3_key': s3_keys[fn],
                'content_type': 'text/plain',
                'size': len(fc),
                'sha256': 'abc',
            }
            for fn, fc in file_contents.items()
        ]

        table.put_item(Item={
            'request_id': batch_id,
            'action': 'upload_batch',
            'bucket': 'bouncer-uploads-111111111111',  # target == staging bucket for simplicity
            'files': json.dumps(files_manifest),
            'file_count': 2,
            'total_size': sum(len(fc) for fc in file_contents.values()),
            'account_id': '111111111111',
            'account_name': 'Default',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'trust_scope': '',
            'ttl': int(time.time()) + 300,
        })

        # Load callbacks module with correct mocked table
        for mod in list(sys.modules.keys()):
            if mod in ('callbacks', 'mcp_upload', 'db', 'telegram', 'notifications',
                       'paging', 'commands', 'metrics', 'grant', 'trust',
                       'accounts', 'rate_limit', 'smart_approval', 'risk_scorer',
                       'constants', 'utils'):
                del sys.modules[mod]
        import db
        db.table = table
        import mcp_upload
        mcp_upload.table = table

        with patch('telegram.update_message'), \
             patch('telegram.answer_callback'), \
             patch('metrics.emit_metric'):
            import callbacks
            callbacks._db.table = table

            result = callbacks.handle_upload_batch_callback(
                action='approve',
                request_id=batch_id,
                item=table.get_item(Key={'request_id': batch_id})['Item'],
                message_id=1,
                callback_id='cb-1',
                user_id='approver-999',
            )

        assert result['statusCode'] == 200

        # Verify copied files exist in the bucket at their new keys
        resp = s3.list_objects_v2(Bucket='bouncer-uploads-111111111111', Prefix='2026-')
        keys_after = [o['Key'] for o in resp.get('Contents', [])]
        # At least 2 new objects copied
        assert len(keys_after) >= 2

        # Verify staging objects are deleted
        for fn in file_contents:
            staging_key = s3_keys[fn]
            exists = False
            try:
                s3.head_object(Bucket='bouncer-uploads-111111111111', Key=staging_key)
                exists = True
            except Exception:
                pass
            assert not exists, f"Staging object {staging_key} should be deleted after approve"

    def test_legacy_single_approve_still_works(self, mock_aws_resources):
        """舊格式（content 欄位）的 DDB item 仍可正常 approve（backward compat）"""
        s3 = mock_aws_resources['s3']
        table = mock_aws_resources['table']

        # Create target bucket
        s3.create_bucket(Bucket='target-bucket-legacy')

        content = b'legacy content'
        content_b64 = base64.b64encode(content).decode()

        table.put_item(Item={
            'request_id': 'req-legacy-1',
            'action': 'upload',
            'bucket': 'target-bucket-legacy',
            'key': 'legacy/file.txt',
            'content': content_b64,   # old format
            'content_type': 'text/plain',
            'content_size': len(content),
            'account_id': '111111111111',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'ttl': int(time.time()) + 300,
        })

        for mod in list(sys.modules.keys()):
            if mod in ('mcp_upload', 'db'):
                del sys.modules[mod]
        import db
        db.table = table
        import mcp_upload
        mcp_upload.table = table

        result = mcp_upload.execute_upload('req-legacy-1', 'approver-legacy')

        assert result['success'] is True
        target_obj = s3.get_object(Bucket='target-bucket-legacy', Key='legacy/file.txt')
        assert target_obj['Body'].read() == content
