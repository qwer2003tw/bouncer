"""Regression tests for sprint12-003: CLEANUP handler message_id fallback.

Covers:
- create_expiry_schedule() passes telegram_message_id in EventBridge payload
- handle_cleanup_expired() falls back to event payload when DDB record not found
- Normal path (DDB has message_id) is unaffected
- post_notification_setup() passes telegram_message_id to scheduler
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================================
# Tests: SchedulerService.create_expiry_schedule — telegram_message_id payload
# ============================================================================

class TestCreateExpirySchedulePayload:
    """Verify that create_expiry_schedule embeds telegram_message_id in payload."""

    def _make_service(self):
        from scheduler_service import SchedulerService
        mock_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_client,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:bouncer',
            role_arn='arn:aws:iam::123456789012:role/SchedulerRole',
            group_name='bouncer-expiry-schedules',
            enabled=True,
        )
        return svc, mock_client

    def test_payload_without_message_id(self):
        """Without telegram_message_id, payload only has source/action/request_id."""
        svc, mock_client = self._make_service()
        svc.create_expiry_schedule(request_id='req-abc', expires_at=9999999999)

        _args, kwargs = mock_client.create_schedule.call_args
        input_payload = json.loads(kwargs['Target']['Input'])
        assert input_payload == {
            'source': 'bouncer-scheduler',
            'action': 'cleanup_expired',
            'request_id': 'req-abc',
        }
        assert 'telegram_message_id' not in input_payload
        assert 'chat_id' not in input_payload

    def test_payload_with_message_id(self):
        """With telegram_message_id, payload includes it as fallback."""
        svc, mock_client = self._make_service()
        svc.create_expiry_schedule(
            request_id='req-abc',
            expires_at=9999999999,
            telegram_message_id=12345,
        )

        _args, kwargs = mock_client.create_schedule.call_args
        input_payload = json.loads(kwargs['Target']['Input'])
        assert input_payload['telegram_message_id'] == 12345
        assert input_payload['request_id'] == 'req-abc'
        assert 'chat_id' not in input_payload

    def test_payload_with_message_id_and_chat_id(self):
        """With both telegram_message_id and chat_id."""
        svc, mock_client = self._make_service()
        svc.create_expiry_schedule(
            request_id='req-xyz',
            expires_at=9999999999,
            telegram_message_id=99999,
            chat_id=-100123456789,
        )

        _args, kwargs = mock_client.create_schedule.call_args
        input_payload = json.loads(kwargs['Target']['Input'])
        assert input_payload['telegram_message_id'] == 99999
        assert input_payload['chat_id'] == -100123456789

    def test_returns_true_on_success(self):
        svc, mock_client = self._make_service()
        result = svc.create_expiry_schedule(
            request_id='req-ok', expires_at=9999999999, telegram_message_id=111
        )
        assert result is True

    def test_disabled_scheduler_returns_false(self):
        from scheduler_service import SchedulerService
        mock_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_client,
            lambda_arn='arn:aws:lambda:us-east-1:123:function:x',
            role_arn='arn:aws:iam::123:role/r',
            enabled=False,
        )
        result = svc.create_expiry_schedule(
            request_id='req-disabled', expires_at=9999999999, telegram_message_id=111
        )
        assert result is False
        mock_client.create_schedule.assert_not_called()


# ============================================================================
# Tests: handle_cleanup_expired — fallback when DDB record not found
# ============================================================================

class TestHandleCleanupExpiredFallback:
    """Verify the fallback path in handle_cleanup_expired when DDB has no record."""

    def _make_event(self, request_id='req-missing', telegram_message_id=None):
        ev = {
            'source': 'bouncer-scheduler',
            'action': 'cleanup_expired',
            'request_id': request_id,
        }
        if telegram_message_id is not None:
            ev['telegram_message_id'] = telegram_message_id
        return ev

    def test_no_item_no_fallback_message_id(self):
        """DDB returns None, no telegram_message_id in event => skip silently."""
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no 'Item' key = not found

        # Ensure src/app.py is imported (xdist isolation fix)
        import sys
        import os
        src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
        if src_path in sys.path:
            sys.path.remove(src_path)
        sys.path.insert(0, src_path)
        if 'app' in sys.modules:
            app_file = getattr(sys.modules['app'], '__file__', '')
            if 'deployer' in app_file:
                del sys.modules['app']
                import app  # Re-import from src/

        with patch('app.table', mock_table), \
             patch('app.update_message') as mock_update:
            from app import handle_cleanup_expired
            result = handle_cleanup_expired(self._make_event())

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['skipped'] is True
        assert body['reason'] == 'not_found'
        mock_update.assert_not_called()

    def test_no_item_with_fallback_message_id(self):
        """DDB returns None, but event has telegram_message_id => clear buttons."""
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}

        # Ensure src/app.py is imported (xdist isolation fix)
        import sys
        import os
        src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
        if src_path in sys.path:
            sys.path.remove(src_path)
        sys.path.insert(0, src_path)
        if 'app' in sys.modules:
            app_file = getattr(sys.modules['app'], '__file__', '')
            if 'deployer' in app_file:
                del sys.modules['app']
                import app  # Re-import from src/

        with patch('app.table', mock_table), \
             patch('app.update_message') as mock_update:
            from app import handle_cleanup_expired
            result = handle_cleanup_expired(
                self._make_event(telegram_message_id=55555)
            )

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['skipped'] is True
        assert body['reason'] == 'not_found'
        mock_update.assert_called_once_with(55555, "⏰ 此請求已過期", remove_buttons=True)

    def test_no_item_fallback_message_update_failure_is_graceful(self):
        """Fallback update_message failure is swallowed, still returns ok."""
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}

        # Ensure src/app.py is imported (xdist isolation fix)
        import sys
        import os
        src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
        if src_path in sys.path:
            sys.path.remove(src_path)
        sys.path.insert(0, src_path)
        if 'app' in sys.modules:
            app_file = getattr(sys.modules['app'], '__file__', '')
            if 'deployer' in app_file:
                del sys.modules['app']
                import app  # Re-import from src/

        with patch('app.table', mock_table), \
             patch('app.update_message', side_effect=Exception("Telegram 500")):
            from app import handle_cleanup_expired
            result = handle_cleanup_expired(
                self._make_event(telegram_message_id=55555)
            )

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['ok'] is True

    def test_item_exists_normal_path_unaffected(self):
        """When DDB has record with message_id, normal flow proceeds unchanged."""
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'request_id': 'req-found',
                'status': 'pending',
                'telegram_message_id': 77777,
                'source': 'test',
                'command': 'aws s3 ls',
                'reason': 'testing',
                'context': '',
                'action': 'execute',
            }
        }

        # Ensure src/app.py is imported (xdist isolation fix)
        import sys
        import os
        src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
        if src_path in sys.path:
            sys.path.remove(src_path)
        sys.path.insert(0, src_path)
        if 'app' in sys.modules:
            app_file = getattr(sys.modules['app'], '__file__', '')
            if 'deployer' in app_file:
                del sys.modules['app']
                import app  # Re-import from src/

        with patch('app.table', mock_table), \
             patch('app.update_message') as mock_update, \
             patch('app.emit_metric'):
            from app import handle_cleanup_expired
            result = handle_cleanup_expired(self._make_event(request_id='req-found'))

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('cleaned') is True
        # update_message called with DDB message_id
        assert mock_update.call_args[0][0] == 77777


# ============================================================================
# Tests: post_notification_setup — passes telegram_message_id to scheduler
# ============================================================================

class TestPostNotificationSetupScheduler:
    """Verify post_notification_setup passes telegram_message_id to create_expiry_schedule."""

    def test_scheduler_called_with_message_id(self):
        """create_expiry_schedule must receive telegram_message_id kwarg."""
        mock_svc = MagicMock()
        mock_dbtable = MagicMock()
        mock_dbtable.update_item.return_value = {}

        import notifications as notif_mod

        # The function does 'from scheduler_service import get_scheduler_service' locally,
        # so we need to patch the scheduler_service module directly.
        import scheduler_service as sched_mod
        with patch.object(sched_mod, 'get_scheduler_service', return_value=mock_svc), \
             patch('db.table', mock_dbtable):
            notif_mod.post_notification_setup(
                request_id='req-test',
                telegram_message_id=42000,
                expires_at=9999999999,
            )

        mock_svc.create_expiry_schedule.assert_called_once_with(
            request_id='req-test',
            expires_at=9999999999,
            telegram_message_id=42000,
        )

    def test_scheduler_still_called_when_message_id_is_zero(self):
        """message_id=0: create_expiry_schedule still invoked (telegram_message_id=0 is passed)."""
        mock_svc = MagicMock()
        mock_dbtable = MagicMock()
        mock_dbtable.update_item.return_value = {}

        import notifications as notif_mod
        import scheduler_service as sched_mod

        with patch.object(sched_mod, 'get_scheduler_service', return_value=mock_svc), \
             patch('db.table', mock_dbtable):
            notif_mod.post_notification_setup(
                request_id='req-test2',
                telegram_message_id=0,
                expires_at=9999999999,
            )

        # Schedule should still be created even with message_id=0
        mock_svc.create_expiry_schedule.assert_called_once()
        call_kwargs = mock_svc.create_expiry_schedule.call_args[1]
        assert call_kwargs['telegram_message_id'] == 0
