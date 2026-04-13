"""
test_sprint31_003_deploy_pin.py — Deploy Pin / Notifier Progress Msg (Sprint 31-003)

Tests:
  1. handle_deploy_callback (approve) → calls pin_message(message_id) best-effort
  2. handle_deploy_callback pin failure → deploy proceeds, warning logged
  3. Notifier handle_start with existing telegram_message_id → updates existing msg, does NOT pin (pin already done by callbacks.py on approve)
  4. Notifier handle_start without existing telegram_message_id → sends new msg, does NOT pin
  5. Notifier handle_success → updates existing message + unpins
"""
import json
import sys
import os
import time
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ---------------------------------------------------------------------------
# Feature 003 — T001: callbacks.py pin_message after approve
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.xdist_group("test_sprint31_003_deploy_pin")


class TestCallbacksDeployPin:
    """handle_deploy_callback (approve) calls pin_message after update_message."""

    def test_pin_message_called_on_deploy_approve(self, app_module):
        """pin_message must be called after 部署已啟動 update_message."""
        import callbacks
        import deployer

        with patch.object(deployer, 'start_deploy') as mock_start, \
             patch.object(callbacks, 'update_message'), \
             patch.object(callbacks, 'answer_callback'), \
             patch.object(deployer, 'update_deploy_record'), \
             patch('callbacks.pin_message') as mock_pin:

            mock_start.return_value = {
                'status': 'started',
                'deploy_id': 'deploy-s31-001',
                'commit_short': 'abc1234',
                'commit_message': 'feat: test',
            }
            mock_pin.return_value = True

            callbacks.handle_deploy_callback(
                action='approve',
                request_id='req-s31-pin-001',
                item={
                    'project_id': 'test-project',
                    'project_name': 'Test Project',
                    'branch': 'main',
                    'stack_name': 'test-stack',
                    'source': 'mcp',
                    'reason': 'sprint31 test',
                    'context': '',
                },
                message_id=11111,
                callback_id='cb-s31-001',
                user_id='user123',
            )

            mock_pin.assert_called_once_with(11111)

    def test_pin_failure_does_not_block_deploy(self, app_module):
        """pin_message failure must be caught; deploy still records telegram_message_id."""
        import callbacks
        import deployer

        with patch.object(deployer, 'start_deploy') as mock_start, \
             patch.object(callbacks, 'update_message'), \
             patch.object(callbacks, 'answer_callback'), \
             patch.object(deployer, 'update_deploy_record') as mock_update_record, \
                          patch('callbacks.pin_message') as mock_pin:

            mock_start.return_value = {
                'status': 'started',
                'deploy_id': 'deploy-s31-002',
            }
            mock_pin.side_effect = Exception('Bot lacks admin rights')

            # Must not raise
            result = callbacks.handle_deploy_callback(
                action='approve',
                request_id='req-s31-pin-002',
                item={
                    'project_id': 'test-project',
                    'project_name': 'Test Project',
                    'branch': 'main',
                    'stack_name': 'test-stack',
                    'source': 'mcp',
                    'reason': 'sprint31 pin fail test',
                    'context': '',
                },
                message_id=22222,
                callback_id='cb-s31-002',
                user_id='user123',
            )

            # Deploy record must still be updated
            assert mock_update_record.called
            assert result is not None


# ---------------------------------------------------------------------------
# Feature 003 — T002: Notifier Lambda handle_start / handle_success
# ---------------------------------------------------------------------------

class TestNotifierHandleStart:
    """Notifier handle_start: use existing telegram_message_id when available."""

    def _make_event(self, deploy_id='deploy-notifier-001', project_id='test-project', branch='main'):
        return {'action': 'start', 'deploy_id': deploy_id, 'project_id': project_id, 'branch': branch}

    def test_handle_start_updates_existing_message(self):
        """When DDB has telegram_message_id, handle_start updates it (not sends new)."""
        import importlib, types

        # Patch notifier app at module level
        notifier_path = os.path.join(os.path.dirname(__file__), '..', 'deployer', 'notifier', 'app.py')
        spec = importlib.util.spec_from_file_location('notifier_app', notifier_path)
        notifier = importlib.util.module_from_spec(spec)

        # Set env vars before exec
        notifier.TELEGRAM_BOT_TOKEN = 'test-token'
        notifier.TELEGRAM_CHAT_ID = '-100999'
        notifier.MESSAGE_THREAD_ID = ''
        notifier.HISTORY_TABLE = 'test-history'
        notifier.LOCKS_TABLE = 'test-locks'

        # Inject mock boto3 resources before exec
        mock_history_table = MagicMock()
        mock_history_table.get_item.return_value = {'Item': {'telegram_message_id': 55555}}
        mock_locks_table = MagicMock()

        notifier.history_table = mock_history_table
        notifier.locks_table = mock_locks_table

        spec.loader.exec_module(notifier)

        # Re-assign tables after exec (exec_module resets them)
        notifier.history_table = mock_history_table
        notifier.locks_table = mock_locks_table
        notifier.TELEGRAM_BOT_TOKEN = 'test-token'
        notifier.TELEGRAM_CHAT_ID = '-100999'
        notifier.MESSAGE_THREAD_ID = ''

        update_calls = []
        send_calls = []
        pin_calls = []

        notifier.update_telegram_message = lambda msg_id, text: update_calls.append(msg_id)
        notifier.send_telegram_message = lambda text: send_calls.append(text) or 99999
        notifier.pin_telegram_message = lambda msg_id: pin_calls.append(msg_id)
        notifier.update_history = MagicMock()
        notifier.get_history = lambda deploy_id: {'telegram_message_id': 55555}

        result = notifier.handle_start(self._make_event())

        # Must update existing message, not send new
        assert 55555 in update_calls, "Expected update_telegram_message to be called with existing message_id"
        assert len(send_calls) == 0, "Must not send new message when existing message_id is available"
        # Must NOT pin — callbacks.py already pinned on approve; double-pin is a regression (Issue #119)
        assert len(pin_calls) == 0, "handle_start must NOT call pin_telegram_message (callbacks.py pins on approve)"
        assert result['message_id'] == 55555

    def test_handle_start_sends_new_when_no_existing_message(self):
        """When DDB has no telegram_message_id, handle_start sends a new message."""
        import importlib

        notifier_path = os.path.join(os.path.dirname(__file__), '..', 'deployer', 'notifier', 'app.py')
        spec = importlib.util.spec_from_file_location('notifier_app_2', notifier_path)
        notifier = importlib.util.module_from_spec(spec)

        mock_history_table = MagicMock()
        mock_history_table.get_item.return_value = {'Item': {}}
        mock_locks_table = MagicMock()
        notifier.history_table = mock_history_table
        notifier.locks_table = mock_locks_table
        notifier.TELEGRAM_BOT_TOKEN = 'test-token'
        notifier.TELEGRAM_CHAT_ID = '-100999'
        notifier.MESSAGE_THREAD_ID = ''
        notifier.HISTORY_TABLE = 'test-history'
        notifier.LOCKS_TABLE = 'test-locks'

        spec.loader.exec_module(notifier)

        notifier.history_table = mock_history_table
        notifier.locks_table = mock_locks_table
        notifier.TELEGRAM_BOT_TOKEN = 'test-token'
        notifier.TELEGRAM_CHAT_ID = '-100999'
        notifier.MESSAGE_THREAD_ID = ''

        update_calls = []
        send_calls = []

        notifier.update_telegram_message = lambda msg_id, text: update_calls.append(msg_id)
        notifier.send_telegram_message = lambda text: send_calls.append(text) or 77777
        notifier.pin_telegram_message = MagicMock()
        notifier.update_history = MagicMock()
        notifier.get_history = lambda deploy_id: {}

        result = notifier.handle_start({'deploy_id': 'deploy-no-existing', 'project_id': 'proj', 'branch': 'main'})

        # Should send new message
        assert len(send_calls) == 1, "Expected send_telegram_message to be called"
        assert len(update_calls) == 0, "Must not call update when no existing message"
        # Must NOT pin — callbacks.py already pinned on approve; double-pin is a regression (Issue #119)
        notifier.pin_telegram_message.assert_not_called()


class TestNotifierHandleSuccess:
    """Notifier handle_success: updates message + unpins."""

    def test_handle_success_unpins_existing_message(self):
        """handle_success must call unpin_telegram_message with existing message_id."""
        import importlib

        notifier_path = os.path.join(os.path.dirname(__file__), '..', 'deployer', 'notifier', 'app.py')
        spec = importlib.util.spec_from_file_location('notifier_app_3', notifier_path)
        notifier = importlib.util.module_from_spec(spec)

        mock_history_table = MagicMock()
        mock_history_table.get_item.return_value = {
            'Item': {
                'telegram_message_id': 44444,
                'started_at': int(time.time()) - 120,
                'branch': 'main',
            }
        }
        mock_locks_table = MagicMock()
        notifier.history_table = mock_history_table
        notifier.locks_table = mock_locks_table
        notifier.TELEGRAM_BOT_TOKEN = 'test-token'
        notifier.TELEGRAM_CHAT_ID = '-100999'
        notifier.MESSAGE_THREAD_ID = ''
        notifier.HISTORY_TABLE = 'test-history'
        notifier.LOCKS_TABLE = 'test-locks'

        spec.loader.exec_module(notifier)

        notifier.history_table = mock_history_table
        notifier.locks_table = mock_locks_table
        notifier.TELEGRAM_BOT_TOKEN = 'test-token'
        notifier.TELEGRAM_CHAT_ID = '-100999'
        notifier.MESSAGE_THREAD_ID = ''

        update_calls = []
        unpin_calls = []

        notifier.update_telegram_message = lambda msg_id, text: update_calls.append(msg_id)
        notifier.unpin_telegram_message = lambda msg_id: unpin_calls.append(msg_id)
        notifier.update_history = MagicMock()
        notifier.release_lock = MagicMock()
        notifier.get_history = lambda deploy_id: {
            'telegram_message_id': 44444,
            'started_at': int(time.time()) - 120,
            'branch': 'main',
        }

        result = notifier.handle_success({
            'deploy_id': 'deploy-success-001',
            'project_id': 'test-project',
            'build_id': 'build-001',
        })

        assert 44444 in update_calls, "Expected update_telegram_message called"
        assert 44444 in unpin_calls, "Expected unpin_telegram_message called"
        assert result['status'] == 'success'
