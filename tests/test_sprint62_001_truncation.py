"""
Tests for sprint62-001: Fix command output silently truncated (issue #168)

Covers:
  - Path 1: _format_approval_response shows truncation notice when output is truncated but not paged
  - Path 2: send_trust_auto_approve_notification handles outputs > 4096 chars (Telegram limit)
  - Path 3: send_grant_execute_notification handles outputs > 4096 chars (Telegram limit)
"""
import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


# ---------------------------------------------------------------------------
# Path 1: _format_approval_response truncation notice
# ---------------------------------------------------------------------------

class TestFormatApprovalResponseTruncationNotice:
    """Test that _format_approval_response shows truncation notice for non-paged truncated output."""

    def _make_info(self):
        return {
            'source_line': '',
            'account_line': '',
            'safe_reason': 'test reason',
            'cmd_preview': 'aws s3 ls',
        }

    def test_truncation_notice_shown_when_output_exceeds_max_preview_not_paged(self):
        """When len(result) > max_preview but paged=False, truncation notice should appear."""
        # Create output that exceeds max_preview (1000 chars for approve action)
        # but doesn't exceed 4000 chars (which would trigger pagination)
        long_output = 'x' * 1500

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message'):
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve',
                result=long_output,
                paged={'paged': False},  # Not paged
                trust_line='',
                request_id='req-001',
                info=self._make_info(),
                message_id=123,
            )

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        # Should contain truncation notice
        assert '✂️' in sent_text
        assert '輸出已截斷' in sent_text
        assert '顯示前 1000 字元' in sent_text
        assert f'共 {len(long_output)} 字元' in sent_text
        assert 'bouncer_get_page' in sent_text

    def test_truncation_notice_for_approve_trust_action(self):
        """For approve_trust action, max_preview is 800 chars."""
        # Create output that exceeds 800 but not 4000
        long_output = 'y' * 1200

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message'):
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve_trust',
                result=long_output,
                paged={'paged': False},
                trust_line='🔓 信任 10 分鐘',
                request_id='req-002',
                info=self._make_info(),
                message_id=124,
            )

        sent_text = mock_send.call_args[0][0]
        # Should show truncation notice with 800 chars
        assert '✂️' in sent_text
        assert '顯示前 800 字元' in sent_text

    def test_no_truncation_notice_when_output_fits_within_max_preview(self):
        """When len(result) <= max_preview, no truncation notice."""
        short_output = 'x' * 500

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message'):
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve',
                result=short_output,
                paged={'paged': False},
                trust_line='',
                request_id='req-003',
                info=self._make_info(),
                message_id=125,
            )

        sent_text = mock_send.call_args[0][0]
        # Should NOT contain truncation notice
        assert '✂️' not in sent_text
        assert '輸出已截斷' not in sent_text

    def test_paged_output_shows_paging_notice_not_truncation_notice(self):
        """When paged=True, show pagination notice instead of truncation notice."""
        long_output = 'x' * 5000

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message'):
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve',
                result=long_output,
                paged={'paged': True, 'total_pages': 3, 'output_length': 5000},
                trust_line='',
                request_id='req-004',
                info=self._make_info(),
                message_id=126,
            )

        sent_text = mock_send.call_args[0][0]
        # Should show pagination notice, not truncation notice
        assert '📄' in sent_text
        assert '共 3 頁' in sent_text
        # Should NOT show the scissors truncation notice
        assert '✂️' not in sent_text


# ---------------------------------------------------------------------------
# Path 2: send_trust_auto_approve_notification 4096 char limit
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_telegram_send():
    """Mock send_message_with_entities for trust/grant notification tests."""
    with patch('telegram.send_message_with_entities') as mock:
        mock.return_value = {'ok': True, 'result': {'message_id': 99999}}
        yield mock


class TestTrustAutoApproveNotificationTruncation:
    """Test that send_trust_auto_approve_notification handles 4096 char limit."""

    def test_notification_sent_when_output_under_4096_chars(self, mock_telegram_send):
        """Normal output under 4096 chars should be sent as-is."""
        from notifications import send_trust_auto_approve_notification

        short_result = 'x' * 500
        send_trust_auto_approve_notification(
            command='aws s3 ls',
            trust_id='trust-001',
            remaining='9:45',
            count=1,
            result=short_result,
            source='cli',
            reason='Test',
        )

        mock_telegram_send.assert_called_once()
        sent_text = mock_telegram_send.call_args[0][0]
        assert len(sent_text) <= 4096
        assert '已截斷' not in sent_text

    def test_notification_truncated_when_output_exceeds_4096_chars(self, mock_telegram_send):
        """Output > 4096 chars should be truncated with notice."""
        from notifications import send_trust_auto_approve_notification

        # Create a very long result that will exceed 4096 when combined with header
        very_long_result = 'x' * 5000
        send_trust_auto_approve_notification(
            command='aws s3 ls',
            trust_id='trust-002',
            remaining='9:45',
            count=2,
            result=very_long_result,
            source='cli',
            reason='Test',
        )

        mock_telegram_send.assert_called_once()
        sent_text = mock_telegram_send.call_args[0][0]
        # Text should be truncated to fit within 4096
        assert len(sent_text) <= 4096
        # Should contain truncation notice
        assert '已截斷' in sent_text
        assert 'Telegram 4096 字元限制' in sent_text

    def test_notification_without_result_works_normally(self, mock_telegram_send):
        """Notification without result should work (result=None)."""
        from notifications import send_trust_auto_approve_notification

        send_trust_auto_approve_notification(
            command='aws s3 ls',
            trust_id='trust-003',
            remaining='9:45',
            count=3,
            result=None,
            source='cli',
            reason='Test',
        )

        mock_telegram_send.assert_called_once()
        sent_text = mock_telegram_send.call_args[0][0]
        assert len(sent_text) <= 4096


# ---------------------------------------------------------------------------
# Path 3: send_grant_execute_notification 4096 char limit
# ---------------------------------------------------------------------------

class TestGrantExecuteNotificationTruncation:
    """Test that send_grant_execute_notification handles 4096 char limit."""

    def test_notification_sent_when_output_under_4096_chars(self, mock_telegram_send):
        """Normal output under 4096 chars should be sent as-is."""
        from notifications import send_grant_execute_notification

        short_result = 'y' * 500
        send_grant_execute_notification(
            command='aws ec2 describe-instances',
            grant_id='grant-001',
            result=short_result,
            remaining_info='2/5 命令, 28:30',
        )

        mock_telegram_send.assert_called_once()
        sent_text = mock_telegram_send.call_args[0][0]
        assert len(sent_text) <= 4096
        assert '已截斷' not in sent_text

    def test_notification_truncated_when_output_exceeds_4096_chars(self, mock_telegram_send):
        """Output > 4096 chars should be truncated with notice."""
        from notifications import send_grant_execute_notification

        # Create a very long result
        very_long_result = 'z' * 5000
        send_grant_execute_notification(
            command='aws ec2 describe-instances',
            grant_id='grant-002',
            result=very_long_result,
            remaining_info='3/5 命令, 25:00',
        )

        mock_telegram_send.assert_called_once()
        sent_text = mock_telegram_send.call_args[0][0]
        # Text should be truncated to fit within 4096
        assert len(sent_text) <= 4096
        # Should contain truncation notice
        assert '已截斷' in sent_text
        assert 'Telegram 4096 字元限制' in sent_text

    def test_empty_result_handled_correctly(self, mock_telegram_send):
        """Empty result should work without errors."""
        from notifications import send_grant_execute_notification

        send_grant_execute_notification(
            command='aws s3 ls',
            grant_id='grant-003',
            result='',
            remaining_info='1/3 命令, 29:55',
        )

        mock_telegram_send.assert_called_once()
        sent_text = mock_telegram_send.call_args[0][0]
        assert len(sent_text) <= 4096
