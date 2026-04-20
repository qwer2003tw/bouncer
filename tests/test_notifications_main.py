"""
test_notifications_main.py — Notifications 與 display summary 測試
Extracted from test_bouncer.py batch-b
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

pytestmark = pytest.mark.xdist_group("notifications")


def _make_client_error(code='TestError', message='Test error'):
    return ClientError({'Error': {'Code': code, 'Message': message}}, 'TestOperation')


# ============================================================================
# Generate Display Summary 測試
# ============================================================================


@pytest.fixture(autouse=True)
def _mock_entities_send():
    """Ensure send_message_with_entities is mocked for pre-entities tests.

    Uses mock.patch.object for thread-safe mocking (xdist compatible).
    """
    import telegram as _tg
    from unittest.mock import patch, MagicMock

    mock_msg_id = 99999
    mock_response = {'ok': True, 'result': {'message_id': mock_msg_id}}

    # Save originals
    orig_entities = getattr(_tg, 'send_message_with_entities', None)
    orig_send = getattr(_tg, 'send_telegram_message', None)

    # Replace both so assertions on either work
    mock_entities = MagicMock(return_value=mock_response)
    mock_send_fn = MagicMock(return_value=mock_response)
    _tg.send_message_with_entities = mock_entities
    _tg.send_telegram_message = mock_send_fn

    # Reload notification sub-modules and main module so they pick up the mocks
    import importlib
    for mod_name in ['notifications_core', 'notifications_execute', 'notifications_grant', 'notifications']:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])

    yield mock_entities

    # Restore
    if orig_entities is not None:
        _tg.send_message_with_entities = orig_entities
    elif hasattr(_tg, 'send_message_with_entities'):
        delattr(_tg, 'send_message_with_entities')
    if orig_send is not None:
        _tg.send_telegram_message = orig_send


class TestGenerateDisplaySummary:
    """Tests for generate_display_summary() helper function in utils.py"""

    def test_execute_command(self, app_module):
        """Execute action uses command[:100]"""
        from utils import generate_display_summary
        result = generate_display_summary('execute', command='aws s3 ls --region us-east-1')
        assert result == 'aws s3 ls --region us-east-1'

    def test_execute_command_truncation(self, app_module):
        """Execute command truncated to 100 chars"""
        from utils import generate_display_summary
        long_cmd = 'aws s3 cp ' + 'x' * 200
        result = generate_display_summary('execute', command=long_cmd)
        assert len(result) == 100
        assert result == long_cmd[:100]

    def test_execute_empty_command(self, app_module):
        """Execute with empty command shows fallback"""
        from utils import generate_display_summary
        result = generate_display_summary('execute', command='')
        assert result == '(empty command)'

    def test_execute_no_action(self, app_module):
        """No action defaults to execute behavior"""
        from utils import generate_display_summary
        result = generate_display_summary('', command='aws sts get-caller-identity')
        assert result == 'aws sts get-caller-identity'

    def test_upload_with_size(self, app_module):
        """Upload shows filename and size"""
        from utils import generate_display_summary
        result = generate_display_summary('upload', filename='index.html', content_size=12288)
        assert result == 'upload: index.html (12.00 KB)'

    def test_upload_without_size(self, app_module):
        """Upload without size shows just filename"""
        from utils import generate_display_summary
        result = generate_display_summary('upload', filename='index.html')
        assert result == 'upload: index.html'

    def test_upload_batch_with_size(self, app_module):
        """Upload batch shows count and total size"""
        from utils import generate_display_summary
        result = generate_display_summary('upload_batch', file_count=9, total_size=250880)
        assert result == 'upload_batch (9 個檔案, 245.00 KB)'

    def test_upload_batch_without_size(self, app_module):
        """Upload batch without total_size shows just count"""
        from utils import generate_display_summary
        result = generate_display_summary('upload_batch', file_count=5)
        assert result == 'upload_batch (5 個檔案)'

    def test_upload_batch_missing_count(self, app_module):
        """Upload batch with missing file_count shows 'unknown'"""
        from utils import generate_display_summary
        result = generate_display_summary('upload_batch')
        assert 'unknown' in result

    def test_add_account(self, app_module):
        """Add account shows name and ID"""
        from utils import generate_display_summary
        result = generate_display_summary('add_account', account_name='Dev', account_id='992382394211')
        assert result == 'add_account: Dev (992382394211)'

    def test_remove_account(self, app_module):
        """Remove account shows name and ID"""
        from utils import generate_display_summary
        result = generate_display_summary('remove_account', account_name='Dev', account_id='992382394211')
        assert result == 'remove_account: Dev (992382394211)'

    def test_deploy(self, app_module):
        """Deploy shows project_id"""
        from utils import generate_display_summary
        result = generate_display_summary('deploy', project_id='bouncer')
        assert result == 'deploy: bouncer'

    def test_deploy_missing_project(self, app_module):
        """Deploy with missing project_id shows fallback"""
        from utils import generate_display_summary
        result = generate_display_summary('deploy')
        assert result == 'deploy: unknown project'

    def test_unknown_action(self, app_module):
        """Unknown action returns action name"""
        from utils import generate_display_summary
        result = generate_display_summary('some_future_action')
        assert result == 'some_future_action'


# ============================================================================
# Display Summary In Items 測試
# ============================================================================

@pytest.mark.xdist_group("app_module")
class TestDisplaySummaryInItems:
    """Tests that display_summary is written to DynamoDB items"""

    @patch('execute_pipeline.send_approval_request')
    @patch('execute_pipeline.send_blocked_notification')
    def test_execute_item_has_display_summary(self, mock_blocked, mock_approval, app_module):
        """Execute approval item has display_summary field"""
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', 'src'))
        from notifications import NotificationResult
        from mcp_execute import mcp_tool_execute
        mock_approval.return_value = NotificationResult(ok=True, message_id=None)
        """Execute approval item has display_summary field"""
        result = mcp_tool_execute('ds-exec-1', {
            'command': 'aws s3 cp local.txt s3://my-bucket/file.txt',
            'trust_scope': 'test-session',
            'reason': 'test display summary',
            'source': 'test-bot',
        })
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert item['display_summary'] == 'aws s3 cp local.txt s3://my-bucket/file.txt'

    @patch('telegram.send_telegram_message')
    def test_upload_item_has_display_summary(self, mock_telegram, app_module):
        """Upload approval item has display_summary field"""
        mock_telegram.return_value = {'ok': True}
        import base64
        content_b64 = base64.b64encode(b'test content').decode()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-upload-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_upload', 'arguments': {
                    'filename': 'test.js',
                    'content': content_b64,
                    'content_type': 'application/javascript',
                    'reason': 'test display summary',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert item['display_summary'].startswith('upload: test.js')

    @patch('mcp_upload.send_batch_upload_notification')
    def test_upload_batch_item_has_display_summary(self, mock_notification, app_module):
        """Upload batch approval item has display_summary field"""
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', 'src'))
        from notifications import NotificationResult
        mock_notification.return_value = NotificationResult(ok=True, message_id=None)
        import base64
        content_b64 = base64.b64encode(b'test content').decode()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-batch-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_upload_batch', 'arguments': {
                    'files': [
                        {'filename': 'a.js', 'content': content_b64, 'content_type': 'application/javascript'},
                        {'filename': 'b.js', 'content': content_b64, 'content_type': 'application/javascript'},
                    ],
                    'reason': 'test display summary',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert 'upload_batch' in item['display_summary']
        assert '2 個檔案' in item['display_summary']

    @patch('mcp_admin.send_account_approval_request')
    def test_add_account_item_has_display_summary(self, mock_approval, app_module):
        """Add account approval item has display_summary field"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-add-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_add_account', 'arguments': {
                    'account_id': '222222222222',
                    'name': 'TestAccount',
                    'role_arn': 'arn:aws:iam::222222222222:role/BouncerExecutionRole',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert item['display_summary'] == 'add_account: TestAccount (222222222222)'

    @patch('mcp_admin.send_account_approval_request')
    def test_remove_account_item_has_display_summary(self, mock_approval, app_module):
        """Remove account approval item has display_summary field"""
        # First add the account so it exists for removal
        import accounts
        import db
        db.accounts_table.put_item(Item={
            'account_id': '333333333333',
            'name': 'RemoveMe',
            'role_arn': 'arn:aws:iam::333333333333:role/BouncerExecutionRole',
            'enabled': True,
        })
        # Clear cache
        if hasattr(accounts, '_accounts_cache'):
            accounts._accounts_cache = {}

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-remove-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_remove_account', 'arguments': {
                    'account_id': '333333333333',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert 'remove_account' in item['display_summary']
        assert '333333333333' in item['display_summary']


# ============================================================================
# notifications.py 直接覆蓋測試
# ============================================================================

# Ensure src is on path for direct notifications import
_SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _make_notifications_module():
    """Reload notifications and its dependencies with mocked env."""
    from unittest.mock import MagicMock
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
    os.environ.setdefault('APPROVED_CHAT_ID', '99999')
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')

    # Clear cached modules so imports are fresh
    for mod in ['notifications', 'notifications_core', 'notifications_execute', 'notifications_grant',
                'telegram', 'commands', 'constants', 'utils',
                'risk_scorer', 'template_scanner', 'scheduler_service']:
        sys.modules.pop(mod, None)
        sys.modules.pop(f'src.{mod}', None)

    import notifications as _n

    # Mock send_message_with_entities (entities Phase 2, Sprint 13)
    _n._telegram.send_message_with_entities = MagicMock(
        return_value={'ok': True, 'result': {'message_id': 99999}}
    )

    return _n


class TestNotificationsCoverage:
    """Direct unit tests for notifications.py functions.

    Each test patches telegram._send_message / _send_message_silent
    at the notifications module level so we get real line coverage.
    """

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    @pytest.fixture(autouse=True)
    def _reload_notifications(self):
        """Reload notifications fresh for each test to avoid state bleed."""
        self.notif = _make_notifications_module()

    def _patch_send(self, ok: bool = True):
        """Return a context manager that patches both send helpers."""
        send_ret = {'ok': ok, 'result': {'message_id': 1}}
        send_mock = MagicMock(return_value=send_ret)
        silent_mock = MagicMock(return_value=None)
        ctx = patch.multiple(
            self.notif,
            _send_message=send_mock,
            _send_message_silent=silent_mock,
        )
        return ctx, send_mock, silent_mock

    def _patch_entities_send(self, ok: bool = True):
        """Return a context manager that patches _telegram.send_message_with_entities.

        Use this for tests of functions migrated to entities mode:
        send_approval_request, send_account_approval_request, send_blocked_notification.
        Call signature: send_mock(text, entities, reply_markup=keyboard) or
                        send_mock(text, entities, silent=True)
        Extract: text = call_args[0][0], keyboard = call_args[1].get('reply_markup')
        """
        send_ret = {'ok': ok, 'result': {'message_id': 1}}
        send_mock = MagicMock(return_value=send_ret)
        ctx = patch.object(self.notif._telegram, 'send_message_with_entities', send_mock)
        return ctx, send_mock

    # ------------------------------------------------------------------
    # _escape_markdown
    # ------------------------------------------------------------------

    def test_escape_markdown_special_chars(self):
        """_escape_markdown delegates to telegram.escape_markdown."""
        with patch('telegram.escape_markdown', return_value='escaped') as mock_esc:
            result = self.notif._escape_markdown('hello_world')
        mock_esc.assert_called_once_with('hello_world')
        assert result == 'escaped'

    def test_escape_markdown_real_chars(self):
        """_escape_markdown produces non-empty output for special chars."""
        # Use the real telegram module (no mock) to verify actual escaping
        result = self.notif._escape_markdown('hello_world*[test]')
        assert result  # non-empty
        assert isinstance(result, str)

    # ------------------------------------------------------------------
    # send_approval_request — happy path (normal command)
    # ------------------------------------------------------------------

    def test_send_approval_request_happy_path(self):
        """Normal (non-dangerous) command → sends message, returns NotificationResult(ok=True)."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            result = self.notif.send_approval_request(
                request_id='req-001',
                command='aws s3 ls',
                reason='list buckets',
                source='TestBot',
                account_id='123456789012',
                account_name='DevAccount',
            )
        assert result.ok is True
        send_mock.assert_called_once()
        text = send_mock.call_args[0][0]
        keyboard = send_mock.call_args[1].get('reply_markup')
        assert 'req-001' in text
        assert 'list buckets' in text

    def test_send_approval_request_returns_false_on_api_failure(self):
        """Returns NotificationResult(ok=False) when telegram API returns ok=False."""
        ctx, send_mock = self._patch_entities_send(ok=False)
        with ctx:
            result = self.notif.send_approval_request(
                request_id='req-002',
                command='aws s3 ls',
                reason='test',
            )
        assert result.ok is False

    def test_send_approval_request_dangerous_command(self):
        """Dangerous command (e.g. delete) → different keyboard (⚠️ 確認執行)."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with patch('commands.is_dangerous', return_value=True):
            with ctx:
                result = self.notif.send_approval_request(
                    request_id='req-003',
                    command='aws s3 rb s3://important-bucket --force',
                    reason='cleanup',
                )
        assert result.ok is True
        text = send_mock.call_args[0][0]
        keyboard = send_mock.call_args[1].get('reply_markup')
        # Dangerous path shows "高危操作"
        assert '高危' in text or '⚠️' in text
        # keyboard should have "Confirm" (English button)
        buttons_flat = [btn['text'] for row in keyboard['inline_keyboard'] for btn in row]
        assert any('Confirm' in b for b in buttons_flat)

    def test_send_approval_request_long_command_truncated(self):
        """Commands longer than 500 chars are truncated in the message."""
        long_cmd = 'aws s3 cp ' + 'x' * 600
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-004',
                command=long_cmd,
                reason='big copy',
            )
        text = send_mock.call_args[0][0]
        # The preview should be truncated — original 600+10 chars would show in full
        assert '...' in text

    def test_send_approval_request_timeout_formats(self):
        """Timeout value is formatted correctly (secs / mins / hours)."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_approval_request('r1', 'aws s3 ls', 'r', timeout=30)
        text = send_mock.call_args[0][0]
        assert '秒' in text

        with ctx:
            self.notif.send_approval_request('r2', 'aws s3 ls', 'r', timeout=120)
        text = send_mock.call_args[0][0]
        assert '分鐘' in text

        with ctx:
            self.notif.send_approval_request('r3', 'aws s3 ls', 'r', timeout=7200)
        text = send_mock.call_args[0][0]
        assert '小時' in text

    def test_send_approval_request_with_assume_role(self):
        """assume_role is parsed for account_id and role name."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-005',
                command='aws sts get-caller-identity',
                reason='check role',
                assume_role='arn:aws:iam::999888777666:role/BouncerRole',
            )
        text = send_mock.call_args[0][0]
        assert '999888777666' in text

    def test_send_approval_request_with_template_scan_hit(self):
        """template_scan_result with hits is included in message."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        scan_result = {
            'hit_count': 2,
            'severity': 'high',
            'max_score': 80,
            'escalate': False,
            'factors': [
                {'details': 'risky param --force'},
                {'details': 'delete operation'},
            ]
        }
        with ctx:
            self.notif.send_approval_request(
                request_id='req-006',
                command='aws s3 rb s3://bucket --force',
                reason='cleanup',
                template_scan_result=scan_result,
            )
        text = send_mock.call_args[0][0]
        assert 'Template Scan' in text or 'HIGH' in text

    def test_send_approval_request_template_scan_many_factors(self):
        """template_scan_result with >3 factors shows truncation note."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        scan_result = {
            'hit_count': 5,
            'severity': 'critical',
            'max_score': 95,
            'escalate': True,
            'factors': [
                {'details': f'factor {i}'} for i in range(6)
            ]
        }
        with ctx:
            self.notif.send_approval_request(
                request_id='req-007',
                command='aws iam delete-user --user-name admin',
                reason='remove user',
                template_scan_result=scan_result,
            )
        text = send_mock.call_args[0][0]
        # Should show truncation for >3 factors
        assert '及其他' in text or '...' in text

    # ------------------------------------------------------------------
    # send_account_approval_request
    # ------------------------------------------------------------------

    def test_send_account_approval_request_add(self):
        """add action sends appropriate text with account info."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acc-001',
                action='add',
                account_id='111222333444',
                name='NewAccount',
                role_arn='arn:aws:iam::111222333444:role/BouncerExecutionRole',
                source='AdminBot',
            )
        send_mock.assert_called_once()
        text = send_mock.call_args[0][0]
        keyboard = send_mock.call_args[1].get('reply_markup')
        assert '新增' in text
        assert '111222333444' in text
        assert 'NewAccount' in text
        assert 'acc-001' in text

    def test_send_account_approval_request_remove(self):
        """remove action sends appropriate text."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acc-002',
                action='remove',
                account_id='555666777888',
                name='OldAccount',
                role_arn='',
                source='AdminBot',
            )
        send_mock.assert_called_once()
        text = send_mock.call_args[0][0]
        assert '移除' in text
        assert '555666777888' in text

    def test_send_account_approval_request_with_context(self):
        """context is included in the message when provided."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acc-003',
                action='add',
                account_id='000111222333',
                name='ContextAccount',
                role_arn='arn:aws:iam::000111222333:role/R',
                source='AdminBot',
                context='sprint5 setup',
            )
        send_mock.assert_called_once()

    # ------------------------------------------------------------------
    # send_trust_auto_approve_notification
    # ------------------------------------------------------------------

    def test_send_trust_auto_approve_basic(self):
        """Basic trust auto-approve notification is sent silently."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-001',
                remaining='5 min',
                count=2,
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        keyboard = entities_mock.call_args[1].get('reply_markup')
        assert '自動批准' in text
        assert 'aws s3 ls' in text

    def test_send_trust_auto_approve_with_result(self):
        """When result is provided, it appears in the message."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-002',
                remaining='3 min',
                count=1,
                result='s3://bucket1\ns3://bucket2',
            )
        text = entities_mock.call_args[0][0]
        assert '結果' in text

    def test_send_trust_auto_approve_with_error_result(self):
        """Error result prefix (❌) is detected and shown."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-003',
                remaining='',
                count=3,
                result='❌ Access denied',
            )
        text = entities_mock.call_args[0][0]
        assert '❌' in text

    def test_send_trust_auto_approve_with_long_result(self):
        """Long result (>50 lines) uses expandable_blockquote (S34-004)."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            # Create result with 60 lines (above threshold of 50)
            long_result = '\n'.join([f'Line {i}' for i in range(60)])
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-004',
                remaining='',
                count=1,
                result=long_result,
            )
        text = entities_mock.call_args[0][0]
        entities = entities_mock.call_args[0][1]

        # Long results are NOT truncated anymore - they use expandable_blockquote
        assert long_result in text

        # Verify expandable_blockquote entity is present for long output
        assert any(e.get('type') == 'expandable_blockquote' for e in entities)

    def test_send_trust_auto_approve_with_source_and_reason(self):
        """source and reason appear in notification."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-005',
                remaining='10 min',
                count=4,
                source='TestBot',
                reason='auto-deploy',
            )
        text = entities_mock.call_args[0][0]
        assert 'TestBot' in text
        assert 'auto-deploy' in text

    def test_send_trust_auto_approve_revoke_button(self):
        """Keyboard has revoke button with correct trust_id."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-006',
                remaining='',
                count=1,
            )
        keyboard = entities_mock.call_args[1].get('reply_markup')
        callbacks = [btn['callback_data'] for row in keyboard['inline_keyboard'] for btn in row]
        assert any('trust-006' in cb for cb in callbacks)

    # ------------------------------------------------------------------
    # send_grant_request_notification
    # ------------------------------------------------------------------

    def test_send_grant_request_basic(self):
        """Basic grant request with grantable commands."""
        ctx, entities_mock = self._patch_entities_send()
        commands_detail = [
            {'command': 'aws s3 ls', 'category': 'grantable'},
            {'command': 'aws s3 cp src dst', 'category': 'grantable'},
        ]
        with ctx:
            self.notif.send_grant_request_notification(
                grant_id='grant-001',
                commands_detail=commands_detail,
                reason='batch deploy',
                source='DevBot',
                account_id='123456789012',
                ttl_minutes=30,
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        keyboard = entities_mock.call_args[1].get('reply_markup')
        assert 'grant-001' in text
        assert 'batch deploy' in text
        assert '可授權' in text

    def test_send_grant_request_allow_repeat(self):
        """allow_repeat=True shows 可重複 mode."""
        ctx, entities_mock = self._patch_entities_send()
        commands_detail = [
            {'command': 'aws s3 ls', 'category': 'grantable'},
        ]
        with ctx:
            self.notif.send_grant_request_notification(
                grant_id='grant-002',
                commands_detail=commands_detail,
                reason='repeat mode',
                source='DevBot',
                account_id='123456789012',
                ttl_minutes=60,
                allow_repeat=True,
            )
        text = entities_mock.call_args[0][0]
        assert '可重複' in text

    def test_send_grant_request_mixed_categories(self):
        """Mixed grantable + requires_individual + blocked all appear."""
        ctx, entities_mock = self._patch_entities_send()
        commands_detail = [
            {'command': 'aws s3 ls', 'category': 'grantable'},
            {'command': 'aws iam create-user', 'category': 'requires_individual'},
            {'command': 'aws iam delete-account', 'category': 'blocked'},
        ]
        with ctx:
            self.notif.send_grant_request_notification(
                grant_id='grant-003',
                commands_detail=commands_detail,
                reason='mixed',
                source='Bot',
                account_id='111111111111',
                ttl_minutes=15,
            )
        text = entities_mock.call_args[0][0]
        keyboard = entities_mock.call_args[1].get('reply_markup')
        assert '可授權' in text
        assert '需個別審批' in text
        assert '已攔截' in text
        # Should have "Approve Safe Only" button when both grantable and requires_individual exist
        buttons = [btn['text'] for row in keyboard['inline_keyboard'] for btn in row]
        assert any('Safe' in b for b in buttons)

    def test_send_grant_request_many_commands_truncated(self):
        """More than 10 grantable commands shows truncation notice."""
        ctx, entities_mock = self._patch_entities_send()
        commands_detail = [
            {'command': f'aws s3 ls bucket-{i}', 'category': 'grantable'}
            for i in range(15)
        ]
        with ctx:
            self.notif.send_grant_request_notification(
                grant_id='grant-004',
                commands_detail=commands_detail,
                reason='many',
                source='Bot',
                account_id='111111111111',
                ttl_minutes=10,
            )
        text = entities_mock.call_args[0][0]
        assert '及其他' in text

    def test_send_grant_request_error_handling(self):
        """Exception inside the function is caught — does not raise."""
        # Pass invalid commands_detail to trigger error path
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('fail')):
            # Should not raise
            self.notif.send_grant_request_notification(
                grant_id='grant-err',
                commands_detail=[{'command': 'aws s3 ls', 'category': 'grantable'}],
                reason='test',
                source='Bot',
                account_id='111',
                ttl_minutes=5,
            )

    # ------------------------------------------------------------------
    # send_grant_execute_notification
    # ------------------------------------------------------------------

    def test_send_grant_execute_notification_success(self):
        """Grant execute notification sent silently."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='aws s3 ls',
                grant_id='grant-exec-001',
                result='bucket1\nbucket2',
                remaining_info='2/3 commands, 10:00',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        keyboard = entities_mock.call_args[1].get('reply_markup')
        assert 'Grant' in text
        assert 'grant-exec-001' in text[:50] or 'grant-exec' in text

    def test_send_grant_execute_notification_error_result(self):
        """Error result is detected and shown with ❌."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='aws s3 cp src dst',
                grant_id='grant-exec-002',
                result='❌ NoSuchBucket',
                remaining_info='1/3',
            )
        text = entities_mock.call_args[0][0]
        assert '❌' in text

    def test_send_grant_execute_notification_long_command(self):
        """Long command is truncated."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='aws s3 cp ' + 'x' * 200,
                grant_id='grant-exec-003',
                result='ok',
                remaining_info='1/1',
            )
        text = entities_mock.call_args[0][0]
        assert '...' in text

    # ------------------------------------------------------------------
    # send_grant_execute_notification - exit code regression tests (S34)
    # ------------------------------------------------------------------

    def test_send_grant_execute_notification_exit_code_0_shows_success(self):
        """Exit code 0 should show ✅ status."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='echo "Hello World"',
                grant_id='grant-exec-exit0',
                result='Hello World\n\n(exit code: 0)',
                remaining_info='1/1',
            )
        text = entities_mock.call_args[0][0]
        assert '✅' in text
        assert '❌' not in text

    def test_send_grant_execute_notification_exit_code_1_shows_failure(self):
        """Exit code 1 should show ❌ status."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='false',
                grant_id='grant-exec-exit1',
                result='Command failed\n\n(exit code: 1)',
                remaining_info='1/1',
            )
        text = entities_mock.call_args[0][0]
        assert '❌' in text

    def test_send_grant_execute_notification_exit_code_127_shows_failure(self):
        """Exit code 127 (command not found) should show ❌ status."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='nonexistent_command',
                grant_id='grant-exec-exit127',
                result='bash: nonexistent_command: command not found\n\n(exit code: 127)',
                remaining_info='1/1',
            )
        text = entities_mock.call_args[0][0]
        assert '❌' in text

    def test_send_grant_execute_notification_no_exit_code_defaults_to_success(self):
        """No exit code in result should default to ✅ status."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_execute_notification(
                command='some command',
                grant_id='grant-exec-no-exit',
                result='Some output without exit code marker',
                remaining_info='1/1',
            )
        text = entities_mock.call_args[0][0]
        assert '✅' in text
        assert '❌' not in text

    # ------------------------------------------------------------------
    # send_grant_complete_notification
    # ------------------------------------------------------------------

    def test_send_grant_complete_notification(self):
        """Grant complete notification is sent silently."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_complete_notification(
                grant_id='grant-done-001',
                reason='all commands executed',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        assert 'Grant' in text
        assert '已結束' in text

    def test_send_grant_complete_notification_long_id(self):
        """Long grant_id is truncated in notification."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_grant_complete_notification(
                grant_id='g' * 30,
                reason='expired',
            )
        text = entities_mock.call_args[0][0]
        assert '...' in text

    # ------------------------------------------------------------------
    # send_blocked_notification
    # ------------------------------------------------------------------

    def test_send_blocked_notification_basic(self):
        """Basic blocked notification is sent silently."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_blocked_notification(
                command='aws iam delete-account',
                block_reason='Blocked: dangerous operation',
                source='TestBot',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        assert '封鎖' in text
        assert 'aws iam delete-account' in text

    def test_send_blocked_notification_long_command(self):
        """Long command is truncated to 100 chars."""
        ctx, entities_mock = self._patch_entities_send()
        long_cmd = 'aws s3 cp ' + 'x' * 200
        with ctx:
            self.notif.send_blocked_notification(
                command=long_cmd,
                block_reason='too long',
                source='Bot',
            )
        text = entities_mock.call_args[0][0]
        assert '...' in text

    def test_send_blocked_notification_no_source(self):
        """No source defaults to 'Unknown'."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_blocked_notification(
                command='aws s3 ls',
                block_reason='rate limit',
            )
        text = entities_mock.call_args[0][0]
        assert 'Unknown' in text

    def test_send_blocked_notification_error_handling(self):
        """Exception is caught — does not raise."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('fail')):
            # Should not raise
            self.notif.send_blocked_notification(
                command='aws s3 ls',
                block_reason='rate limit',
                source='Bot',
            )

    # ------------------------------------------------------------------
    # send_trust_upload_notification
    # ------------------------------------------------------------------

    def test_send_trust_upload_notification_basic(self):
        """Trust upload notification is sent silently."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_upload_notification(
                filename='index.html',
                content_size=1024,
                sha256_hash='abcdef1234567890abcdef1234567890',
                trust_id='trust-up-001',
                upload_count=1,
                max_uploads=5,
                source='UploadBot',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        assert 'index.html' in text
        assert '信任上傳' in text

    def test_send_trust_upload_notification_batch_hash(self):
        """batch sha256 is handled (not truncated)."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_trust_upload_notification(
                filename='batch-upload',
                content_size=2048,
                sha256_hash='batch',
                trust_id='trust-up-002',
                upload_count=3,
                max_uploads=10,
            )
        text = entities_mock.call_args[0][0]
        assert 'batch' in text

    def test_send_trust_upload_notification_error_handling(self):
        """Exception is caught — does not raise."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('fail')):
            self.notif.send_trust_upload_notification(
                filename='file.txt',
                content_size=100,
                sha256_hash='abc123',
                trust_id='trust-up-err',
                upload_count=1,
                max_uploads=5,
            )

    # ------------------------------------------------------------------
    # send_batch_upload_notification
    # ------------------------------------------------------------------

    def test_send_batch_upload_notification_basic(self):
        """Batch upload notification is sent."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_batch_upload_notification(
                batch_id='batch-001',
                file_count=5,
                total_size=512 * 1024,
                ext_counts={'.js': 3, '.css': 2},
                reason='deploy frontend',
                source='DevBot',
                account_name='DevAccount',
                trust_scope='test-session',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        assert '批量上傳' in text
        assert 'batch-001' in text
        assert '5 個檔案' in text

    def test_send_batch_upload_notification_error_handling(self):
        """Exception is caught — does not raise."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('fail')):
            self.notif.send_batch_upload_notification(
                batch_id='batch-err',
                file_count=2,
                total_size=1024,
                ext_counts={'.js': 2},
                reason='test',
                source='Bot',
            )

    # ------------------------------------------------------------------
    # send_presigned_notification
    # ------------------------------------------------------------------

    def test_send_presigned_notification_basic(self):
        """Presigned notification is sent silently with correct fields."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_presigned_notification(
                filename='report.pdf',
                source='PrivateBot',
                account_id='190825685292',
                expires_at='2024-01-01T00:00:00Z',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        assert 'report.pdf' in text
        assert 'PrivateBot' in text
        assert '190825685292' in text
        assert '2024-01-01T00:00:00Z' in text

    def test_send_presigned_notification_no_presigned_url_in_message(self):
        """Presigned URL itself must NOT appear in the notification text."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_presigned_notification(
                filename='secret.pdf',
                source='Bot',
                account_id='111',
                expires_at='2024-01-01T00:00:00Z',
            )
        text = entities_mock.call_args[0][0]
        # No URL with X-Amz-Signature in the notification
        assert 'X-Amz-Signature' not in text
        assert 'https://s3.amazonaws.com' not in text

    def test_send_presigned_notification_none_fields(self):
        """None fields default gracefully (no crash)."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_presigned_notification(
                filename=None,
                source=None,
                account_id=None,
                expires_at=None,
            )
        entities_mock.assert_called_once()

    def test_send_presigned_notification_error_handling(self):
        """Exception in send_message_with_entities is caught — no crash."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('fail')):
            self.notif.send_presigned_notification(
                filename='file.pdf',
                source='Bot',
                account_id='111',
                expires_at='2024-01-01T00:00:00Z',
            )

    # ------------------------------------------------------------------
    # send_presigned_batch_notification
    # ------------------------------------------------------------------

    def test_send_presigned_batch_notification_basic(self):
        """Presigned batch notification is sent silently with correct fields."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_presigned_batch_notification(
                source='BatchBot',
                count=7,
                account_id='190825685292',
                expires_at='2024-06-01T12:00:00Z',
            )
        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]
        assert '7' in text
        assert 'BatchBot' in text
        assert '190825685292' in text

    def test_send_presigned_batch_notification_none_fields(self):
        """None fields default gracefully (no crash)."""
        ctx, entities_mock = self._patch_entities_send()
        with ctx:
            self.notif.send_presigned_batch_notification(
                source=None,
                count=0,
                account_id=None,
                expires_at=None,
            )
        entities_mock.assert_called_once()

    def test_send_presigned_batch_notification_error_handling(self):
        """Exception is caught — no crash."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('fail')):
            self.notif.send_presigned_batch_notification(
                source='Bot',
                count=3,
                account_id='111',
                expires_at='2024-01-01T00:00:00Z',
            )

    # ------------------------------------------------------------------
    # _send_message / _send_message_silent pass-through
    # ------------------------------------------------------------------

    def test_send_message_delegates_to_telegram(self):
        """_send_message calls telegram.send_telegram_message."""
        with patch('telegram.send_telegram_message', return_value={'ok': True}) as mock_tg:
            result = self.notif._send_message('hello')
        mock_tg.assert_called_once_with('hello', None)
        assert result == {'ok': True}

    def test_send_message_silent_delegates_to_telegram(self):
        """_send_message_silent calls telegram.send_telegram_message_silent."""
        with patch('telegram.send_telegram_message_silent', return_value=None) as mock_tg:
            self.notif._send_message_silent('hello')
        mock_tg.assert_called_once_with('hello', None)

    # ------------------------------------------------------------------
    # Additional edge-case tests for remaining uncovered lines
    # ------------------------------------------------------------------

    def test_send_approval_request_lambda_env_dangerous(self):
        """lambda update-function-configuration --environment triggers warning block."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with patch('commands.check_lambda_env_update', return_value=('DANGEROUS', 'Risky env update')), \
             patch('commands.is_dangerous', return_value=False):
            with ctx:
                self.notif.send_approval_request(
                    request_id='req-lambda',
                    command='aws lambda update-function-configuration --function-name fn --environment Variables={K=V}',
                    reason='update env',
                )
        text = send_mock.call_args[0][0]
        assert 'Risky env update' in text or '🔴' in text

    def test_send_approval_request_bad_assume_role_format(self):
        """Invalid assume_role ARN falls back to raw display without crashing."""
        ctx, send_mock = self._patch_entities_send(ok=True)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-bad-arn',
                command='aws s3 ls',
                reason='test',
                assume_role='not-a-valid-arn',
            )
        # Should still succeed (exception handled internally)
        assert send_mock.called

    def test_send_grant_request_many_requires_individual(self):
        """More than 10 requires_individual commands shows truncation notice."""
        ctx, entities_mock = self._patch_entities_send()
        commands_detail = [
            {'command': f'aws iam create-user --user-name user-{i}', 'category': 'requires_individual'}
            for i in range(12)
        ]
        with ctx:
            self.notif.send_grant_request_notification(
                grant_id='grant-many-req',
                commands_detail=commands_detail,
                reason='many requires_individual',
                source='Bot',
                account_id='111111111111',
                ttl_minutes=10,
            )
        text = entities_mock.call_args[0][0]
        assert '及其他' in text

    def test_send_grant_request_many_blocked(self):
        """More than 10 blocked commands shows truncation notice."""
        ctx, entities_mock = self._patch_entities_send()
        commands_detail = [
            {'command': f'aws iam delete-account-{i}', 'category': 'blocked'}
            for i in range(12)
        ]
        with ctx:
            self.notif.send_grant_request_notification(
                grant_id='grant-many-blocked',
                commands_detail=commands_detail,
                reason='many blocked',
                source='Bot',
                account_id='111111111111',
                ttl_minutes=5,
            )
        text = entities_mock.call_args[0][0]
        assert '及其他' in text

    def test_send_grant_execute_notification_error_handling(self):
        """Exception in send_grant_execute_notification is caught — no crash."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('tg fail')):
            self.notif.send_grant_execute_notification(
                command='aws s3 ls',
                grant_id='grant-exec-err',
                result='ok',
                remaining_info='1/1',
            )

    def test_send_grant_complete_notification_error_handling(self):
        """Exception in send_grant_complete_notification is caught — no crash."""
        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=OSError('tg fail')):
            self.notif.send_grant_complete_notification(
                grant_id='grant-complete-err',
                reason='expired',
            )


# ============================================================================
# sprint7-002: NotificationResult, post_notification_setup, SchedulerService
# ============================================================================

class TestSchedulerIntegration:
    """Tests for the message_id storage + EventBridge schedule creation
    introduced in sprint7-002.

    B's architecture: send_approval_request returns NotificationResult;
    callers invoke post_notification_setup to store message_id + schedule.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Fresh notifications module + mocked DynamoDB table per test."""
        # Save all modules that _make_notifications_module will pop
        saved_modules = {}
        for mod_name in ['notifications', 'telegram', 'commands', 'constants', 'utils',
                         'risk_scorer', 'template_scanner', 'scheduler_service']:
            for key in [mod_name, f'src.{mod_name}']:
                if key in sys.modules:
                    saved_modules[key] = sys.modules[key]

        self.notif = _make_notifications_module()

        # Provide a mock db.table for post_notification_setup
        import db as _db
        self._original_table = _db.table
        self.mock_table = MagicMock()
        _db.table = self.mock_table

        yield

        # Restore original table to avoid state bleed
        _db.table = self._original_table

        # Restore original modules to avoid poisoning module-scoped fixtures
        for key, mod in saved_modules.items():
            sys.modules[key] = mod

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _patch_send_with_message_id(self, message_id: int = 42):
        """Patch both _send_message (legacy) and send_message_with_entities (entities Phase 2).

        Some functions (send_approval_request) use entities mode; others (_send_message) use legacy.
        This patches both so any function works correctly.
        """
        from contextlib import ExitStack
        stack = ExitStack()
        mock_resp = {'ok': True, 'result': {'message_id': message_id}}
        # Patch entities path (Sprint 13 Phase 2 migration)
        stack.enter_context(patch.object(
            self.notif._telegram, 'send_message_with_entities',
            return_value=mock_resp,
        ))
        # Patch legacy path (still used by upload/grant notifications)
        stack.enter_context(patch.object(
            self.notif, '_send_message',
            return_value=mock_resp,
        ))
        return stack

    # ------------------------------------------------------------------
    # test_message_id_returned_from_send_command_request
    # ------------------------------------------------------------------

    def test_message_id_returned_from_send_command_request(self):
        """send_approval_request returns NotificationResult with message_id."""
        with self._patch_send_with_message_id(999):
            result = self.notif.send_approval_request(
                request_id='cmd-req-001',
                command='aws s3 ls',
                reason='test',
                timeout=30,
            )

        assert result.ok is True
        assert result.message_id == 999

    # ------------------------------------------------------------------
    # test_message_id_returned_from_send_upload_request
    # ------------------------------------------------------------------

    def test_message_id_returned_from_send_upload_request(self):
        """send_batch_upload_notification returns NotificationResult with message_id."""
        with self._patch_send_with_message_id(777):
            result = self.notif.send_batch_upload_notification(
                batch_id='upload-req-001',
                file_count=3,
                total_size=1024,
                ext_counts={'.js': 3},
                reason='deploy',
                source='Bot',
                timeout=300,
            )

        assert result.ok is True
        assert result.message_id == 777

    # ------------------------------------------------------------------
    # test_post_notification_setup_stores_message_id
    # ------------------------------------------------------------------

    def test_post_notification_setup_stores_message_id(self):
        """post_notification_setup stores telegram_message_id in DDB."""
        with patch('scheduler_service.get_scheduler_service') as mock_svc:
            mock_svc.return_value = MagicMock()
            self.notif.post_notification_setup(
                request_id='my-req-123',
                telegram_message_id=4567,
                expires_at=int(time.time()) + 300,
            )

        self.mock_table.update_item.assert_called_once()
        call_kwargs = self.mock_table.update_item.call_args[1]
        assert call_kwargs['Key'] == {'request_id': 'my-req-123'}
        assert ':mid' in call_kwargs['ExpressionAttributeValues']
        assert call_kwargs['ExpressionAttributeValues'][':mid'] == 4567

    # ------------------------------------------------------------------
    # test_post_notification_setup_ddb_error_is_non_fatal
    # ------------------------------------------------------------------

    def test_post_notification_setup_ddb_error_is_non_fatal(self):
        """post_notification_setup swallows DynamoDB exceptions."""
        self.mock_table.update_item.side_effect = _make_client_error('ProvisionedThroughputExceededException', 'Throughput exceeded')
        with patch('scheduler_service.get_scheduler_service') as mock_svc:
            mock_svc.return_value = MagicMock()
            # Should not raise
            self.notif.post_notification_setup(
                request_id='req-ddb-fail',
                telegram_message_id=999,
                expires_at=int(time.time()) + 300,
            )

    # ------------------------------------------------------------------
    # test_scheduler_created_with_correct_expiry
    # ------------------------------------------------------------------

    def test_scheduler_created_with_correct_expiry(self):
        """post_notification_setup calls SchedulerService.create_expiry_schedule."""
        expires_at = int(time.time()) + 120

        mock_service = MagicMock()
        with patch('scheduler_service.get_scheduler_service', return_value=mock_service):
            self.notif.post_notification_setup(
                request_id='sched-req-001',
                telegram_message_id=101,
                expires_at=expires_at,
            )

        mock_service.create_expiry_schedule.assert_called_once_with(
            request_id='sched-req-001',
            expires_at=expires_at,
            telegram_message_id=101,
        )

    # ------------------------------------------------------------------
    # test_scheduler_not_created_if_send_fails
    # ------------------------------------------------------------------

    def test_scheduler_not_called_if_send_fails(self):
        """If Telegram send fails, result.ok is False, so post_notification_setup
        should not be invoked by the caller (tested via NotificationResult)."""
        fail_resp = {'ok': False}
        with patch.object(self.notif, '_send_message', return_value=fail_resp),              patch.object(self.notif._telegram, 'send_message_with_entities', return_value=fail_resp):
            result = self.notif.send_approval_request(
                request_id='fail-req-001',
                command='aws s3 ls',
                reason='test',
            )

        assert result.ok is False
        assert result.message_id is None

    # ------------------------------------------------------------------
    # test_scheduler_failure_is_non_fatal
    # ------------------------------------------------------------------

    def test_scheduler_failure_is_non_fatal(self):
        """post_notification_setup does not raise even if SchedulerService fails."""
        mock_service = MagicMock()
        mock_service.create_expiry_schedule.side_effect = _make_client_error('SchedulerServiceException', 'Scheduler API unavailable')

        with patch('scheduler_service.get_scheduler_service', return_value=mock_service):
            # Should NOT raise
            self.notif.post_notification_setup(
                request_id='req-nonfatal',
                telegram_message_id=55,
                expires_at=int(time.time()) + 60,
            )

    # ------------------------------------------------------------------
    # test_scheduler_service_disabled_is_noop
    # ------------------------------------------------------------------

    def test_scheduler_service_disabled_is_noop(self):
        """SchedulerService with enabled=False returns False for create_expiry_schedule."""
        from scheduler_service import SchedulerService
        svc = SchedulerService(enabled=False)
        result = svc.create_expiry_schedule(request_id='req-disabled', expires_at=9999999999)
        assert result is False

    # ------------------------------------------------------------------
    # test_schedule_name_format
    # ------------------------------------------------------------------

    def test_schedule_name_format(self):
        """schedule_name() returns 'bouncer-expire-{request_id}' (truncated to 64)."""
        from scheduler_service import schedule_name
        name = schedule_name('special-req-abc')
        assert name == 'bouncer-expire-special-req-abc'
        assert len(name) <= 64

    # ------------------------------------------------------------------
    # test_scheduler_service_create_calls_boto
    # ------------------------------------------------------------------

    def test_scheduler_service_create_calls_boto(self):
        """SchedulerService.create_expiry_schedule calls boto3 scheduler.create_schedule."""
        mock_client = MagicMock()
        svc_mod = __import__('scheduler_service')
        svc = svc_mod.SchedulerService(
            scheduler_client=mock_client,
            lambda_arn='arn:aws:lambda:us-east-1:123:function:test',
            role_arn='arn:aws:iam::123:role/sched-role',
            group_name='test-group',
            enabled=True,
        )
        result = svc.create_expiry_schedule(
            request_id='boto-test-001',
            expires_at=int(time.time()) + 300,
        )
        assert result is True
        mock_client.create_schedule.assert_called_once()
        call_kwargs = mock_client.create_schedule.call_args[1]
        assert call_kwargs['Name'] == 'bouncer-expire-boto-test-001'
        assert call_kwargs['ActionAfterCompletion'] == 'DELETE'
        assert 'bouncer-scheduler' in call_kwargs['Target']['Input']

    # ------------------------------------------------------------------
    # test_scheduler_service_delete_handles_not_found
    # ------------------------------------------------------------------

    def test_scheduler_service_delete_handles_not_found(self):
        """SchedulerService.delete_schedule returns True when schedule doesn't exist."""
        mock_client = MagicMock()
        # Simulate ResourceNotFoundException
        exc_class = type('ResourceNotFoundException', (Exception,), {})
        mock_client.exceptions = MagicMock()
        mock_client.exceptions.ResourceNotFoundException = exc_class
        mock_client.delete_schedule.side_effect = exc_class("Not found")

        svc_mod = __import__('scheduler_service')
        svc = svc_mod.SchedulerService(
            scheduler_client=mock_client,
            enabled=True,
        )
        result = svc.delete_schedule(request_id='already-gone')
        assert result is True

    # ------------------------------------------------------------------
    # test_scheduler_service_create_failure_returns_false
    # ------------------------------------------------------------------

    def test_scheduler_service_create_failure_returns_false(self):
        """SchedulerService.create_expiry_schedule returns False on boto3 error."""
        mock_client = MagicMock()
        mock_client.create_schedule.side_effect = _make_client_error('AccessDeniedException', 'Access denied')

        svc_mod = __import__('scheduler_service')
        svc = svc_mod.SchedulerService(
            scheduler_client=mock_client,
            lambda_arn='arn:aws:lambda:us-east-1:123:function:test',
            role_arn='arn:aws:iam::123:role/test',
            enabled=True,
        )
        result = svc.create_expiry_schedule(request_id='fail-test', expires_at=9999999999)
        assert result is False
