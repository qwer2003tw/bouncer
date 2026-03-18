"""
test_trust_expiry_ring.py — Sprint 11-010

Regression: _send_trust_expiry_notification should ring (send_telegram_message)
when pending_count > 0, and send silently (send_telegram_message_silent)
when pending_count == 0.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Sprint 58 s58-001: Use centralized module list from conftest
from conftest import BOUNCER_MODS


@pytest.fixture
def app_mod():
    # Sprint 58 s58-001: Use centralized BOUNCER_MODS from conftest
    for mod in list(sys.modules.keys()):
        if mod in BOUNCER_MODS:
            del sys.modules[mod]
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('TABLE_NAME', 'bouncer-test-010')
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
    os.environ.setdefault('APPROVED_CHAT_ID', '999999999')
    os.environ.setdefault('REQUEST_SECRET', 'test-secret')
    os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')

    with patch('db.boto3'):
        import app
        mock_table = MagicMock()
        app.table = mock_table
        yield app


class TestTrustExpiryRing:
    """Trust expiry notification should ring when pending requests exist."""

    def test_no_pending_sends_silent(self, app_mod):
        """With 0 pending requests, should use send_telegram_message_silent."""
        with patch('telegram.send_telegram_message_silent') as mock_silent, \
             patch('telegram.send_telegram_message') as mock_ring:
            app_mod._send_trust_expiry_notification(
                trust_id='trust-no-pending',
                source='Test Bot',
                trust_scope='test-scope',
                pending_count=0,
                pending_requests=[],
            )
            mock_silent.assert_called_once()
            mock_ring.assert_not_called()

    def test_with_pending_sends_ring(self, app_mod):
        """With pending requests, should use send_telegram_message (rings)."""
        with patch('telegram.send_telegram_message_silent') as mock_silent, \
             patch('telegram.send_telegram_message') as mock_ring:
            pending = [
                {'request_id': 'req-001', 'command': 'aws s3 ls'},
            ]
            app_mod._send_trust_expiry_notification(
                trust_id='trust-with-pending',
                source='Test Bot',
                trust_scope='test-scope',
                pending_count=1,
                pending_requests=pending,
            )
            mock_ring.assert_called_once()
            mock_silent.assert_not_called()

    def test_multiple_pending_sends_ring(self, app_mod):
        """With multiple pending requests, should still ring."""
        with patch('telegram.send_telegram_message_silent') as mock_silent, \
             patch('telegram.send_telegram_message') as mock_ring:
            pending = [
                {'request_id': f'req-{i:03d}', 'command': f'aws s3 ls {i}'}
                for i in range(7)
            ]
            app_mod._send_trust_expiry_notification(
                trust_id='trust-many-pending',
                source='Test Bot',
                trust_scope='test-scope',
                pending_count=7,
                pending_requests=pending,
            )
            mock_ring.assert_called_once()
            mock_silent.assert_not_called()

    def test_pending_message_contains_count(self, app_mod):
        """Notification text should mention the pending count."""
        with patch('telegram.send_telegram_message') as mock_ring, \
             patch('telegram.send_telegram_message_silent'):
            pending = [
                {'request_id': 'req-001', 'command': 'aws ec2 describe-instances'},
                {'request_id': 'req-002', 'command': 'aws s3 ls'},
            ]
            app_mod._send_trust_expiry_notification(
                trust_id='trust-count-check',
                source='Test Bot',
                trust_scope='test-scope',
                pending_count=2,
                pending_requests=pending,
            )
            assert mock_ring.called
            text = mock_ring.call_args[0][0]
            assert '2' in text

    def test_no_pending_message_indicates_no_action(self, app_mod):
        """Silent notification text should indicate no pending requests."""
        with patch('telegram.send_telegram_message_silent') as mock_silent, \
             patch('telegram.send_telegram_message'):
            app_mod._send_trust_expiry_notification(
                trust_id='trust-silent-check',
                source='Test Bot',
                trust_scope='test-scope',
                pending_count=0,
                pending_requests=[],
            )
            assert mock_silent.called
            text = mock_silent.call_args[0][0]
            # Should mention no pending
            assert 'pending' in text.lower() or '無' in text

    def test_pending_count_1_triggers_ring(self, app_mod):
        """Boundary: exactly 1 pending should trigger ring (not silent)."""
        with patch('telegram.send_telegram_message_silent') as mock_silent, \
             patch('telegram.send_telegram_message') as mock_ring:
            app_mod._send_trust_expiry_notification(
                trust_id='trust-boundary',
                source='Boundary Test',
                trust_scope='boundary-scope',
                pending_count=1,
                pending_requests=[{'request_id': 'req-x', 'command': 'aws sts get-caller-identity'}],
            )
            mock_ring.assert_called_once()
            mock_silent.assert_not_called()

    def test_trust_id_in_notification_text(self, app_mod):
        """Trust ID should appear in the notification text."""
        with patch('telegram.send_telegram_message_silent') as mock_silent, \
             patch('telegram.send_telegram_message'):
            trust_id = 'trust-abc123xyz'
            app_mod._send_trust_expiry_notification(
                trust_id=trust_id,
                source='Test',
                trust_scope='scope',
                pending_count=0,
                pending_requests=[],
            )
            text = mock_silent.call_args[0][0]
            assert trust_id in text


if __name__ == '__main__':
    import pytest as _pytest
    _pytest.main([__file__, '-v'])
