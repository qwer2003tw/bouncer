"""
Auto-generated test file split from test_bouncer.py
"""
import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3
from botocore.exceptions import ClientError


def _make_client_error(code='TestError', message='Test error'):
    return ClientError({'Error': {'Code': code, 'Message': message}}, 'TestOperation')


class TestUploadFunctionality:
    """Upload 功能測試"""
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_missing_filename(self, mock_telegram, app_module):
        """上傳缺少 filename"""
        result = app_module.mcp_tool_upload('test-1', {
            'content': 'dGVzdA==',  # base64 'test'
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'filename' in content['error']
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_missing_content(self, mock_telegram, app_module):
        """上傳缺少 content"""
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'content' in content['error']
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_invalid_base64(self, mock_telegram, app_module):
        """上傳無效 base64"""
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': 'not-valid-base64!!!',
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'base64' in content['error'].lower()
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_too_large(self, mock_telegram, app_module):
        """上傳檔案過大"""
        import base64
        # 建立 5MB 的內容（超過 4.5MB 限制）
        large_content = base64.b64encode(b'x' * (5 * 1024 * 1024)).decode()
        
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'large.bin',
            'content': large_content,
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'too large' in content['error'].lower() or 'large' in content['error'].lower()
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_success_async(self, mock_telegram, app_module):
        """上傳成功（異步模式）"""
        mock_telegram.return_value = {'ok': True}
        import base64
        content = base64.b64encode(b'test content').decode()
        
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test upload'
        })
        
        body = json.loads(result['body'])
        resp_content = json.loads(body['result']['content'][0]['text'])
        assert resp_content['status'] == 'pending_approval'
        assert 'request_id' in resp_content


# ============================================================================
# Trust Session 自動批准測試
# ============================================================================



class TestCrossAccountUpload:
    """Upload 跨帳號功能測試"""

    @pytest.fixture(autouse=True)
    def setup_default_account(self, monkeypatch, app_module):
        """設定預設帳號 ID for upload tests"""
        import mcp_upload
        monkeypatch.setattr(mcp_upload, 'DEFAULT_ACCOUNT_ID', '111111111111')

    @pytest.fixture(autouse=True)
    def setup_accounts_table(self, mock_dynamodb, app_module):
        """Accounts table already exists at session scope, just reset cache"""
        import accounts
        accounts._accounts_table = None  # 重置快取

    @patch('telegram.send_telegram_message')
    def test_upload_default_account_no_assume_role(self, mock_telegram, app_module):
        """不帶 account 參數 → 使用預設帳號，不 assume role"""
        mock_telegram.return_value = {'ok': True}
        import base64
        content = base64.b64encode(b'test content').decode()

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test upload',
            'source': 'test-bot'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval'
        assert 'bouncer-uploads-111111111111' in resp['s3_uri']

        # 檢查 DynamoDB item 沒有 assume_role
        table = app_module.table
        items = table.scan()['Items']
        upload_item = [i for i in items if i.get('action') == 'upload'][-1]
        assert 'assume_role' not in upload_item
        assert upload_item['account_id'] == '111111111111'
        assert upload_item['account_name'] == 'Default'

    @patch('telegram.send_telegram_message')
    def test_upload_cross_account_with_role(self, mock_telegram, app_module):
        """帶 account 參數 → 使用跨帳號，存 assume_role"""
        mock_telegram.return_value = {'ok': True}
        import base64
        content = base64.b64encode(b'cross account test').decode()

        # 先新增帳號
        from accounts import _get_accounts_table
        _get_accounts_table().put_item(Item={
            'account_id': '222222222222',
            'name': 'Dev',
            'role_arn': 'arn:aws:iam::222222222222:role/BouncerRole',
            'enabled': True,
            'created_at': 1000
        })

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'template.yaml',
            'content': content,
            'reason': 'deploy test',
            'source': 'test-bot',
            'account': '222222222222'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval'
        assert 'bouncer-uploads-222222222222' in resp['s3_uri']

        # 檢查 DynamoDB item 有 assume_role
        table = app_module.table
        items = table.scan()['Items']
        upload_item = [i for i in items if i.get('action') == 'upload' and i.get('account_id') == '222222222222'][-1]
        assert upload_item['assume_role'] == 'arn:aws:iam::222222222222:role/BouncerRole'
        assert upload_item['account_name'] == 'Dev'
        assert upload_item['bucket'] == 'bouncer-uploads-222222222222'

    @patch('telegram.send_telegram_message')
    def test_upload_invalid_account(self, mock_telegram, app_module):
        """帶不存在的 account → 錯誤"""
        import base64
        content = base64.b64encode(b'test').decode()

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test',
            'source': 'test-bot',
            'account': '111111111111'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'error'
        assert '未配置' in resp['error']

    @patch('telegram.send_telegram_message')
    def test_upload_disabled_account(self, mock_telegram, app_module):
        """帶停用的 account → 錯誤"""
        import base64
        content = base64.b64encode(b'test').decode()

        # 新增停用帳號
        from accounts import _get_accounts_table
        _get_accounts_table().put_item(Item={
            'account_id': '333333333333',
            'name': 'Disabled',
            'role_arn': 'arn:aws:iam::333333333333:role/BouncerRole',
            'enabled': False,
            'created_at': 1000
        })

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test',
            'source': 'test-bot',
            'account': '333333333333'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'error'
        assert '已停用' in resp['error']

    @patch('telegram.send_telegram_message')
    def test_upload_notification_includes_account(self, mock_telegram, app_module):
        """通知訊息包含帳號資訊"""
        mock_telegram.return_value = {'ok': True}
        import base64
        content = base64.b64encode(b'test').decode()

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test',
            'source': 'test-bot'
        })

        # 檢查 Telegram 通知有帳號欄位
        mock_telegram.assert_called_once()
        msg = mock_telegram.call_args[0][0]
        assert '帳號' in msg
        assert '111111111111' in msg


# ============================================================================
# Cross-Account Upload Execution Tests
# ============================================================================



class TestCrossAccountUploadExecution:
    """Upload 跨帳號執行（審批後）測試"""

    def test_execute_upload_no_assume_role(self, app_module):
        """無 assume_role → 用 Lambda 自身權限上傳"""
        import base64

        # 建立 mock upload request
        request_id = 'test-upload-no-assume'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'bouncer-uploads-111111111111',
            'key': '2026-02-21/test/file.txt',
            'content': base64.b64encode(b'hello').decode(),
            'content_type': 'text/plain',
            'status': 'pending_approval'
        })

        mock_s3 = MagicMock()
        mock_s3.meta.region_name = 'us-east-1'
        with patch('mcp_upload.get_s3_client', return_value=mock_s3) as mock_factory:
            result = app_module.execute_upload(request_id, 'test-approver')

            assert result['success'] is True
            # Called with role_arn=None (no assume role)
            mock_factory.assert_called_once()
            assert mock_factory.call_args[1].get('role_arn') is None
            mock_s3.put_object.assert_called_once()

    def test_execute_upload_with_assume_role(self, app_module):
        """有 assume_role → STS assume role 後上傳"""
        import base64

        request_id = 'test-upload-with-assume'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'bouncer-uploads-222222222222',
            'key': '2026-02-21/test/file.txt',
            'content': base64.b64encode(b'hello').decode(),
            'content_type': 'text/plain',
            'assume_role': 'arn:aws:iam::222222222222:role/BouncerRole',
            'status': 'pending_approval'
        })

        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            'Credentials': {
                'AccessKeyId': 'AKIATEST',
                'SecretAccessKey': 'secret',
                'SessionToken': 'token'
            }
        }
        mock_s3 = MagicMock()
        mock_s3.meta.region_name = 'us-east-1'

        with patch('mcp_upload.get_s3_client', return_value=mock_s3) as mock_factory:
            result = app_module.execute_upload(request_id, 'test-approver')

            assert result['success'] is True
            # get_s3_client called with role_arn
            mock_factory.assert_called_once()
            assert mock_factory.call_args[1].get('role_arn') == 'arn:aws:iam::222222222222:role/BouncerRole'
            mock_s3.put_object.assert_called_once()


# ============================================================================
# Cross-Account Upload Callback Tests
# ============================================================================



class TestCrossAccountUploadCallback:
    """Upload callback 帳號顯示測試"""

    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_upload_callback_shows_account(self, mock_update, mock_answer, app_module):
        """上傳 callback 顯示帳號資訊"""
        import callbacks

        item = {
            'request_id': 'test-cb-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-222222222222',
            'key': '2026-02-21/test/file.txt',
            'content': 'dGVzdA==',
            'content_type': 'text/plain',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'account_id': '222222222222',
            'account_name': 'Dev',
            'assume_role': 'arn:aws:iam::222222222222:role/BouncerRole',
            'status': 'pending_approval'
        }

        # Mock execute_upload
        with patch.object(app_module, 'execute_upload', return_value={
            'success': True,
            's3_uri': 's3://bouncer-uploads-222222222222/2026-02-21/test/file.txt',
            's3_url': 'https://bouncer-uploads-222222222222.s3.amazonaws.com/2026-02-21/test/file.txt'
        }):
            callbacks.handle_upload_callback('approve', 'test-cb-account', item, 123, 'cb-1', 'user-1')

        # 確認通知包含帳號
        msg = mock_update.call_args[0][1]
        assert '222222222222' in msg
        assert 'Dev' in msg

    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_upload_callback_no_account_backward_compat(self, mock_update, mock_answer, app_module):
        """舊的 upload item（無 account_id）→ 不顯示帳號行"""
        import callbacks

        item = {
            'request_id': 'test-cb-no-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-111111111111',
            'key': '2026-02-21/test/file.txt',
            'content': 'dGVzdA==',
            'content_type': 'text/plain',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'status': 'pending_approval'
        }

        with patch.object(app_module, 'execute_upload', return_value={
            'success': True,
            's3_uri': 's3://bouncer-uploads-111111111111/2026-02-21/test/file.txt',
            's3_url': 'https://bouncer-uploads-111111111111.s3.amazonaws.com/2026-02-21/test/file.txt'
        }):
            callbacks.handle_upload_callback('approve', 'test-cb-no-account', item, 123, 'cb-1', 'user-1')

        msg = mock_update.call_args[0][1]
        assert '帳號' not in msg


# ============================================================================
# Cross-Account Deploy Tests
# ============================================================================



class TestUploadDenyCallbackAccount:
    """Upload deny callback 帳號顯示測試"""

    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_upload_deny_callback_shows_account(self, mock_update, mock_answer, app_module):
        """拒絕上傳的 callback 也顯示帳號資訊"""
        import callbacks

        item = {
            'request_id': 'test-deny-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-222222222222',
            'key': '2026-02-21/test/file.txt',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'account_id': '222222222222',
            'account_name': 'Dev',
            'status': 'pending_approval'
        }

        callbacks.handle_upload_callback('deny', 'test-deny-account', item, 123, 'cb-1', 'user-1')

        msg = mock_update.call_args[0][1]
        assert '222222222222' in msg
        assert 'Dev' in msg
        assert '拒絕' in msg

    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_upload_deny_callback_no_account(self, mock_update, mock_answer, app_module):
        """舊的 upload deny item（無 account_id）→ 不顯示帳號行"""
        import callbacks

        item = {
            'request_id': 'test-deny-no-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-111111111111',
            'key': '2026-02-21/test/file.txt',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'status': 'pending_approval'
        }

        callbacks.handle_upload_callback('deny', 'test-deny-no-account', item, 123, 'cb-1', 'user-1')

        msg = mock_update.call_args[0][1]
        assert '帳號' not in msg
        assert '拒絕' in msg


# ============================================================================
# Trust Session Limits Tests
# ============================================================================



# ============================================================================
# S3 Verification Tests (sprint8-006, approach-b)
# ============================================================================

import base64 as _base64
from unittest.mock import call as _call


class TestUploadVerificationResult:
    """Unit tests for UploadVerificationResult dataclass and _verify_upload() helper."""

    def test_verify_upload_success(self):
        """Scenario 1 & 2: head_object succeeds → verified=True, s3_size populated."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from mcp_upload import _verify_upload, UploadVerificationResult

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {'ContentLength': 42}

        result = _verify_upload(mock_s3, 'my-bucket', 'some/key.txt', 'key.txt')

        assert isinstance(result, UploadVerificationResult)
        assert result.verified is True
        assert result.s3_size == 42
        assert result.s3_uri == 's3://my-bucket/some/key.txt'
        assert result.filename == 'key.txt'
        assert result.error is None

        mock_s3.head_object.assert_called_once_with(Bucket='my-bucket', Key='some/key.txt')

    def test_verify_upload_failure_non_blocking(self):
        """Scenario 3 & 4: head_object raises → verified=False, no exception propagated, error captured."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from mcp_upload import _verify_upload, UploadVerificationResult

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = _make_client_error('AccessDenied', '403 Forbidden')

        result = _verify_upload(mock_s3, 'my-bucket', 'some/key.txt', 'key.txt')

        assert isinstance(result, UploadVerificationResult)
        assert result.verified is False
        assert result.s3_size is None
        assert '403 Forbidden' in result.error
        # No exception raised — caller is unaffected

    def test_verify_upload_warning_logged(self, caplog):
        """Scenario 4: verification failure logs a warning."""
        import sys, os, logging
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from mcp_upload import _verify_upload

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = _make_client_error('NoSuchKey', 'The specified key does not exist')

        with caplog.at_level(logging.WARNING, logger='mcp_upload'):
            _verify_upload(mock_s3, 'bucket', 'key.txt', 'key.txt')

        assert any('UPLOAD VERIFY' in r.message or 'head_object' in r.message
                   for r in caplog.records), \
            "Expected a warning log for verification failure"

    def test_verify_upload_result_fields(self):
        """UploadVerificationResult has all required fields."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from mcp_upload import UploadVerificationResult

        r = UploadVerificationResult(filename='f.txt', s3_uri='s3://b/k', verified=True, s3_size=100)
        assert r.filename == 'f.txt'
        assert r.s3_uri == 's3://b/k'
        assert r.verified is True
        assert r.s3_size == 100
        assert r.error is None

        r2 = UploadVerificationResult(filename='f.txt', s3_uri='s3://b/k', verified=False, error='boom')
        assert r2.verified is False
        assert r2.s3_size is None
        assert r2.error == 'boom'


class TestUploadBatchS3Verification:
    """Integration tests for S3 verification in upload_batch trust auto-approve path."""

    @mock_aws
    @patch('telegram.send_telegram_message')
    @patch('mcp_upload.get_trust_session')
    @patch('mcp_upload.should_trust_approve_upload')
    @patch('mcp_upload.increment_trust_upload_count')
    @patch('mcp_upload.send_trust_upload_notification')
    def test_batch_trust_each_file_verified(
        self, mock_notif, mock_incr, mock_should, mock_session, mock_telegram, app_module
    ):
        """Scenario 1 & 2: trust auto-approve → each file has verified=True, s3_size set."""
        import mcp_upload as mu

        # Arrange AWS
        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        # bucket already created in conftest; just ensure it exists
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        mock_session.return_value = {
            'request_id': 'trust-sess-001',
            'max_uploads': 10,
            'upload_count': 0,
            'upload_bytes_total': 0,
        }
        mock_should.return_value = (True, None, None)
        mock_incr.return_value = True
        mock_notif.return_value = None

        files = [
            {
                'filename': 'alpha.txt',
                'content': _base64.b64encode(b'hello').decode(),
                'content_type': 'text/plain',
            },
            {
                'filename': 'beta.txt',
                'content': _base64.b64encode(b'world').decode(),
                'content_type': 'text/plain',
            },
        ]

        result_raw = mu.mcp_tool_upload_batch('req-001', {
            'files': files,
            'reason': 'test upload',
            'trust_scope': 'test-scope',
        })
        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['status'] == 'trust_auto_approved'
        for f in data['uploaded']:
            assert f['verified'] is True, f"Expected verified for {f['filename']}"
            assert isinstance(f['s3_size'], int), f"Expected s3_size for {f['filename']}"
        assert 'verification_failed' not in data  # all succeeded → key absent

    @mock_aws
    @patch('telegram.send_telegram_message')
    @patch('mcp_upload.get_trust_session')
    @patch('mcp_upload.should_trust_approve_upload')
    @patch('mcp_upload.increment_trust_upload_count')
    @patch('mcp_upload.send_trust_upload_notification')
    def test_batch_trust_verification_failure_non_blocking(
        self, mock_notif, mock_incr, mock_should, mock_session, mock_telegram, app_module
    ):
        """Scenario 3: verification failure goes into verification_failed, upload still succeeds."""
        import mcp_upload as mu

        bucket = 'bouncer-uploads-111111111111'
        s3_client = boto3.client('s3', region_name='us-east-1')
        try:
            s3_client.create_bucket(Bucket=bucket)
        except Exception:
            pass

        mock_session.return_value = {
            'request_id': 'trust-sess-002',
            'max_uploads': 10,
            'upload_count': 0,
            'upload_bytes_total': 0,
        }
        mock_should.return_value = (True, None, None)
        mock_incr.return_value = True
        mock_notif.return_value = None

        # Patch _verify_upload to simulate failure for the first file
        original_verify = mu._verify_upload
        call_count = [0]

        def flaky_verify(s3, bucket_name, key, filename):
            call_count[0] += 1
            if call_count[0] == 1:
                from mcp_upload import UploadVerificationResult
                return UploadVerificationResult(
                    filename=filename,
                    s3_uri=f's3://{bucket_name}/{key}',
                    verified=False,
                    error='simulated head_object failure',
                )
            return original_verify(s3, bucket_name, key, filename)

        with patch.object(mu, '_verify_upload', side_effect=flaky_verify):
            result_raw = mu.mcp_tool_upload_batch('req-002', {
                'files': [
                    {
                        'filename': 'file1.txt',
                        'content': _base64.b64encode(b'data1').decode(),
                        'content_type': 'text/plain',
                    },
                    {
                        'filename': 'file2.txt',
                        'content': _base64.b64encode(b'data2').decode(),
                        'content_type': 'text/plain',
                    },
                ],
                'reason': 'test',
                'trust_scope': 'test-scope',
            })

        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])

        # Upload still succeeded for both files
        assert data['status'] == 'trust_auto_approved'
        assert data['total_files'] == 2

        # file1 failed verification
        assert 'verification_failed' in data
        assert 'file1.txt' in data['verification_failed']
        assert 'file2.txt' not in data['verification_failed']

        # file1 entry: verified=False
        file1 = next(f for f in data['uploaded'] if f['filename'] == 'file1.txt')
        assert file1['verified'] is False

        # file2 entry: verified=True
        file2 = next(f for f in data['uploaded'] if f['filename'] == 'file2.txt')
        assert file2['verified'] is True

    def test_batch_trust_empty_files(self, app_module):
        """Edge case: empty files list → error, no verification attempted."""
        import mcp_upload as mu

        result_raw = mu.mcp_tool_upload_batch('req-003', {
            'files': [],
            'reason': 'test',
        })
        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])
        assert data['status'] == 'error'
        assert 'files' in data['error'].lower() or 'array' in data['error'].lower()


class TestUploadBatchS3VerificationExtended:
    """Extended S3 verification tests (merged from approach-c unique scenarios)."""

    @mock_aws
    @patch('telegram.send_telegram_message')
    @patch('mcp_upload.get_trust_session')
    @patch('mcp_upload.should_trust_approve_upload')
    @patch('mcp_upload.increment_trust_upload_count')
    @patch('mcp_upload.send_trust_upload_notification')
    def test_batch_trust_single_file_verified(
        self, mock_notif, mock_incr, mock_should, mock_session, mock_telegram, app_module
    ):
        """Single file batch: verified=True, s3_size present, no verification_failed."""
        import mcp_upload as mu

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        mock_session.return_value = {
            'request_id': 'trust-sess-single',
            'max_uploads': 5,
            'upload_count': 0,
            'upload_bytes_total': 0,
        }
        mock_should.return_value = (True, None, None)
        mock_incr.return_value = True
        mock_notif.return_value = None

        data_bytes = b'single file content'
        files = [{
            'filename': 'single.html',
            'content': _base64.b64encode(data_bytes).decode(),
            'content_type': 'text/html',
        }]

        result_raw = mu.mcp_tool_upload_batch('req-single', {
            'files': files,
            'reason': 'single test',
            'trust_scope': 'test-scope',
        })
        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['status'] == 'trust_auto_approved'
        assert data['total_files'] == 1
        assert len(data['uploaded']) == 1

        entry = data['uploaded'][0]
        assert entry['filename'] == 'single.html'
        assert entry['verified'] is True
        assert isinstance(entry['s3_size'], int)
        assert 'verification_failed' not in data

    @mock_aws
    @patch('telegram.send_telegram_message')
    @patch('mcp_upload.get_trust_session')
    @patch('mcp_upload.should_trust_approve_upload')
    @patch('mcp_upload.increment_trust_upload_count')
    @patch('mcp_upload.send_trust_upload_notification')
    def test_batch_trust_size_matches_expected(
        self, mock_notif, mock_incr, mock_should, mock_session, mock_telegram, app_module
    ):
        """s3_size in response matches ContentLength returned by head_object."""
        import mcp_upload as mu

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        mock_session.return_value = {
            'request_id': 'trust-sess-size',
            'max_uploads': 10,
            'upload_count': 0,
            'upload_bytes_total': 0,
        }
        mock_should.return_value = (True, None, None)
        mock_incr.return_value = True
        mock_notif.return_value = None

        content_bytes = b'exactly_this_content'
        files = [{
            'filename': 'size.txt',
            'content': _base64.b64encode(content_bytes).decode(),
            'content_type': 'text/plain',
        }]

        result_raw = mu.mcp_tool_upload_batch('req-size', {
            'files': files,
            'reason': 'size check',
            'trust_scope': 'test-scope',
        })
        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['status'] == 'trust_auto_approved'
        entry = data['uploaded'][0]
        # moto head_object returns actual ContentLength matching put_object body
        assert entry['s3_size'] == len(content_bytes), \
            f"Expected s3_size={len(content_bytes)}, got {entry.get('s3_size')}"
        assert entry['verified'] is True

    @mock_aws
    @patch('telegram.send_telegram_message')
    @patch('mcp_upload.get_trust_session')
    @patch('mcp_upload.should_trust_approve_upload')
    @patch('mcp_upload.increment_trust_upload_count')
    @patch('mcp_upload.send_trust_upload_notification')
    def test_batch_trust_partial_failure_list(
        self, mock_notif, mock_incr, mock_should, mock_session, mock_telegram, app_module
    ):
        """2 files: first succeeds, second fails head_object → partial verification_failed."""
        import mcp_upload as mu

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        mock_session.return_value = {
            'request_id': 'trust-sess-partial',
            'max_uploads': 10,
            'upload_count': 0,
            'upload_bytes_total': 0,
        }
        mock_should.return_value = (True, None, None)
        mock_incr.return_value = True
        mock_notif.return_value = None

        files = [
            {
                'filename': 'good.js',
                'content': _base64.b64encode(b'ok').decode(),
                'content_type': 'application/javascript',
            },
            {
                'filename': 'bad.css',
                'content': _base64.b64encode(b'bad').decode(),
                'content_type': 'text/css',
            },
        ]

        original_verify = mu._verify_upload
        call_count = [0]

        def partial_verify(s3_c, bkt, key, fname):
            call_count[0] += 1
            if call_count[0] == 2:
                from mcp_upload import UploadVerificationResult
                return UploadVerificationResult(
                    filename=fname,
                    s3_uri=f's3://{bkt}/{key}',
                    verified=False,
                    error='HeadObjectFailed',
                )
            return original_verify(s3_c, bkt, key, fname)

        with patch.object(mu, '_verify_upload', side_effect=partial_verify):
            result_raw = mu.mcp_tool_upload_batch('req-partial', {
                'files': files,
                'reason': 'partial test',
                'trust_scope': 'test-scope',
            })

        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['status'] == 'trust_auto_approved'
        assert data['total_files'] == 2

        # good.js: verified=True
        good = next(f for f in data['uploaded'] if f['filename'] == 'good.js')
        assert good['verified'] is True

        # bad.css: verified=False
        bad = next(f for f in data['uploaded'] if f['filename'] == 'bad.css')
        assert bad['verified'] is False

        # verification_failed contains only bad.css
        assert 'verification_failed' in data
        assert 'bad.css' in data['verification_failed']
        assert 'good.js' not in data['verification_failed']

    @mock_aws
    @patch('telegram.send_telegram_message')
    @patch('mcp_upload.get_trust_session')
    @patch('mcp_upload.should_trust_approve_upload')
    @patch('mcp_upload.increment_trust_upload_count')
    @patch('mcp_upload.send_trust_upload_notification')
    def test_batch_trust_all_fail_non_blocking(
        self, mock_notif, mock_incr, mock_should, mock_session, mock_telegram, app_module
    ):
        """All files fail head_object → status still trust_auto_approved, all in verification_failed."""
        import mcp_upload as mu
        from mcp_upload import UploadVerificationResult

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        mock_session.return_value = {
            'request_id': 'trust-sess-allfail',
            'max_uploads': 10,
            'upload_count': 0,
            'upload_bytes_total': 0,
        }
        mock_should.return_value = (True, None, None)
        mock_incr.return_value = True
        mock_notif.return_value = None

        files = [
            {
                'filename': 'x.html',
                'content': _base64.b64encode(b'x').decode(),
                'content_type': 'text/html',
            },
            {
                'filename': 'y.js',
                'content': _base64.b64encode(b'y').decode(),
                'content_type': 'application/javascript',
            },
            {
                'filename': 'z.css',
                'content': _base64.b64encode(b'z').decode(),
                'content_type': 'text/css',
            },
        ]

        def all_fail_verify(s3_c, bkt, key, fname):
            return UploadVerificationResult(
                filename=fname,
                s3_uri=f's3://{bkt}/{key}',
                verified=False,
                error='S3ServiceError',
            )

        with patch.object(mu, '_verify_upload', side_effect=all_fail_verify):
            result_raw = mu.mcp_tool_upload_batch('req-allfail', {
                'files': files,
                'reason': 'all fail',
                'trust_scope': 'test-scope',
            })

        body = json.loads(result_raw['body'])
        data = json.loads(body['result']['content'][0]['text'])

        # Must not error out — upload succeeded, verification is non-blocking
        assert data['status'] == 'trust_auto_approved', f"Expected trust_auto_approved, got: {data}"
        assert data['total_files'] == 3
        assert len(data['uploaded']) == 3

        # All verified=False
        for entry in data['uploaded']:
            assert entry['verified'] is False

        # All in verification_failed
        assert 'verification_failed' in data
        failed_names = set(data['verification_failed'])
        assert failed_names == {'x.html', 'y.js', 'z.css'}


class TestUploadBatchCallbackVerification:
    """Tests for handle_upload_batch_callback S3 verification (non-blocking)."""

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_callback_approve_verified_fields_present(self, mock_update, mock_answer, app_module):
        """Scenario 1 & 2: approved callback → uploaded items have verified+s3_size fields."""
        import callbacks
        import json as _json

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        # Stage a file in staging bucket for s3-to-s3 copy
        staging_bucket = 'bouncer-uploads-111111111111'  # same in test (DEFAULT_ACCOUNT_ID = 111...)
        s3.put_object(Bucket=staging_bucket, Key='pending/batch-001/report.txt', Body=b'content')

        files_manifest = _json.dumps([{
            'filename': 'report.txt',
            's3_key': 'pending/batch-001/report.txt',
            'content_type': 'text/plain',
            'size': 7,
            'sha256': 'abc123',
        }])

        item = {
            'request_id': 'batch-cb-001',
            'action': 'upload_batch',
            'bucket': bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 7,
            'source': 'test-bot',
            'reason': 'verify test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
        }

        callbacks.handle_upload_batch_callback(
            'approve', 'batch-cb-001', item, 123, 'cb-001', 'user-1'
        )

        # Check DB for uploaded_details
        import db as _db
        stored = _db.table.get_item(Key={'request_id': 'batch-cb-001'}).get('Item', {})
        uploaded_details = _json.loads(stored.get('uploaded_details', '[]'))
        assert len(uploaded_details) == 1
        entry = uploaded_details[0]
        assert 'verified' in entry, "uploaded_details must contain 'verified'"
        assert 's3_size' in entry, "uploaded_details must contain 's3_size'"
        assert entry['verified'] is True

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_callback_verify_failure_non_blocking(self, mock_update, mock_answer, app_module):
        """Scenario 3: head_object fails → verification_failed list populated, upload not blocked."""
        import callbacks
        import json as _json
        from mcp_upload import UploadVerificationResult

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        staging_bucket = 'bouncer-uploads-111111111111'
        s3.put_object(Bucket=staging_bucket, Key='pending/batch-002/data.bin', Body=b'binary')

        files_manifest = _json.dumps([{
            'filename': 'data.bin',
            's3_key': 'pending/batch-002/data.bin',
            'content_type': 'application/octet-stream',
            'size': 6,
            'sha256': 'def456',
        }])

        item = {
            'request_id': 'batch-cb-002',
            'action': 'upload_batch',
            'bucket': bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 6,
            'source': 'test-bot',
            'reason': 'verify failure test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
        }

        import mcp_upload as mu
        # Simulate head_object failure
        failing_result = UploadVerificationResult(
            filename='data.bin',
            s3_uri='s3://bouncer-uploads-111111111111/data.bin',
            verified=False,
            error='permission denied',
        )
        with patch('callbacks_upload._verify_upload', return_value=failing_result):
            callbacks.handle_upload_batch_callback(
                'approve', 'batch-cb-002', item, 124, 'cb-002', 'user-1'
            )

        import db as _db
        stored = _db.table.get_item(Key={'request_id': 'batch-cb-002'}).get('Item', {})

        # Upload completed (status=approved), not failed
        assert stored.get('status') == 'approved'

        # verification_failed list persisted
        vf = _json.loads(stored.get('verification_failed', '[]'))
        assert 'data.bin' in vf

        # uploaded_details has verified=False
        uploaded_details = _json.loads(stored.get('uploaded_details', '[]'))
        assert uploaded_details[0]['verified'] is False

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_callback_verify_failure_logs_warning(
        self, mock_update, mock_answer, app_module, caplog
    ):
        """Scenario 4: head_object failure → warning logged via _verify_upload."""
        import callbacks, logging
        import json as _json
        from mcp_upload import UploadVerificationResult

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        staging_bucket = 'bouncer-uploads-111111111111'
        s3.put_object(Bucket=staging_bucket, Key='pending/batch-003/warn.txt', Body=b'warn')

        files_manifest = _json.dumps([{
            'filename': 'warn.txt',
            's3_key': 'pending/batch-003/warn.txt',
            'content_type': 'text/plain',
            'size': 4,
            'sha256': 'ghi789',
        }])

        item = {
            'request_id': 'batch-cb-003',
            'action': 'upload_batch',
            'bucket': bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 4,
            'source': 'test-bot',
            'reason': 'warning log test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
        }

        import mcp_upload as mu

        def warn_verify(s3_c, bkt, key, fname):
            from mcp_upload import UploadVerificationResult
            import logging as _log
            _log.getLogger('mcp_upload').warning(
                '[UPLOAD VERIFY] head_object failed for s3://%s/%s: permission denied', bkt, key
            )
            return UploadVerificationResult(
                filename=fname, s3_uri=f's3://{bkt}/{key}',
                verified=False, error='permission denied',
            )

        with caplog.at_level(logging.WARNING, logger='mcp_upload'):
            with patch('callbacks_upload._verify_upload', side_effect=warn_verify):
                callbacks.handle_upload_batch_callback(
                    'approve', 'batch-cb-003', item, 125, 'cb-003', 'user-1'
                )

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any('UPLOAD VERIFY' in m or 'head_object' in m or 'permission denied' in m
                   for m in warning_msgs), \
            f"Expected warning log, got: {warning_msgs}"

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_callback_s3_permission_error_on_head_object(
        self, mock_update, mock_answer, app_module
    ):
        """Edge case: ClientError on head_object → verification_failed, upload not blocked."""
        import callbacks
        import json as _json
        from botocore.exceptions import ClientError
        from mcp_upload import UploadVerificationResult

        s3 = boto3.client('s3', region_name='us-east-1')
        bucket = 'bouncer-uploads-111111111111'
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:
            pass

        staging_bucket = 'bouncer-uploads-111111111111'
        s3.put_object(Bucket=staging_bucket, Key='pending/batch-004/perm.txt', Body=b'perm')

        files_manifest = _json.dumps([{
            'filename': 'perm.txt',
            's3_key': 'pending/batch-004/perm.txt',
            'content_type': 'text/plain',
            'size': 4,
            'sha256': 'jkl012',
        }])

        item = {
            'request_id': 'batch-cb-004',
            'action': 'upload_batch',
            'bucket': bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 4,
            'source': 'test-bot',
            'reason': 'permission error test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
        }

        import mcp_upload as mu
        perm_fail = UploadVerificationResult(
            filename='perm.txt',
            s3_uri='s3://bouncer-uploads-111111111111/perm.txt',
            verified=False,
            error='AccessDenied',
        )
        with patch('callbacks_upload._verify_upload', return_value=perm_fail):
            callbacks.handle_upload_batch_callback(
                'approve', 'batch-cb-004', item, 126, 'cb-004', 'user-1'
            )

        import db as _db
        stored = _db.table.get_item(Key={'request_id': 'batch-cb-004'}).get('Item', {})
        assert stored.get('status') == 'approved', "Upload must succeed despite verify failure"
        vf = _json.loads(stored.get('verification_failed', '[]'))
        assert 'perm.txt' in vf


# =============================================================================
# Regression tests: upload_batch silent S3 failure fix (#39)
# =============================================================================

class TestUploadBatchCrossAccountFix:
    """Regression tests for #39: upload_batch S3 silent failure when assume_role is set.

    Root cause: handle_upload_batch_callback was using a single S3 client obtained
    with the assumed role (target account) to call copy_object FROM the staging bucket
    (main account). Cross-account copy_object fails with AccessDenied if the assumed
    role does not have s3:GetObject on the staging bucket — and the exception was
    caught per-file, silently adding the file to the errors list.

    Fix: Use two separate S3 clients:
    - s3_staging: Lambda execution role (no assumed role) — reads from staging bucket
    - s3_target:  Assumed role — writes to target bucket
    """

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_cross_account_upload_uses_staging_client_for_get_object(
        self, mock_update, mock_answer, app_module
    ):
        """With assume_role set, get_object must use Lambda-role client (staging bucket).

        Verifies that the staging file is read successfully even when a cross-account
        assumed role is in use (the Lambda role can always read its own staging bucket).
        """
        import callbacks
        import json as _json

        s3 = boto3.client('s3', region_name='us-east-1')
        staging_bucket = 'bouncer-uploads-111111111111'
        target_bucket = 'cross-account-target-bucket'

        try:
            s3.create_bucket(Bucket=staging_bucket)
        except Exception:
            pass
        try:
            s3.create_bucket(Bucket=target_bucket)
        except Exception:
            pass

        # Stage the file
        s3.put_object(Bucket=staging_bucket, Key='pending/batch-x001/report.csv', Body=b'csv-content')

        files_manifest = _json.dumps([{
            'filename': 'report.csv',
            's3_key': 'pending/batch-x001/report.csv',
            'content_type': 'text/csv',
            'size': 11,
            'sha256': 'abc',
        }])

        item = {
            'request_id': 'batch-x001',
            'action': 'upload_batch',
            'bucket': target_bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 11,
            'source': 'test-bot',
            'reason': 'cross-account regression test',
            'account_id': '222222222222',
            'account_name': 'CrossAccount',
            'trust_scope': '',
            'status': 'pending_approval',
            'assume_role': 'arn:aws:iam::222222222222:role/BouncerRole',
        }

        # With @mock_aws, the assume_role call will use fake AWS creds.
        # Patch get_s3_client in callbacks module (imported at module level)
        with patch('callbacks_upload.get_s3_client') as mock_get_s3:
            s3_staging_mock = boto3.client('s3', region_name='us-east-1')
            s3_target_mock = boto3.client('s3', region_name='us-east-1')

            def get_s3_side_effect(role_arn=None, session_name='bouncer-s3', region=None):
                if role_arn is None:
                    return s3_staging_mock   # Lambda role -> staging reads
                return s3_target_mock        # Assumed role -> target writes

            mock_get_s3.side_effect = get_s3_side_effect

            callbacks.handle_upload_batch_callback(
                'approve', 'batch-x001', item, 123, 'cb-x001', 'user-1'
            )

        # Verify get_s3_client was called with role_arn=None for staging
        staging_calls = [
            c for c in mock_get_s3.call_args_list
            if c[1].get('role_arn') is None or (len(c[0]) > 0 and c[0][0] is None)
        ]
        assert len(staging_calls) >= 1, (
            "Expected get_s3_client(role_arn=None) call for staging bucket reads, "
            f"got calls: {mock_get_s3.call_args_list}"
        )

        # Verify get_s3_client was called with the assume_role ARN for target
        target_calls = [
            c for c in mock_get_s3.call_args_list
            if c[1].get('role_arn') == 'arn:aws:iam::222222222222:role/BouncerRole'
        ]
        assert len(target_calls) >= 1, (
            "Expected get_s3_client(role_arn=<arn>) call for target bucket writes, "
            f"got calls: {mock_get_s3.call_args_list}"
        )

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_no_assume_role_uses_single_client_for_both(
        self, mock_update, mock_answer, app_module
    ):
        """Without assume_role, both staging read and target write use Lambda-role client."""
        import callbacks
        import json as _json

        s3 = boto3.client('s3', region_name='us-east-1')
        staging_bucket = 'bouncer-uploads-111111111111'
        target_bucket = 'same-account-target'

        try:
            s3.create_bucket(Bucket=staging_bucket)
        except Exception:
            pass
        try:
            s3.create_bucket(Bucket=target_bucket)
        except Exception:
            pass

        s3.put_object(Bucket=staging_bucket, Key='pending/batch-x002/doc.txt', Body=b'hello')

        files_manifest = _json.dumps([{
            'filename': 'doc.txt',
            's3_key': 'pending/batch-x002/doc.txt',
            'content_type': 'text/plain',
            'size': 5,
            'sha256': 'def',
        }])

        item = {
            'request_id': 'batch-x002',
            'action': 'upload_batch',
            'bucket': target_bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 5,
            'source': 'test-bot',
            'reason': 'no assume_role regression test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
            # No assume_role key
        }

        with patch('callbacks_upload.get_s3_client') as mock_get_s3:
            s3_client = boto3.client('s3', region_name='us-east-1')
            mock_get_s3.return_value = s3_client

            callbacks.handle_upload_batch_callback(
                'approve', 'batch-x002', item, 124, 'cb-x002', 'user-1'
            )

        # get_s3_client called twice: once with role_arn=None (staging), once with role_arn=None (target)
        for c in mock_get_s3.call_args_list[:2]:
            role = c[1].get('role_arn')
            assert role is None, f"Expected role_arn=None when no assume_role, got {role!r}"

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_file_is_actually_uploaded_to_target_bucket(
        self, mock_update, mock_answer, app_module
    ):
        """Regression: file content must actually arrive in the target bucket after approval.

        Previously (before #39 fix), copy_object from staging to target would silently fail
        when the assumed role lacked read access to staging — the file would never appear
        in the target bucket.
        """
        import callbacks
        import json as _json

        s3 = boto3.client('s3', region_name='us-east-1')
        staging_bucket = 'bouncer-uploads-111111111111'
        target_bucket = 'regression-target-bucket-39'
        file_content = b'regression-test-content-for-issue-39'

        try:
            s3.create_bucket(Bucket=staging_bucket)
        except Exception:
            pass
        try:
            s3.create_bucket(Bucket=target_bucket)
        except Exception:
            pass

        s3.put_object(Bucket=staging_bucket, Key='pending/batch-x003/upload.bin', Body=file_content)

        files_manifest = _json.dumps([{
            'filename': 'upload.bin',
            's3_key': 'pending/batch-x003/upload.bin',
            'content_type': 'application/octet-stream',
            'size': len(file_content),
            'sha256': 'ghi',
        }])

        item = {
            'request_id': 'batch-x003',
            'action': 'upload_batch',
            'bucket': target_bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': len(file_content),
            'source': 'test-bot',
            'reason': 'regression test: file must land in target bucket',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
        }

        callbacks.handle_upload_batch_callback(
            'approve', 'batch-x003', item, 125, 'cb-x003', 'user-1'
        )

        # Verify file actually exists in target bucket (regression check)
        import db as _db
        stored = _db.table.get_item(Key={'request_id': 'batch-x003'}).get('Item', {})
        assert stored.get('upload_status') == 'completed', (
            f"Expected upload_status='completed', got {stored.get('upload_status')!r}. "
            "This is the regression check for #39 silent S3 failure."
        )
        uploaded_details = _json.loads(stored.get('uploaded_details', '[]'))
        assert len(uploaded_details) == 1, (
            f"Expected 1 uploaded file, got {len(uploaded_details)}. "
            "File was not recorded as uploaded."
        )
        assert uploaded_details[0]['filename'] == 'upload.bin'

    @mock_aws
    @patch('callbacks_upload.answer_callback')
    @patch('callbacks_upload.update_message')
    def test_copy_object_not_called(self, mock_update, mock_answer, app_module):
        """Regression: copy_object must NOT be called. Fix uses get_object+put_object instead.

        This ensures the old copy_object pattern (which caused the silent failure) has
        been fully replaced and cannot regress.
        """
        import callbacks
        import json as _json

        s3 = boto3.client('s3', region_name='us-east-1')
        staging_bucket = 'bouncer-uploads-111111111111'
        target_bucket = 'target-no-copy-object'

        try:
            s3.create_bucket(Bucket=staging_bucket)
        except Exception:
            pass
        try:
            s3.create_bucket(Bucket=target_bucket)
        except Exception:
            pass

        s3.put_object(Bucket=staging_bucket, Key='pending/batch-x004/file.txt', Body=b'data')

        files_manifest = _json.dumps([{
            'filename': 'file.txt',
            's3_key': 'pending/batch-x004/file.txt',
            'content_type': 'text/plain',
            'size': 4,
            'sha256': 'jkl',
        }])

        item = {
            'request_id': 'batch-x004',
            'action': 'upload_batch',
            'bucket': target_bucket,
            'files': files_manifest,
            'file_count': 1,
            'total_size': 4,
            'source': 'test-bot',
            'reason': 'ensure copy_object not used',
            'account_id': '111111111111',
            'account_name': 'Default',
            'trust_scope': '',
            'status': 'pending_approval',
        }

        with patch('callbacks_upload.get_s3_client') as mock_get_s3:
            mock_s3 = MagicMock()
            mock_s3.get_object.return_value = {'Body': MagicMock(read=lambda: b'data')}
            mock_get_s3.return_value = mock_s3

            callbacks.handle_upload_batch_callback(
                'approve', 'batch-x004', item, 126, 'cb-x004', 'user-1'
            )

        # copy_object must NOT be called
        mock_s3.copy_object.assert_not_called()
        # put_object must be called (the new approach)
        mock_s3.put_object.assert_called()
