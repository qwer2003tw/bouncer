"""
Tests for deployer/notifier/handler.py

Regression test for Sprint 27-003: Notifier Lambda missing unpin on deploy complete
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add notifier directory to path
NOTIFIER_DIR = Path(__file__).parent.parent / "notifier"
sys.path.insert(0, str(NOTIFIER_DIR))

# Mock boto3 before importing app (app.py initializes dynamodb at module level)
with patch('boto3.resource'):
    import handler as app


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables"""
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test_token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '12345')
    monkeypatch.setenv('HISTORY_TABLE', 'test-history-table')
    monkeypatch.setenv('LOCKS_TABLE', 'test-locks-table')


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB tables"""
    with patch('handler.history_table') as mock_history, \
         patch('handler.locks_table') as mock_locks:
        yield mock_history, mock_locks


class TestPinTelegramMessage:
    """Test pin_telegram_message function (Sprint 29-004)"""

    def test_pin_message_success(self):
        """Test successful pin"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', 'test_token'), \
             patch.object(app, 'TELEGRAM_CHAT_ID', '12345'):

            app.pin_telegram_message(456)

            # Verify the request was made
            assert mock_urlopen.called
            request = mock_urlopen.call_args[0][0]
            assert 'pinChatMessage' in request.full_url

            # Check request data
            data = request.data.decode()
            assert 'chat_id=12345' in data
            assert 'message_id=456' in data
            assert 'disable_notification=True' in data

    def test_pin_message_no_token(self):
        """Test pin with missing token (should skip silently)"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', ''), \
             patch.object(app, 'TELEGRAM_CHAT_ID', ''):

            app.pin_telegram_message(456)

            # Should not make any requests
            assert not mock_urlopen.called

    def test_pin_message_no_message_id(self):
        """Test pin with None message_id (should skip silently)"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', 'test_token'), \
             patch.object(app, 'TELEGRAM_CHAT_ID', '12345'):

            app.pin_telegram_message(None)

            # Should not make any requests
            assert not mock_urlopen.called

    def test_pin_message_error_handling(self):
        """Test pin error is caught and logged (best-effort)"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', 'test_token'), \
             patch.object(app, 'TELEGRAM_CHAT_ID', '12345'):

            mock_urlopen.side_effect = Exception("Network error")

            # Should not raise exception (best-effort)
            try:
                app.pin_telegram_message(456)
                # If it doesn't raise, that's correct behavior
            except Exception:
                pytest.fail("pin_telegram_message should not raise exceptions")


class TestUnpinTelegramMessage:
    """Test unpin_telegram_message function"""

    def test_unpin_message_success(self):
        """Test successful unpin"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', 'test_token'), \
             patch.object(app, 'TELEGRAM_CHAT_ID', '12345'):

            app.unpin_telegram_message(123)

            # Verify the request was made
            assert mock_urlopen.called
            request = mock_urlopen.call_args[0][0]
            assert 'unpinChatMessage' in request.full_url

            # Check request data
            data = request.data.decode()
            assert 'chat_id=12345' in data
            assert 'message_id=123' in data

    def test_unpin_message_no_token(self):
        """Test unpin with missing token (should skip silently)"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', ''), \
             patch.object(app, 'TELEGRAM_CHAT_ID', ''):

            app.unpin_telegram_message(123)

            # Should not make any requests
            assert not mock_urlopen.called

    def test_unpin_message_no_message_id(self):
        """Test unpin with None message_id (should skip silently)"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', 'test_token'), \
             patch.object(app, 'TELEGRAM_CHAT_ID', '12345'):

            app.unpin_telegram_message(None)

            # Should not make any requests
            assert not mock_urlopen.called

    def test_unpin_message_error_handling(self):
        """Test unpin error is caught and logged (best-effort)"""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(app, 'TELEGRAM_BOT_TOKEN', 'test_token'), \
             patch.object(app, 'TELEGRAM_CHAT_ID', '12345'):

            mock_urlopen.side_effect = Exception("Network error")

            # Should not raise exception (best-effort)
            try:
                app.unpin_telegram_message(123)
                # If it doesn't raise, that's correct behavior
            except Exception:
                pytest.fail("unpin_telegram_message should not raise exceptions")


class TestHandleSuccessUnpin:
    """Test handle_success calls unpin correctly"""

    def test_handle_success_unpins_message(self, mock_env, mock_dynamodb):
        """Regression test: handle_success should unpin message on deploy complete"""
        mock_history, mock_locks = mock_dynamodb

        # Mock history with message_id
        mock_history.get_item.return_value = {
            'Item': {
                'telegram_message_id': 456,
                'started_at': 1000000,
                'branch': 'main'
            }
        }

        with patch('handler.update_telegram_message') as mock_update, \
             patch('handler.unpin_telegram_message') as mock_unpin:

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'build_id': 'build-123'
            }

            result = app.handle_success(event)

            # Should update message
            assert mock_update.called
            assert mock_update.call_args[0][0] == 456

            # Should unpin message (this is the bug fix)
            assert mock_unpin.called
            assert mock_unpin.call_args[0][0] == 456

            # Should return success
            assert result['status'] == 'success'

    def test_handle_success_no_message_id(self, mock_env, mock_dynamodb):
        """Test handle_success with no message_id (should not crash)"""
        mock_history, mock_locks = mock_dynamodb

        # Mock history without message_id
        mock_history.get_item.return_value = {'Item': {}}

        with patch('handler.send_telegram_message') as mock_send, \
             patch('handler.unpin_telegram_message') as mock_unpin:

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'build_id': 'build-123'
            }

            result = app.handle_success(event)

            # Should send new message instead of updating
            assert mock_send.called

            # Should NOT call unpin (no message_id)
            assert not mock_unpin.called

            # Should return success
            assert result['status'] == 'success'


class TestHandleFailureUnpin:
    """Test handle_failure calls unpin correctly"""

    def test_handle_failure_unpins_message(self, mock_env, mock_dynamodb):
        """Regression test: handle_failure should unpin message on deploy failure"""
        mock_history, mock_locks = mock_dynamodb

        # Mock history with message_id
        mock_history.get_item.return_value = {
            'Item': {
                'telegram_message_id': 789,
                'started_at': 1000000,
                'branch': 'main',
                'phase': 'BUILD'
            }
        }

        with patch('handler.update_telegram_message') as mock_update, \
             patch('handler.unpin_telegram_message') as mock_unpin:

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'error': {'Error': 'BuildFailed', 'Cause': 'Test error'}
            }

            result = app.handle_failure(event)

            # Should update message
            assert mock_update.called
            assert mock_update.call_args[0][0] == 789

            # Should unpin message (this is the bug fix)
            assert mock_unpin.called
            assert mock_unpin.call_args[0][0] == 789

            # Should return failed
            assert result['status'] == 'failed'

    def test_handle_failure_no_message_id(self, mock_env, mock_dynamodb):
        """Test handle_failure with no message_id (should not crash)"""
        mock_history, mock_locks = mock_dynamodb

        # Mock history without message_id
        mock_history.get_item.return_value = {'Item': {}}

        with patch('handler.send_telegram_message') as mock_send, \
             patch('handler.unpin_telegram_message') as mock_unpin:

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'error': 'Test error'
            }

            result = app.handle_failure(event)

            # Should send new message instead of updating
            assert mock_send.called

            # Should NOT call unpin (no message_id)
            assert not mock_unpin.called

            # Should return failed
            assert result['status'] == 'failed'


class TestHandleStartPin:
    """Test handle_start calls pin correctly (Sprint 29-004)"""

    def test_handle_start_pins_message(self, mock_env, mock_dynamodb):
        """Test that handle_start sends message and returns message_id (#119: pin removed)"""
        mock_history, mock_locks = mock_dynamodb
        # No existing message_id → falls through to send_telegram_message
        mock_history.get_item.return_value = {}

        with patch('handler.send_telegram_message') as mock_send, \
             patch('handler.pin_telegram_message') as mock_pin:

            # Mock message send
            mock_send.return_value = 789

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'branch': 'main'
            }

            result = app.handle_start(event)

            # Should send message
            assert not mock_send.called  # #277

            # pin was removed in Issue #119 (callbacks.py already pins at approval time)
            assert not mock_pin.called

            # Should return message_id
            assert result['message_id'] == 0  # #277: no Telegram send

    def test_handle_start_no_message_id(self, mock_env, mock_dynamodb):
        """Test handle_start with no message_id (should not crash)"""
        mock_history, mock_locks = mock_dynamodb
        # No existing message_id → falls through to send_telegram_message
        mock_history.get_item.return_value = {}

        with patch('handler.send_telegram_message') as mock_send, \
             patch('handler.pin_telegram_message') as mock_pin:

            # Mock message send returns None
            mock_send.return_value = None

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'branch': 'main'
            }

            result = app.handle_start(event)

            # Should send message
            assert not mock_send.called  # #277

            # Should NOT call pin (no message_id)
            assert not mock_pin.called

            # Should return None message_id
            assert result['message_id'] == 0  # #277


class TestSprint39UX:
    """Test Sprint 39 UX improvements (s39-002: ANALYZING phase + elapsed time)"""

    def test_handle_progress_analyzing_phase(self, mock_env, mock_dynamodb):
        """Test that ANALYZING phase shows correct icons in progress."""
        mock_history, mock_locks = mock_dynamodb
        mock_history.get_item.return_value = {
            'Item': {
                'deploy_id': 'test-deploy-123',
                'telegram_message_id': 789,
                'started_at': 1000000,
                'created_at': 1000000,
            }
        }

        with patch('handler.update_telegram_message') as mock_update, \
             patch('handler.format_duration') as mock_format_duration, \
             patch('time.time', return_value=1000010):  # 10 seconds elapsed

            mock_format_duration.return_value = '10s'

            event = {
                'deploy_id': 'test-deploy-123',
                'project_id': 'test-project',
                'branch': 'main',
                'phase': 'ANALYZING',
            }

            result = app.handle_progress(event)

            # Should update message
            # #277: handle_progress no longer updates Telegram
            assert not mock_update.called

            # Elapsed time assertions removed (#277)

    def test_handle_analyze_updates_phase_to_analyzing(self, mock_env, mock_dynamodb):
        """Test that handle_analyze sets phase=ANALYZING at the start."""
        mock_history, mock_locks = mock_dynamodb

        # Mock get_history to return a history with telegram_message_id
        mock_history.get_item.return_value = {
            'Item': {
                'deploy_id': 'test-deploy-123',
                'telegram_message_id': 789,
            }
        }

        # Override get_history to also return changeset_name (Sprint 73 DDB-based flow)
        mock_history.get_item.return_value = {
            'Item': {
                'deploy_id': 'test-deploy-123',
                'telegram_message_id': 789,
                'changeset_name': 'changeset-123',
                'no_changes': False,
            }
        }

        with patch('handler.update_history') as mock_update_history, \
             patch('handler.handle_progress') as mock_handle_progress, \
             patch('handler._get_stack_name', return_value='test-stack'), \
             patch('boto3.client') as mock_boto3_client:

            # Mock CFN client
            mock_cfn = mock_boto3_client.return_value

            # Mock changeset_analyzer imports (no more create_dry_run_changeset)
            with patch('changeset_analyzer.analyze_changeset') as mock_analyze, \
                 patch('changeset_analyzer.is_code_only_change', return_value=True):

                mock_analyze.return_value = type('obj', (object,), {
                    'resource_changes': [],
                    'error': None,
                })

                event = {
                    'deploy_id': 'test-deploy-123',
                    'project_id': 'test-project',
                }

                result = app.handle_analyze(event)

                # Should update history with phase=ANALYZING
                assert mock_update_history.called
                update_call = mock_update_history.call_args_list[0]
                assert update_call[0][0] == 'test-deploy-123'
                assert update_call[0][1] == {'phase': 'ANALYZING'}

                # Should call handle_progress to update the message
                assert not mock_handle_progress.called  # #277: removed
