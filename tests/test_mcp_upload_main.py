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
        import mcp_tools
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

        with patch('boto3.client') as mock_boto:
            mock_s3 = MagicMock()
            mock_s3.meta.region_name = 'us-east-1'
            mock_boto.return_value = mock_s3

            result = app_module.execute_upload(request_id, 'test-approver')

            assert result['success'] is True
            # Should NOT have called sts assume_role
            mock_boto.assert_called_once_with('s3')
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

        def mock_client(service, **kwargs):
            if service == 'sts':
                return mock_sts
            return mock_s3

        with patch('boto3.client', side_effect=mock_client):
            result = app_module.execute_upload(request_id, 'test-approver')

            assert result['success'] is True
            mock_sts.assume_role.assert_called_once_with(
                RoleArn='arn:aws:iam::222222222222:role/BouncerRole',
                RoleSessionName='bouncer-upload'
            )
            mock_s3.put_object.assert_called_once()


# ============================================================================
# Cross-Account Upload Callback Tests
# ============================================================================



class TestCrossAccountUploadCallback:
    """Upload callback 帳號顯示測試"""

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
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

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
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

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
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

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
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


