"""
Regression tests for sprint12-003: CLEANUP handler message_id fallback.
When DDB record not found, use telegram_message_id from event payload to clear buttons.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

class TestCleanupFallback:
    """Test CLEANUP handler fallback when DDB record not found."""

    def _make_event(self, request_id='req-test', message_id=None):
        e = {'source': 'bouncer-scheduler', 'action': 'cleanup_expired', 'request_id': request_id}
        if message_id:
            e['telegram_message_id'] = message_id
        return e

    def test_fallback_clears_buttons_when_record_not_found(self):
        """When DDB returns None and event has message_id, buttons should be cleared."""
        import app
        with patch.object(app, 'table') as mock_table, \
             patch('app.update_message') as mock_update:
            mock_table.get_item.return_value = {}  # no Item
            app.handle_cleanup_expired(self._make_event(message_id=1234))
            mock_update.assert_called_once()
            call_kwargs = mock_update.call_args
            assert call_kwargs[1].get('remove_buttons') is True or \
                   (len(call_kwargs[0]) >= 2 and call_kwargs[0][0] == 1234)

    def test_no_fallback_when_no_message_id_in_event(self):
        """When DDB returns None and event has no message_id, skip silently."""
        import app
        with patch.object(app, 'table') as mock_table, \
             patch('app.update_message') as mock_update:
            mock_table.get_item.return_value = {}
            app.handle_cleanup_expired(self._make_event(message_id=None))
            mock_update.assert_not_called()

    def test_normal_cleanup_still_works_for_pending(self):
        """When DDB record exists and status=pending, normal cleanup should run."""
        import app
        import time
        with patch.object(app, 'table') as mock_table, \
             patch('app.update_message') as mock_update, \
             patch('app._mark_request_timeout'):
            mock_table.get_item.return_value = {
                'Item': {
                    'request_id': 'req-1',
                    'status': 'pending',
                    'telegram_message_id': 5678,
                    'ttl': int(time.time()) - 10,
                    'source': 'test',
                    'command': 'aws s3 ls',
                    'reason': 'test',
                }
            }
            app.handle_cleanup_expired(self._make_event('req-1', message_id=5678))
            mock_update.assert_called()
