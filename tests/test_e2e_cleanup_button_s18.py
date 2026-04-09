"""End-to-end tests for the CLEANUP button flow (Sprint 18).

Verifies the complete path:
  1. deployer.send_deploy_approval_request() sends a Telegram notification
     and calls post_notification_setup() with the returned message_id
  2. post_notification_setup() stores telegram_message_id in DDB and
     schedules EventBridge expiry trigger via create_expiry_schedule()
  3. handle_cleanup_expired() (triggered by EventBridge) removes the
     inline keyboard from the Telegram message

Two sub-paths are verified:
  A. Normal path -- DDB has item with telegram_message_id
  B. Fallback path -- DDB item not found, fallback to event payload message_id

This test also confirms mcp_deploy_frontend uses the same mechanism.
"""
import json
import sys
import os
import time
import pytest
from unittest.mock import MagicMock, patch, call
import pytest
pytestmark = pytest.mark.xdist_group("e2e_cleanup")


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'  # Must override any existing value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cleanup_event(request_id, telegram_message_id=None):
    """Build an EventBridge cleanup event (as would be sent by SchedulerService)."""
    ev = {
        'source': 'bouncer-scheduler',
        'action': 'cleanup_expired',
        'request_id': request_id,
    }
    if telegram_message_id is not None:
        ev['telegram_message_id'] = telegram_message_id
    return ev


# ---------------------------------------------------------------------------
# E2E Path A: deployer -> post_notification_setup -> CLEANUP (DDB path)
# ---------------------------------------------------------------------------

class TestE2ECleanupNormalPath:
    """Full flow: deployer sends notification -> post_notification_setup stores
    telegram_message_id in DDB -> CLEANUP reads from DDB and removes buttons."""

    def test_deployer_calls_post_notification_setup_with_message_id(self):
        """deployer.send_deploy_approval_request calls post_notification_setup
        with the telegram message_id returned by the Telegram API."""

        FAKE_MESSAGE_ID = 88001
        FAKE_REQUEST_ID = 'req-e2e-deploy-001'
        FAKE_EXPIRES_AT = int(time.time()) + 300

        mock_tg_response = {
            'ok': True,
            'result': {'message_id': FAKE_MESSAGE_ID},
        }

        mock_post_setup = MagicMock()

        with patch('telegram.send_telegram_message', return_value=mock_tg_response), \
             patch('notifications.post_notification_setup', mock_post_setup):
            from deployer import send_deploy_approval_request
            send_deploy_approval_request(
                request_id=FAKE_REQUEST_ID,
                project={
                    'project_id': 'bouncer',
                    'name': 'Bouncer',
                    'stack_name': 'clawdbot-bouncer',
                    'target_account': '190825685292',
                },
                branch='master',
                reason='E2E test',
                source='test-bot',
                expires_at=FAKE_EXPIRES_AT,
            )

        mock_post_setup.assert_called_once_with(
            request_id=FAKE_REQUEST_ID,
            telegram_message_id=FAKE_MESSAGE_ID,
            expires_at=FAKE_EXPIRES_AT,
        )

    def test_post_notification_setup_stores_message_id_in_ddb(self):
        """post_notification_setup persists telegram_message_id in DDB."""

        FAKE_MESSAGE_ID = 88002
        FAKE_REQUEST_ID = 'req-e2e-notify-001'

        mock_table = MagicMock()
        mock_table.update_item.return_value = {}

        mock_svc = MagicMock()

        import notifications as notif_mod
        import scheduler_service as sched_mod

        with patch('db.table', mock_table), \
             patch.object(sched_mod, 'get_scheduler_service', return_value=mock_svc):
            notif_mod.post_notification_setup(
                request_id=FAKE_REQUEST_ID,
                telegram_message_id=FAKE_MESSAGE_ID,
                expires_at=int(time.time()) + 300,
            )

        # DDB update_item must have been called with the correct message_id
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs['Key'] == {'request_id': FAKE_REQUEST_ID}
        assert ':mid' in call_kwargs['ExpressionAttributeValues']
        assert call_kwargs['ExpressionAttributeValues'][':mid'] == FAKE_MESSAGE_ID

    def test_post_notification_setup_schedules_expiry_with_message_id(self):
        """post_notification_setup calls create_expiry_schedule with telegram_message_id."""

        FAKE_MESSAGE_ID = 88003
        FAKE_REQUEST_ID = 'req-e2e-sched-001'
        FAKE_EXPIRES_AT = int(time.time()) + 300

        mock_table = MagicMock()
        mock_svc = MagicMock()

        import notifications as notif_mod
        import scheduler_service as sched_mod

        with patch('db.table', mock_table), \
             patch.object(sched_mod, 'get_scheduler_service', return_value=mock_svc):
            notif_mod.post_notification_setup(
                request_id=FAKE_REQUEST_ID,
                telegram_message_id=FAKE_MESSAGE_ID,
                expires_at=FAKE_EXPIRES_AT,
            )

        mock_svc.create_expiry_schedule.assert_called_once_with(
            request_id=FAKE_REQUEST_ID,
            expires_at=FAKE_EXPIRES_AT,
            telegram_message_id=FAKE_MESSAGE_ID,
        )

    def test_cleanup_removes_buttons_using_ddb_message_id(self):
        """handle_cleanup_expired reads telegram_message_id from DDB and calls
        update_message with remove_buttons=True."""

        FAKE_MESSAGE_ID = 88004
        FAKE_REQUEST_ID = 'req-e2e-cleanup-001'

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'request_id': FAKE_REQUEST_ID,
                'status': 'pending',
                'telegram_message_id': FAKE_MESSAGE_ID,
                'source': 'test-bot',
                'command': 'aws s3 ls',
                'reason': 'e2e test',
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
            result = handle_cleanup_expired(
                _make_cleanup_event(FAKE_REQUEST_ID)
            )

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('cleaned') is True

        mock_update.assert_called_once()
        call_args = mock_update.call_args
        assert call_args[0][0] == FAKE_MESSAGE_ID
        assert call_args[1].get('remove_buttons') is True

    def test_full_e2e_chain_normal_path(self):
        """Full chain: deployer notifies -> post_notification_setup schedules ->
        CLEANUP event fires -> buttons removed.

        Simulates the entire sequence end-to-end, verifying that message_id
        flows correctly through all three layers.
        """
        FAKE_MESSAGE_ID = 88005
        FAKE_REQUEST_ID = 'req-e2e-chain-001'
        FAKE_EXPIRES_AT = int(time.time()) + 300

        # Step 1: deployer sends Telegram notification
        mock_tg_response = {'ok': True, 'result': {'message_id': FAKE_MESSAGE_ID}}
        ddb_stored_message_id = {}

        def capture_post_setup(request_id, telegram_message_id, expires_at):
            """Simulate post_notification_setup storing message_id."""
            ddb_stored_message_id[request_id] = telegram_message_id

        with patch('telegram.send_telegram_message', return_value=mock_tg_response), \
             patch('notifications.post_notification_setup', side_effect=capture_post_setup):
            from deployer import send_deploy_approval_request
            send_deploy_approval_request(
                request_id=FAKE_REQUEST_ID,
                project={'project_id': 'bouncer', 'name': 'Bouncer', 'stack_name': 'x'},
                branch='master',
                reason='e2e chain test',
                source='test-bot',
                expires_at=FAKE_EXPIRES_AT,
            )

        # Verify message_id was captured in post_notification_setup
        assert ddb_stored_message_id.get(FAKE_REQUEST_ID) == FAKE_MESSAGE_ID

        # Step 2: EventBridge fires the cleanup event
        # Simulate DDB returning item with stored message_id
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'request_id': FAKE_REQUEST_ID,
                'status': 'pending',
                'telegram_message_id': ddb_stored_message_id[FAKE_REQUEST_ID],
                'source': 'test-bot',
                'command': 'aws s3 ls',
                'reason': 'e2e chain test',
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
            result = handle_cleanup_expired(
                _make_cleanup_event(FAKE_REQUEST_ID)
            )

        # Verify buttons were cleared with the correct message_id
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('cleaned') is True

        mock_update.assert_called_once()
        update_call_msg_id = mock_update.call_args[0][0]
        assert update_call_msg_id == FAKE_MESSAGE_ID, (
            f"Expected update_message called with message_id={FAKE_MESSAGE_ID}, "
            f"got {update_call_msg_id}"
        )
        assert mock_update.call_args[1].get('remove_buttons') is True


# ---------------------------------------------------------------------------
# E2E Path B: CLEANUP fallback path (DDB item not found, event payload used)
# ---------------------------------------------------------------------------

class TestE2ECleanupFallbackPath:
    """Fallback path: DDB item deleted/expired before CLEANUP fires.
    EventBridge payload contains telegram_message_id as fallback.

    This ensures buttons are still cleared even if the DDB item is gone."""

    def test_scheduler_embeds_message_id_in_eventbridge_payload(self):
        """create_expiry_schedule embeds telegram_message_id in the EventBridge
        target input so CLEANUP can use it as a fallback."""
        from scheduler_service import SchedulerService

        FAKE_MESSAGE_ID = 99001
        FAKE_REQUEST_ID = 'req-e2e-fb-sched-001'

        mock_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_client,
            lambda_arn='arn:aws:lambda:us-east-1:123:function:bouncer',
            role_arn='arn:aws:iam::123:role/sched-role',
            group_name='bouncer-expiry-schedules',
            enabled=True,
        )

        svc.create_expiry_schedule(
            request_id=FAKE_REQUEST_ID,
            expires_at=int(time.time()) + 300,
            telegram_message_id=FAKE_MESSAGE_ID,
        )

        _, kwargs = mock_client.create_schedule.call_args
        payload = json.loads(kwargs['Target']['Input'])
        assert payload['telegram_message_id'] == FAKE_MESSAGE_ID
        assert payload['request_id'] == FAKE_REQUEST_ID
        assert payload['action'] == 'cleanup_expired'

    def test_cleanup_fallback_uses_event_payload_when_ddb_missing(self):
        """When DDB item not found, CLEANUP uses telegram_message_id from
        EventBridge event payload to remove buttons."""

        FAKE_MESSAGE_ID = 99002
        FAKE_REQUEST_ID = 'req-e2e-fb-cleanup-001'

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # Item not found

        # Clear cached app module to ensure correct import in xdist
        import sys
        sys.modules.pop('app', None)

        with patch('app.table', mock_table), \
             patch('app.update_message') as mock_update:
            from app import handle_cleanup_expired
            result = handle_cleanup_expired(
                _make_cleanup_event(FAKE_REQUEST_ID, telegram_message_id=FAKE_MESSAGE_ID)
            )

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['skipped'] is True
        assert body['reason'] == 'not_found'

        # Buttons cleared via fallback
        mock_update.assert_called_once_with(
            FAKE_MESSAGE_ID, "\u23f0 \u6b64\u8acb\u6c42\u5df2\u904e\u671f", remove_buttons=True
        )

    def test_full_e2e_chain_fallback_path(self):
        """Full fallback chain: scheduler embeds message_id in EventBridge payload ->
        CLEANUP fires -> DDB item missing -> fallback clears buttons from payload.

        Simulates the scenario where the DDB item TTL expired before CLEANUP ran.
        """
        FAKE_MESSAGE_ID = 99003
        FAKE_REQUEST_ID = 'req-e2e-fb-chain-001'
        FAKE_EXPIRES_AT = int(time.time()) + 300

        # Step 1: Simulate scheduler embedding message_id in EventBridge payload
        from scheduler_service import SchedulerService

        mock_sched_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_sched_client,
            lambda_arn='arn:aws:lambda:us-east-1:123:function:bouncer',
            role_arn='arn:aws:iam::123:role/r',
            group_name='bouncer-expiry-schedules',
            enabled=True,
        )

        svc.create_expiry_schedule(
            request_id=FAKE_REQUEST_ID,
            expires_at=FAKE_EXPIRES_AT,
            telegram_message_id=FAKE_MESSAGE_ID,
        )

        # Extract the EventBridge payload that would be sent to Lambda
        _, kwargs = mock_sched_client.create_schedule.call_args
        lambda_event = json.loads(kwargs['Target']['Input'])
        assert lambda_event['telegram_message_id'] == FAKE_MESSAGE_ID

        # Step 2: EventBridge fires -> DDB item NOT found (simulating TTL expiry)
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # Item gone

        # Clear cached app module to ensure correct import in xdist
        import sys
        sys.modules.pop('app', None)

        with patch('app.table', mock_table), \
             patch('app.update_message') as mock_update:
            from app import handle_cleanup_expired
            result = handle_cleanup_expired(lambda_event)

        # Step 3: Verify buttons still cleared via fallback
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['ok'] is True

        mock_update.assert_called_once()
        assert mock_update.call_args[0][0] == FAKE_MESSAGE_ID
        assert mock_update.call_args[1].get('remove_buttons') is True


# ---------------------------------------------------------------------------
# E2E: mcp_deploy_frontend also uses the same mechanism
# ---------------------------------------------------------------------------

class TestE2ECleanupDeployFrontendPath:
    """Verify that bouncer_deploy_frontend also calls post_notification_setup,
    ensuring its approval buttons are also cleaned up on expiry."""

    def test_deploy_frontend_calls_post_notification_setup_with_message_id(self):
        """mcp_tool_deploy_frontend calls post_notification_setup after
        successful Telegram notification."""
        import base64

        FAKE_MESSAGE_ID = 77001

        ddb_config = {
            'frontend_bucket': 'test-bucket',
            'distribution_id': 'ETEST123',
            'region': 'us-east-1',
            'deploy_role_arn': 'arn:aws:iam::999:role/test-role',
        }

        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}

        mock_notif = MagicMock()
        mock_notif.ok = True
        mock_notif.message_id = FAKE_MESSAGE_ID

        mock_post_setup = MagicMock()
        mock_dbtable = MagicMock()
        mock_dbtable.put_item.return_value = {}

        files = [
            {
                'filename': 'index.html',
                'content': base64.b64encode(b'<html/>').decode(),
                'content_type': 'text/html',
            }
        ]

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=ddb_config), \
             patch('mcp_deploy_frontend.get_s3_client', return_value=mock_s3), \
             patch('mcp_deploy_frontend.table', mock_dbtable), \
             patch('mcp_deploy_frontend.send_deploy_frontend_notification', return_value=mock_notif), \
             patch('notifications.post_notification_setup', mock_post_setup):
            from mcp_deploy_frontend import mcp_tool_deploy_frontend
            mcp_tool_deploy_frontend('req-e2e-df-001', {
                'project': 'test-project',
                'files': files,
                'reason': 'e2e deploy test',
                'source': 'test-bot',
                'trust_scope': 'test-scope',
            })

        # post_notification_setup must be called with the message_id
        mock_post_setup.assert_called_once()
        call_kwargs = mock_post_setup.call_args[1]
        assert call_kwargs['telegram_message_id'] == FAKE_MESSAGE_ID
