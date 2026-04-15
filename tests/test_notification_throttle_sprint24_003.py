"""
test_notification_throttle_sprint24_003.py

Tests for notification throttling to prevent spam from consecutive auto-approved commands.
Implements sprint24-003: deduplicate consecutive auto_approved notifications (#78)
"""

import time
import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import pytest
pytestmark = pytest.mark.xdist_group("throttle")

# Add src directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Set up minimal environment variables
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '123456789')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


@pytest.fixture(autouse=True)
def reset_throttle_state():
    """Reset throttle state before each test."""
    import notifications
    notifications._last_notification_time = {}
    yield
    notifications._last_notification_time = {}


@pytest.fixture
def mock_telegram():
    """Mock telegram module for testing."""
    with patch('telegram.send_message_with_entities') as mock_send:
        mock_send.return_value = {'ok': True, 'result': {'message_id': 12345}}
        yield mock_send


class TestNotificationThrottling:
    """Test notification throttling functionality."""

    def test_should_throttle_notification_first_call(self):
        """First call should not be throttled."""
        from notifications import _should_throttle_notification

        result = _should_throttle_notification('auto_approve')
        assert result is False, "First notification should not be throttled"

    def test_should_throttle_notification_rapid_calls(self):
        """Rapid consecutive calls should be throttled."""
        from notifications import _should_throttle_notification

        # First call should not be throttled
        result1 = _should_throttle_notification('auto_approve')
        assert result1 is False

        # Immediate second call should be throttled
        result2 = _should_throttle_notification('auto_approve')
        assert result2 is True, "Second notification within throttle window should be throttled"

    def test_should_throttle_notification_after_window(self):
        """Notification should be sent after throttle window expires."""
        from notifications import _should_throttle_notification, NOTIFICATION_THROTTLE_SECONDS

        # First call
        result1 = _should_throttle_notification('test_type')
        assert result1 is False

        # Immediate second call should be throttled
        result2 = _should_throttle_notification('test_type')
        assert result2 is True

        # Simulate time passing (mock the time)
        import notifications
        notifications._last_notification_time['test_type'] = time.time() - (NOTIFICATION_THROTTLE_SECONDS + 1)

        # After throttle window, should not be throttled
        result3 = _should_throttle_notification('test_type')
        assert result3 is False

    def test_should_throttle_different_types_independent(self):
        """Different notification types should have independent throttling."""
        from notifications import _should_throttle_notification

        # First auto_approve should not be throttled
        result1 = _should_throttle_notification('auto_approve')
        assert result1 is False

        # First trust_approve should not be throttled (different type)
        result2 = _should_throttle_notification('trust_approve')
        assert result2 is False

        # Second auto_approve should be throttled
        result3 = _should_throttle_notification('auto_approve')
        assert result3 is True

        # Second trust_approve should be throttled
        result4 = _should_throttle_notification('trust_approve')
        assert result4 is True

    def test_trust_auto_approve_notification_throttled(self, mock_telegram):
        """Trust auto-approve notification should be throttled on rapid calls."""
        from notifications import send_trust_auto_approve_notification

        # First call should send notification
        send_trust_auto_approve_notification(
            command='aws s3 ls',
            trust_id='trust-123',
            remaining='5:00',
            count=1,
            result='bucket1\nbucket2',
            source='cline',
            reason='list buckets'
        )
        assert mock_telegram.call_count == 1

        # Second immediate call should be throttled (no new notification)
        send_trust_auto_approve_notification(
            command='aws s3 ls s3://bucket1',
            trust_id='trust-123',
            remaining='4:50',
            count=2,
            result='file1.txt\nfile2.txt',
            source='cline',
            reason='list bucket contents'
        )
        assert mock_telegram.call_count == 1, "Second call should be throttled"

    def test_trust_auto_approve_notification_after_throttle_window(self, mock_telegram):
        """Trust auto-approve notification should send after throttle window."""
        from notifications import send_trust_auto_approve_notification, NOTIFICATION_THROTTLE_SECONDS
        import notifications

        # First call
        send_trust_auto_approve_notification(
            command='aws s3 ls',
            trust_id='trust-123',
            remaining='5:00',
            count=1
        )
        assert mock_telegram.call_count == 1

        # Simulate time passing
        notifications._last_notification_time['trust_approve'] = time.time() - (NOTIFICATION_THROTTLE_SECONDS + 1)

        # Second call after window should send
        send_trust_auto_approve_notification(
            command='aws s3 ls',
            trust_id='trust-123',
            remaining='4:00',
            count=2
        )
        assert mock_telegram.call_count == 2

    def test_mcp_execute_auto_approve_throttled(self):
        """Test that auto-approve in mcp_execute respects throttling."""
        from mcp_execute import _check_auto_approve, ExecuteContext
        from paging import PaginatedOutput
        from unittest.mock import MagicMock, patch

        # Mock dependencies
        with patch('mcp_execute.is_auto_approve', return_value=True), \
             patch('mcp_execute.execute_command', return_value='command output'), \
             patch('mcp_execute.store_paged_output', return_value=PaginatedOutput(paged=False, result='output', telegram_pages=1)), \
             patch('mcp_execute.send_telegram_message_silent') as mock_send_tg, \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.generate_request_id', return_value='req-123'):

            # Create context
            ctx = ExecuteContext(
                req_id=1,
                command='aws s3 ls',
                reason='test',
                source='test',
                trust_scope='test-scope',
                context=None,
                account_id='123456789012',
                account_name='test-account',
                assume_role=None,
                timeout=120,
                sync_mode=True,
                smart_decision=None
            )

            # First call should send notification
            _check_auto_approve(ctx)
            assert mock_send_tg.call_count == 1

            # Second immediate call should be throttled
            ctx.command = 'aws s3 ls s3://bucket1'
            _check_auto_approve(ctx)
            assert mock_send_tg.call_count == 1, "Second notification should be throttled"

    def test_throttle_window_configurable(self):
        """Test that throttle window is configurable."""
        from notifications import NOTIFICATION_THROTTLE_SECONDS

        # Default should be 60 seconds
        assert NOTIFICATION_THROTTLE_SECONDS == 60


class TestNotificationThrottlingEdgeCases:
    """Test edge cases for notification throttling."""

    def test_empty_notification_type(self):
        """Test behavior with empty notification type."""
        from notifications import _should_throttle_notification

        result = _should_throttle_notification('')
        assert result is False

    def test_none_notification_type(self):
        """Test behavior with None notification type."""
        from notifications import _should_throttle_notification

        # This should not crash but handle gracefully
        try:
            result = _should_throttle_notification(None)
            # If it doesn't crash, check the result
            assert result is False
        except (TypeError, AttributeError):
            # Acceptable to raise error for invalid input
            pass

    def test_concurrent_notification_types(self):
        """Test multiple notification types don't interfere with each other."""
        from notifications import _should_throttle_notification

        # Rapid calls to different types
        types = ['type1', 'type2', 'type3']
        for t in types:
            result = _should_throttle_notification(t)
            assert result is False, f"First call for {t} should not be throttled"

        # Second round should all be throttled
        for t in types:
            result = _should_throttle_notification(t)
            assert result is True, f"Second call for {t} should be throttled"
