"""
Regression Guard: Cross-Account Upload Staging Bucket

關鍵不變量：
  staging bucket 永遠使用 DEFAULT_ACCOUNT_ID，不管 target account 是什麼。
  Lambda IAM policy 只允許存取 bouncer-uploads-{DEFAULT_ACCOUNT_ID}。
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

DEFAULT_ACCOUNT = '190825685292'
DEV_ACCOUNT = '992382394211'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    monkeypatch.setenv('AWS_SESSION_TOKEN', 'test')
    monkeypatch.setenv('DEFAULT_ACCOUNT_ID', DEFAULT_ACCOUNT)
    monkeypatch.setenv('TABLE_NAME', 'clawdbot-approval-requests')
    monkeypatch.setenv('REQUEST_SECRET', 'test-secret')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('APPROVED_CHAT_ID', '999999999')
    monkeypatch.setenv('MCP_MAX_WAIT', '5')


@pytest.fixture
def mock_infra(aws_env):
    """Spin up moto-backed DynamoDB + S3 with the two expected buckets."""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

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

        s3 = boto3.client('s3', region_name='us-east-1')
        # Create ONLY the DEFAULT_ACCOUNT bucket.
        # DEV_ACCOUNT bucket intentionally absent to prove we never write to it.
        s3.create_bucket(Bucket=f'bouncer-uploads-{DEFAULT_ACCOUNT}')

        # Accounts table
        accounts_table = dynamodb.create_table(
            TableName='bouncer-accounts',
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'account_id', 'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        accounts_table.wait_until_exists()

        # Register DEFAULT account
        accounts_table.put_item(Item={
            'account_id': DEFAULT_ACCOUNT,
            'name': 'Default',
            'enabled': True,
            'created_at': 1000,
        })
        # Register DEV account (cross-account target)
        accounts_table.put_item(Item={
            'account_id': DEV_ACCOUNT,
            'name': 'Dev',
            'role_arn': f'arn:aws:iam::{DEV_ACCOUNT}:role/BouncerExecutionRole',
            'enabled': True,
            'created_at': 1001,
        })

        yield {
            'dynamodb': dynamodb,
            'table': table,
            's3': s3,
            'accounts_table': accounts_table,
        }


@pytest.fixture
def mcp_upload_module(mock_infra, monkeypatch):
    """Load mcp_upload with all modules cleared to get fresh state.

    bouncer-bug-014 fix (Approach B):
    Use monkeypatch to restore accounts._accounts_table so that state is
    always cleaned up regardless of test outcome or ordering.  Previously
    the manual ``accounts._accounts_table = None`` in the yield block could
    be skipped on exception, leaking the moto-backed resource into
    subsequent tests that import accounts from a cached sys.modules entry.
    """
    # Purge all relevant cached modules so we get a truly fresh import
    # inside the moto context provided by mock_infra.
    _modules_to_purge = [
        'mcp_upload', 'src.mcp_upload',
        'accounts', 'src.accounts',
        'db', 'src.db',
        'constants', 'src.constants',
    ]
    for mod in list(sys.modules.keys()):
        if mod in _modules_to_purge:
            del sys.modules[mod]

    import mcp_upload
    import accounts

    # Patch mcp_upload module-level constants using monkeypatch (auto-undone)
    monkeypatch.setattr(mcp_upload, 'DEFAULT_ACCOUNT_ID', DEFAULT_ACCOUNT)
    monkeypatch.setattr(mcp_upload, 'table', mock_infra['table'])

    # Use monkeypatch to set accounts._accounts_table so it is *guaranteed*
    # to be restored to its pre-test value after the test completes (even on
    # failure or exception).  This is the core fix for bouncer-bug-014:
    # the previous manual cleanup in a yield block could be skipped.
    monkeypatch.setattr(accounts, '_accounts_table', mock_infra['accounts_table'])

    yield mcp_upload

    # Note: monkeypatch handles teardown automatically - no manual cleanup needed.


# ---------------------------------------------------------------------------
# Test 1: single-file upload – staging bucket must be DEFAULT_ACCOUNT_ID
# ---------------------------------------------------------------------------

class TestUploadCrossAccountStagingUsesDefault:

    @patch('telegram.send_telegram_message')
    def test_upload_cross_account_staging_uses_default_account_id(
        self, mock_telegram, mock_infra, mcp_upload_module
    ):
        """
        Regression: staging bucket 必須用 DEFAULT_ACCOUNT_ID，不管 target account 是什麼。

        傳 account='992382394211'（Dev 帳號）時，
        staging 應寫到 bouncer-uploads-190825685292（DEFAULT），
        而不是 bouncer-uploads-992382394211。

        bouncer-uploads-992382394211 根本不存在 → 寫到 Dev bucket 會直接炸掉。
        """
        content = base64.b64encode(b'cross account test content').decode()

        with patch('boto3.client') as mock_boto_client:
            mock_s3 = MagicMock()
            mock_s3.meta.region_name = 'us-east-1'
            mock_boto_client.return_value = mock_s3

            result = mcp_upload_module.mcp_tool_upload('req-001', {
                'filename': 'deploy.zip',
                'content': content,
                'reason': 'cross-account deploy',
                'source': 'test-bot',
                'account': DEV_ACCOUNT,
            })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval', f"Expected pending_approval, got: {resp}"

        # Verify put_object was called with DEFAULT bucket, NOT DEV bucket
        put_calls = mock_s3.put_object.call_args_list
        assert len(put_calls) >= 1, "Expected at least one put_object call"

        staging_call = put_calls[0]
        called_bucket = staging_call[1].get('Bucket') or staging_call[0][0] if staging_call[0] else staging_call[1]['Bucket']
        assert called_bucket == f'bouncer-uploads-{DEFAULT_ACCOUNT}', (
            f"Staging bucket must be bouncer-uploads-{DEFAULT_ACCOUNT} (DEFAULT_ACCOUNT_ID), "
            f"but got: {called_bucket}"
        )
        assert called_bucket != f'bouncer-uploads-{DEV_ACCOUNT}', (
            f"Staging MUST NOT use DEV account bucket bouncer-uploads-{DEV_ACCOUNT}"
        )

        # Check DynamoDB item: s3_uri should reference DEFAULT bucket for staging
        items = mock_infra['table'].scan()['Items']
        upload_items = [i for i in items if i.get('action') == 'upload']
        assert len(upload_items) >= 1
        item = upload_items[-1]

        # content_s3_key should be in DEFAULT staging bucket
        content_s3_key = item.get('content_s3_key', '')
        assert content_s3_key.startswith('pending/'), (
            f"content_s3_key should start with 'pending/', got: {content_s3_key}"
        )
        # target bucket (for execution) uses target account
        target_bucket = item.get('bucket', '')
        assert DEV_ACCOUNT in target_bucket, (
            f"Target bucket should use DEV account, got: {target_bucket}"
        )


# ---------------------------------------------------------------------------
# Test 2: batch upload – staging bucket must be DEFAULT_ACCOUNT_ID
# ---------------------------------------------------------------------------

class TestUploadBatchCrossAccountStagingUsesDefault:

    @patch('telegram.send_telegram_message')
    def test_upload_batch_cross_account_staging_uses_default_account_id(
        self, mock_telegram, mock_infra, mcp_upload_module
    ):
        """
        Regression: batch upload 的 staging bucket 必須用 DEFAULT_ACCOUNT_ID。

        即使 account=DEV_ACCOUNT，每個 file 的 staging put_object
        都應打到 bouncer-uploads-{DEFAULT_ACCOUNT}。
        """
        files = [
            {
                'filename': 'index.html',
                'content': base64.b64encode(b'<html></html>').decode(),
                'content_type': 'text/html',
            },
            {
                'filename': 'app.js',
                'content': base64.b64encode(b'console.log("hi")').decode(),
                'content_type': 'application/javascript',
            },
        ]

        with patch('boto3.client') as mock_boto_client:
            mock_s3 = MagicMock()
            mock_s3.meta.region_name = 'us-east-1'
            mock_boto_client.return_value = mock_s3

            result = mcp_upload_module.mcp_tool_upload_batch('req-batch-001', {
                'files': files,
                'reason': 'batch cross-account upload',
                'source': 'test-bot',
                'account': DEV_ACCOUNT,
            })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval', f"Expected pending_approval, got: {resp}"

        # All put_object calls must use DEFAULT staging bucket
        put_calls = mock_s3.put_object.call_args_list
        assert len(put_calls) == len(files), (
            f"Expected {len(files)} put_object calls (one per file), got {len(put_calls)}"
        )

        for i, put_call in enumerate(put_calls):
            called_bucket = (
                put_call[1].get('Bucket') or
                (put_call[0][0] if put_call[0] else None)
            )
            assert called_bucket == f'bouncer-uploads-{DEFAULT_ACCOUNT}', (
                f"File {i}: staging bucket must be bouncer-uploads-{DEFAULT_ACCOUNT} "
                f"(DEFAULT_ACCOUNT_ID), but got: {called_bucket}"
            )
            assert called_bucket != f'bouncer-uploads-{DEV_ACCOUNT}', (
                f"File {i}: staging MUST NOT use DEV bucket bouncer-uploads-{DEV_ACCOUNT}"
            )

        # DynamoDB item: 'files' field (JSON string) should list s3_key (pending/...)
        items = mock_infra['table'].scan()['Items']
        batch_items = [i for i in items if i.get('action') == 'upload_batch']
        assert len(batch_items) >= 1
        batch_item = batch_items[-1]

        # 'files' is stored as a JSON string in DynamoDB
        files_field = batch_item.get('files', '[]')
        if isinstance(files_field, str):
            import json as _json
            files_list = _json.loads(files_field)
        else:
            files_list = files_field

        assert len(files_list) == len(files), (
            f"Expected {len(files)} files in manifest, got {len(files_list)}: {files_list}"
        )
        for fm in files_list:
            assert fm.get('s3_key', '').startswith('pending/'), (
                f"files entry should have s3_key starting with 'pending/', got: {fm}"
            )


# ---------------------------------------------------------------------------
# Test 3: execute_upload – staging source must be DEFAULT_ACCOUNT_ID
# ---------------------------------------------------------------------------

class TestExecuteUploadStagingSourceIsDefault:

    def test_execute_upload_staging_source_is_default(
        self, mock_infra, mcp_upload_module, monkeypatch
    ):
        """
        execute_upload 從 DEFAULT_ACCOUNT_ID staging 複製到 target bucket，
        不管 item 的 account_id 是什麼（即使是 DEV_ACCOUNT）。

        這個測試確認 copy_object 的 CopySource.Bucket 固定是
        bouncer-uploads-{DEFAULT_ACCOUNT_ID}。
        """
        request_id = 'test-execute-cross-account-staging'
        content_s3_key = f'pending/{request_id}/deploy.zip'

        # Set up a DDB item simulating a cross-account upload approval
        mock_infra['table'].put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': f'bouncer-uploads-{DEV_ACCOUNT}',   # target bucket is DEV
            'key': '2026-02-25/uuid/deploy.zip',
            'content_type': 'application/zip',
            'content_s3_key': content_s3_key,              # staged in DEFAULT bucket
            'assume_role': f'arn:aws:iam::{DEV_ACCOUNT}:role/BouncerExecutionRole',
            'account_id': DEV_ACCOUNT,
            'status': 'pending_approval',
            'source': 'test-bot',
            'created_at': int(time.time()),
        })

        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            'Credentials': {
                'AccessKeyId': 'AKIA_TEST',
                'SecretAccessKey': 'test-secret',
                'SessionToken': 'test-token',
            }
        }
        mock_s3 = MagicMock()
        mock_s3.meta.region_name = 'us-east-1'

        def _boto_client(service, **kwargs):
            if service == 'sts':
                return mock_sts
            return mock_s3

        with patch('boto3.client', side_effect=_boto_client):
            result = mcp_upload_module.execute_upload(request_id, 'test-approver')

        assert result['success'] is True, f"execute_upload failed: {result}"

        # copy_object must read from DEFAULT staging bucket
        copy_calls = mock_s3.copy_object.call_args_list
        assert len(copy_calls) == 1, f"Expected 1 copy_object call, got {len(copy_calls)}"

        copy_kwargs = copy_calls[0][1]
        copy_source = copy_kwargs.get('CopySource', {})

        assert copy_source.get('Bucket') == f'bouncer-uploads-{DEFAULT_ACCOUNT}', (
            f"CopySource.Bucket must be bouncer-uploads-{DEFAULT_ACCOUNT} (DEFAULT_ACCOUNT_ID), "
            f"but got: {copy_source.get('Bucket')}"
        )
        assert copy_source.get('Bucket') != f'bouncer-uploads-{DEV_ACCOUNT}', (
            f"CopySource.Bucket MUST NOT be bouncer-uploads-{DEV_ACCOUNT}"
        )
        assert copy_source.get('Key') == content_s3_key, (
            f"CopySource.Key must be {content_s3_key}, got: {copy_source.get('Key')}"
        )

        # Destination bucket is the DEV account target bucket
        assert copy_kwargs.get('Bucket') == f'bouncer-uploads-{DEV_ACCOUNT}', (
            f"Destination Bucket should be DEV account bucket, got: {copy_kwargs.get('Bucket')}"
        )
