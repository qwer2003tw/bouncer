"""Tests for Trust Session Upload Support + Batch Upload."""
import base64
import hashlib
import json
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest
import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def _trust_id(scope, account='111111111111'):
    h = hashlib.sha256(scope.encode()).hexdigest()[:16]
    return f'trust-{h}-{account}'


@pytest.fixture
def mock_dynamodb():
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
        yield dynamodb


@pytest.fixture
def app_module(mock_dynamodb):
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['MCP_MAX_WAIT'] = '5'

    for mod in ['app', 'telegram', 'paging', 'trust', 'commands', 'notifications', 'db',
                'callbacks', 'mcp_tools', 'accounts', 'rate_limit', 'smart_approval',
                'tool_schema', 'constants', 'grant', 'risk_scorer',
                'src.app', 'src.telegram', 'src.trust']:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
    import app
    import db
    import trust

    table = mock_dynamodb.Table('clawdbot-approval-requests')
    app.table = table
    app.dynamodb = mock_dynamodb
    db.table = table

    class Module:
        pass
    m = Module()
    m.table = table
    m.trust = trust
    m.app = app
    yield m

    sys.path.pop(0)


def _create_trust_session(table, scope, account='111111111111', max_uploads=5,
                          upload_count=0, upload_bytes_total=0, expires_delta=600,
                          max_commands=20):
    now = int(time.time())
    tid = _trust_id(scope, account)
    table.put_item(Item={
        'request_id': tid,
        'type': 'trust_session',
        'source': scope,
        'trust_scope': scope,
        'account_id': account,
        'approved_by': '999999999',
        'created_at': now,
        'expires_at': now + expires_delta,
        'command_count': 0,
        'max_commands': max_commands,
        'upload_count': upload_count,
        'upload_bytes_total': upload_bytes_total,
        'max_uploads': max_uploads,
        'ttl': now + 3600,
    })
    return tid


# ============================================================================
# Filename Safety
# ============================================================================

class TestUploadFilenameSafety:

    def test_safe_filenames(self, app_module):
        from trust import _is_upload_filename_safe
        assert _is_upload_filename_safe('config.json') is True
        assert _is_upload_filename_safe('my-file_v2.txt') is True
        assert _is_upload_filename_safe('index.html') is True

    def test_empty(self, app_module):
        from trust import _is_upload_filename_safe
        assert _is_upload_filename_safe('') is False

    def test_null_bytes(self, app_module):
        from trust import _is_upload_filename_safe
        assert _is_upload_filename_safe('file\x00.txt') is False

    def test_path_traversal(self, app_module):
        from trust import _is_upload_filename_safe
        assert _is_upload_filename_safe('../etc/passwd') is False

    def test_slash(self, app_module):
        from trust import _is_upload_filename_safe
        assert _is_upload_filename_safe('sub/file.txt') is False
        assert _is_upload_filename_safe('dir\\file.txt') is False


# ============================================================================
# Extension Blocking
# ============================================================================

class TestUploadExtensionBlocking:

    def test_blocked(self, app_module):
        from trust import _is_upload_extension_blocked
        for ext in ['.sh', '.exe', '.py', '.jar', '.zip', '.tar.gz']:
            assert _is_upload_extension_blocked(f'file{ext}') is True, f'{ext} should be blocked'

    def test_allowed(self, app_module):
        from trust import _is_upload_extension_blocked
        for ext in ['.css', '.js', '.html', '.json', '.png', '.jpg']:
            assert _is_upload_extension_blocked(f'file{ext}') is False, f'{ext} should be allowed'

    def test_case_insensitive(self, app_module):
        from trust import _is_upload_extension_blocked
        assert _is_upload_extension_blocked('SCRIPT.SH') is True


# ============================================================================
# should_trust_approve_upload
# ============================================================================

class TestShouldTrustApproveUpload:

    def test_basic_approve(self, app_module):
        _create_trust_session(app_module.table, 'upload-ok')
        ok, session, reason = app_module.trust.should_trust_approve_upload(
            'upload-ok', '111111111111', 'config.json', 1024
        )
        assert ok is True
        assert session is not None

    def test_no_trust_scope(self, app_module):
        ok, _, _ = app_module.trust.should_trust_approve_upload(
            '', '111111111111', 'config.json', 1024
        )
        assert ok is False

    def test_no_session(self, app_module):
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'nonexistent', '111111111111', 'config.json', 1024
        )
        assert ok is False
        assert 'no active' in reason.lower()

    def test_uploads_not_enabled(self, app_module):
        _create_trust_session(app_module.table, 'no-upload', max_uploads=0)
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'no-upload', '111111111111', 'config.json', 1024
        )
        assert ok is False
        assert 'not enabled' in reason.lower()

    def test_quota_exhausted(self, app_module):
        _create_trust_session(app_module.table, 'full-quota',
                              max_uploads=5, upload_count=5)
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'full-quota', '111111111111', 'config.json', 1024
        )
        assert ok is False
        assert 'exhausted' in reason.lower() or 'quota' in reason.lower()

    def test_file_too_large(self, app_module):
        from constants import TRUST_UPLOAD_MAX_BYTES_PER_FILE
        _create_trust_session(app_module.table, 'big-file')
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'big-file', '111111111111', 'config.json',
            TRUST_UPLOAD_MAX_BYTES_PER_FILE + 1
        )
        assert ok is False
        assert 'large' in reason.lower()

    def test_total_bytes_exceeded(self, app_module):
        from constants import TRUST_UPLOAD_MAX_BYTES_TOTAL
        _create_trust_session(app_module.table, 'total-full',
                              upload_bytes_total=TRUST_UPLOAD_MAX_BYTES_TOTAL - 10)
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'total-full', '111111111111', 'config.json', 200
        )
        assert ok is False
        assert 'exceed' in reason.lower()

    def test_blocked_extension(self, app_module):
        _create_trust_session(app_module.table, 'blocked-ext')
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'blocked-ext', '111111111111', 'script.sh', 100
        )
        assert ok is False
        assert 'blocked' in reason.lower()

    def test_unsafe_filename(self, app_module):
        _create_trust_session(app_module.table, 'unsafe-fn')
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'unsafe-fn', '111111111111', '../etc/passwd', 100
        )
        assert ok is False
        assert 'unsafe' in reason.lower()

    def test_expired_session(self, app_module):
        _create_trust_session(app_module.table, 'expired-up', expires_delta=-10)
        ok, _, _ = app_module.trust.should_trust_approve_upload(
            'expired-up', '111111111111', 'config.json', 100
        )
        assert ok is False


# ============================================================================
# increment_trust_upload_count (atomic)
# ============================================================================

class TestIncrementTrustUploadCount:

    def test_basic_increment(self, app_module):
        tid = _create_trust_session(app_module.table, 'inc-1')
        ok = app_module.trust.increment_trust_upload_count(tid, 1024)
        assert ok is True
        item = app_module.table.get_item(Key={'request_id': tid})['Item']
        assert int(item['upload_count']) == 1
        assert int(item['upload_bytes_total']) == 1024

    def test_multiple_increments(self, app_module):
        tid = _create_trust_session(app_module.table, 'inc-multi')
        app_module.trust.increment_trust_upload_count(tid, 100)
        app_module.trust.increment_trust_upload_count(tid, 200)
        ok = app_module.trust.increment_trust_upload_count(tid, 300)
        assert ok is True
        item = app_module.table.get_item(Key={'request_id': tid})['Item']
        assert int(item['upload_count']) == 3
        assert int(item['upload_bytes_total']) == 600

    def test_fails_at_quota(self, app_module):
        tid = _create_trust_session(app_module.table, 'inc-full',
                                     max_uploads=2, upload_count=2)
        ok = app_module.trust.increment_trust_upload_count(tid, 100)
        assert ok is False

    def test_bytes_exceeded_checked_in_should_approve(self, app_module):
        """Byte limit is enforced in should_trust_approve_upload, not in increment."""
        from constants import TRUST_UPLOAD_MAX_BYTES_TOTAL
        _create_trust_session(app_module.table, 'inc-bytes',
                              upload_bytes_total=TRUST_UPLOAD_MAX_BYTES_TOTAL - 10)
        # should_trust_approve_upload rejects before increment is called
        ok, _, reason = app_module.trust.should_trust_approve_upload(
            'inc-bytes', '111111111111', 'test.txt', 100
        )
        assert ok is False
        assert 'exceed' in reason.lower()


# ============================================================================
# _check_upload_trust passthrough
# ============================================================================

class TestCheckUploadTrust:

    def _make_ctx(self, trust_scope='', legacy_bucket=None, legacy_key=None):
        from mcp_tools import UploadContext
        return UploadContext(
            req_id='test', filename='test.txt', content_b64=base64.b64encode(b'x').decode(),
            content_type='text/plain', content_size=1, reason='test', source='test',
            sync_mode=False, legacy_bucket=legacy_bucket, legacy_key=legacy_key,
            account_id='111111111111', account_name='Default', assume_role=None,
            target_account_id='111111111111', trust_scope=trust_scope,
        )

    def test_no_trust_scope(self, app_module):
        from mcp_tools import _check_upload_trust
        ctx = self._make_ctx(trust_scope='')
        assert _check_upload_trust(ctx) is None

    def test_custom_s3_uri(self, app_module):
        from mcp_tools import _check_upload_trust
        ctx = self._make_ctx(trust_scope='test', legacy_bucket='custom-bucket',
                             legacy_key='custom-key')
        assert _check_upload_trust(ctx) is None


# ============================================================================
# Batch Upload Validation
# ============================================================================

class TestBatchUploadValidation:

    def _call(self, arguments, app_module):
        from mcp_tools import mcp_tool_upload_batch
        result = mcp_tool_upload_batch('test-req', arguments)
        # Handle Lambda-style response
        if isinstance(result, dict) and 'body' in result:
            body = json.loads(result['body'])
            content = body.get('result', {}).get('content', [])
            if content:
                return json.loads(content[0].get('text', '{}'))
        # Handle direct MCP result
        content = result.get('result', result).get('content', [])
        if content:
            return json.loads(content[0].get('text', '{}'))
        return result

    def test_empty_files(self, app_module):
        data = self._call({'files': [], 'reason': 't', 'source': 's'}, app_module)
        assert 'error' in str(data).lower()

    def test_too_many_files(self, app_module):
        files = [{'filename': f'f{i}.txt', 'content': base64.b64encode(b'x').decode()}
                 for i in range(51)]
        data = self._call({'files': files, 'reason': 't', 'source': 's'}, app_module)
        assert 'too many' in str(data).lower()

    def test_blocked_extension(self, app_module):
        files = [{'filename': 'evil.sh', 'content': base64.b64encode(b'#!/bin/bash').decode()}]
        data = self._call({'files': files, 'reason': 't', 'source': 's'}, app_module)
        assert 'blocked' in str(data).lower()

    def test_sanitizes_path_traversal(self, app_module):
        """Path traversal filenames are sanitized — '../passwd' becomes 'passwd' (safe)."""
        from mcp_tools import _sanitize_filename
        assert _sanitize_filename('../etc/passwd') == 'passwd'
        assert _sanitize_filename('file\x00evil.txt') == 'fileevil.txt'
        assert _sanitize_filename('sub/dir/file.txt') == 'file.txt'

    def test_invalid_base64(self, app_module):
        files = [{'filename': 'f.txt', 'content': '!!!bad!!!'}]
        data = self._call({'files': files, 'reason': 't', 'source': 's'}, app_module)
        assert 'error' in str(data).lower()

    def test_missing_filename(self, app_module):
        files = [{'content': base64.b64encode(b'data').decode()}]
        data = self._call({'files': files, 'reason': 't', 'source': 's'}, app_module)
        assert 'error' in str(data).lower()

    @patch('mcp_tools.send_batch_upload_notification')
    def test_valid_pending(self, mock_notify, app_module):
        files = [{'filename': 'index.html',
                  'content': base64.b64encode(b'<html></html>').decode()}]
        data = self._call({'files': files, 'reason': 'deploy', 'source': 'bot'}, app_module)
        assert data.get('status') == 'pending_approval'
        assert data.get('file_count') == 1
        mock_notify.assert_called_once()


# ============================================================================
# Constants
# ============================================================================

class TestUploadConstants:

    def test_constants_exist(self, app_module):
        from constants import (
            TRUST_SESSION_MAX_UPLOADS,
            TRUST_UPLOAD_MAX_BYTES_PER_FILE,
            TRUST_UPLOAD_MAX_BYTES_TOTAL,
            TRUST_UPLOAD_BLOCKED_EXTENSIONS,
        )
        assert TRUST_SESSION_MAX_UPLOADS >= 0
        assert TRUST_UPLOAD_MAX_BYTES_PER_FILE == 5 * 1024 * 1024
        assert TRUST_UPLOAD_MAX_BYTES_TOTAL == 20 * 1024 * 1024
        assert '.sh' in TRUST_UPLOAD_BLOCKED_EXTENSIONS


# ============================================================================
# create_trust_session with max_uploads
# ============================================================================

class TestCreateTrustSessionUploads:

    def test_with_max_uploads(self, app_module):
        tid = app_module.trust.create_trust_session(
            'create-up-test', '111111111111', '999999999',
            source='test', max_uploads=5
        )
        item = app_module.table.get_item(Key={'request_id': tid})['Item']
        assert int(item.get('max_uploads', 0)) == 5
        assert int(item.get('upload_count', 0)) == 0

    def test_default_no_uploads(self, app_module):
        """Default max_uploads=0 when not specified (backward compat)."""
        tid = app_module.trust.create_trust_session(
            'create-no-up', '111111111111', '999999999',
            source='test'
        )
        item = app_module.table.get_item(Key={'request_id': tid})['Item']
        assert int(item.get('max_uploads', 0)) == 0


# ============================================================================
# Notification Functions
# ============================================================================

class TestUploadNotifications:

    @patch('notifications._send_message_silent')
    def test_trust_upload_notification(self, mock_send, app_module):
        from notifications import send_trust_upload_notification
        send_trust_upload_notification(
            filename='config.json', content_size=2048,
            sha256_hash='abcdef1234567890', trust_id='trust-abc-111',
            upload_count=2, max_uploads=5, source='test-bot'
        )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert '信任上傳' in text
        assert 'config.json' in text
        assert '2/5' in text

    @patch('notifications._send_message_silent')
    def test_trust_upload_notification_large_file(self, mock_send, app_module):
        from notifications import send_trust_upload_notification
        send_trust_upload_notification(
            filename='big.dat', content_size=2 * 1024 * 1024,
            sha256_hash='xyz', trust_id='trust-abc-111',
            upload_count=1, max_uploads=3
        )
        text = mock_send.call_args[0][0]
        assert 'MB' in text

    @patch('notifications._send_message')
    def test_batch_upload_notification(self, mock_send, app_module):
        from notifications import send_batch_upload_notification
        send_batch_upload_notification(
            batch_id='batch-123', file_count=5, total_size=10240,
            ext_counts={'HTML': 2, 'JS': 3}, reason='deploy',
            source='test-bot', account_name='Default'
        )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert '批量上傳' in text
        assert '5 個檔案' in text
        assert 'HTML: 2' in text

    @patch('notifications._send_message_silent')
    def test_trust_upload_notification_no_source(self, mock_send, app_module):
        from notifications import send_trust_upload_notification
        send_trust_upload_notification(
            filename='x.txt', content_size=10,
            sha256_hash='abc', trust_id='trust-x-111',
            upload_count=1, max_uploads=1
        )
        text = mock_send.call_args[0][0]
        # Should not crash and should not show empty source line
        assert '信任上傳' in text


# ============================================================================
# Tool Schema
# ============================================================================

class TestUploadToolSchema:

    def test_upload_batch_in_schema(self, app_module):
        from tool_schema import MCP_TOOLS
        assert 'bouncer_upload_batch' in MCP_TOOLS
        schema = MCP_TOOLS['bouncer_upload_batch']
        props = schema.get('parameters', schema.get('inputSchema', {})).get('properties', {})
        assert 'files' in props
        assert 'trust_scope' in props

    def test_upload_has_trust_scope(self, app_module):
        from tool_schema import MCP_TOOLS
        schema = MCP_TOOLS['bouncer_upload']
        props = schema.get('parameters', schema.get('inputSchema', {})).get('properties', {})
        assert 'trust_scope' in props


# ============================================================================
# _format_size_human
# ============================================================================

class TestFormatSizeHuman:

    def test_bytes(self, app_module):
        from mcp_tools import _format_size_human
        assert _format_size_human(500) == '500 bytes'

    def test_kb(self, app_module):
        from mcp_tools import _format_size_human
        result = _format_size_human(2048)
        assert 'KB' in result

    def test_mb(self, app_module):
        from mcp_tools import _format_size_human
        result = _format_size_human(3 * 1024 * 1024)
        assert 'MB' in result


# ============================================================================
# Batch Upload Callback (deny)
# ============================================================================

class TestBatchUploadCallback:

    def _create_batch_item(self, table, batch_id='batch-test-001'):
        import json as _json
        files = [{'filename': 'test.txt', 'content_b64': 'dGVzdA==', 'content_type': 'text/plain', 'size': 4, 'sha256': 'abc'}]
        table.put_item(Item={
            'request_id': batch_id,
            'action': 'upload_batch',
            'bucket': 'bouncer-uploads-111111111111',
            'files': _json.dumps(files),
            'file_count': 1,
            'total_size': 4,
            'reason': 'test deploy',
            'source': 'test-bot',
            'trust_scope': 'test-scope',
            'account_id': '111111111111',
            'account_name': 'Default',
            'status': 'pending_approval',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 3600,
            'mode': 'mcp',
        })

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    def test_deny_batch(self, mock_update, mock_answer, app_module):
        from callbacks import handle_upload_batch_callback
        self._create_batch_item(app_module.table)
        item = app_module.table.get_item(Key={'request_id': 'batch-test-001'})['Item']
        result = handle_upload_batch_callback(
            'deny', 'batch-test-001', item, 12345, 'cb-123', '999999999'
        )
        # Verify denied
        updated = app_module.table.get_item(Key={'request_id': 'batch-test-001'})['Item']
        assert updated['status'] == 'denied'
        mock_answer.assert_called_once()

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('boto3.client')
    def test_approve_batch(self, mock_boto_client, mock_update, mock_answer, app_module):
        from callbacks import handle_upload_batch_callback
        self._create_batch_item(app_module.table, 'batch-approve-001')
        item = app_module.table.get_item(Key={'request_id': 'batch-approve-001'})['Item']

        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3
        result = handle_upload_batch_callback(
            'approve', 'batch-approve-001', item, 12345, 'cb-123', '999999999'
        )
        updated = app_module.table.get_item(Key={'request_id': 'batch-approve-001'})['Item']
        assert updated['status'] == 'approved'

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('boto3.client')
    def test_approve_trust_batch(self, mock_boto_client, mock_update, mock_answer, app_module):
        from callbacks import handle_upload_batch_callback
        self._create_batch_item(app_module.table, 'batch-trust-001')
        item = app_module.table.get_item(Key={'request_id': 'batch-trust-001'})['Item']

        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3
        result = handle_upload_batch_callback(
            'approve_trust', 'batch-trust-001', item, 12345, 'cb-123', '999999999'
        )
        # Verify trust session was created
        from trust import get_trust_session
        session = get_trust_session('test-scope', '111111111111')
        assert session is not None
        assert int(session.get('max_uploads', 0)) > 0
