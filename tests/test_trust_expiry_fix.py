"""
test_trust_expiry_fix.py — Sprint 9-008

覆蓋：_send_trust_expiry_notification 的 cmd_preview fallback 行為
  - 有 command 欄位 → 顯示 command
  - 無 command，有 display_summary → 顯示 display_summary
  - 無 command 也無 display_summary，有 action → 顯示 action
  - 三者皆無 → 顯示 'unknown action'
"""

import sys
import os
import time
import pytest
import importlib
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Sprint 58 s58-001: Use centralized module list from _module_list (not conftest — xdist compat)
from _module_list import BOUNCER_MODS


# ===========================================================================
# Direct unit-test of _send_trust_expiry_notification
# ===========================================================================

@pytest.fixture
def app_mod():
    # Sprint 58 s58-001: Use centralized BOUNCER_MODS from conftest
    for mod in list(sys.modules.keys()):
        if mod in BOUNCER_MODS:
            del sys.modules[mod]
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('TABLE_NAME', 'bouncer-test-008')
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
    os.environ.setdefault('APPROVED_CHAT_ID', '999999999')
    os.environ.setdefault('REQUEST_SECRET', 'test-secret')
    os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')

    with patch('db.boto3'):
        import app
        mock_table = MagicMock()
        app.table = mock_table
        yield app


@pytest.fixture(autouse=True)
def fresh_telegram_trust():
    import telegram as tg
    importlib.reload(tg)
    sys.modules['telegram'] = tg
    yield


class TestCmdPreviewFallback:
    """Test the cmd_preview fallback chain in _send_trust_expiry_notification."""

    def _call_send_notification(self, app_mod, pending_requests):
        """Call _send_trust_expiry_notification directly."""
        with patch('app.send_telegram_message_silent') as mock_silent, \
             patch('app.send_telegram_message') as mock_msg:
            app_mod._send_trust_expiry_notification(
                trust_id='trust-test-001',
                source='Test Bot',
                trust_scope='test-scope',
                pending_count=len(pending_requests),
                pending_requests=pending_requests,
            )
            if mock_msg.called:
                return mock_msg.call_args[0][0]
            if mock_silent.called:
                return mock_silent.call_args[0][0]
            return ''

    def test_command_field_used_when_present(self, app_mod):
        """When 'command' is present, it should be displayed."""
        items = [{
            'request_id': 'req-001',
            'command': 'aws s3 ls',
            'display_summary': 'should not appear',
            'action': 'should not appear',
        }]
        msg = self._call_send_notification(app_mod, items)
        assert 'aws s3 ls' in msg

    def test_display_summary_used_when_no_command(self, app_mod):
        """When 'command' is absent, fall back to display_summary."""
        items = [{
            'request_id': 'req-002',
            'display_summary': 'Upload: myfile.zip',
            'action': 'should not appear',
        }]
        msg = self._call_send_notification(app_mod, items)
        assert 'Upload: myfile.zip' in msg

    def test_action_used_when_no_command_no_display_summary(self, app_mod):
        """When both 'command' and 'display_summary' absent, fall back to 'action'."""
        items = [{
            'request_id': 'req-003',
            'action': 'presigned_upload',
        }]
        msg = self._call_send_notification(app_mod, items)
        assert 'presigned' in msg and 'upload' in msg

    def test_unknown_action_when_all_absent(self, app_mod):
        """When all fields absent, show 'unknown action'."""
        items = [{
            'request_id': 'req-004',
        }]
        msg = self._call_send_notification(app_mod, items)
        assert 'unknown action' in msg

    def test_empty_command_falls_through_to_display_summary(self, app_mod):
        """Empty string command should be treated as falsy and fall through."""
        items = [{
            'request_id': 'req-005',
            'command': '',
            'display_summary': 'Grant session: batch upload',
        }]
        msg = self._call_send_notification(app_mod, items)
        assert 'Grant session: batch upload' in msg

    def test_multiple_items_correct_fallback_per_item(self, app_mod):
        """Different items use different fallback levels correctly."""
        items = [
            {
                'request_id': 'req-006',
                'command': 'aws ec2 describe-instances',
            },
            {
                'request_id': 'req-007',
                'display_summary': 'Presigned upload: logo.png',
            },
        ]
        msg = self._call_send_notification(app_mod, items)
        assert 'aws ec2 describe-instances' in msg
        assert 'Presigned upload: logo.png' in msg
