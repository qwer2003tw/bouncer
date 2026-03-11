"""
Tests for deployer/notifier/app.py

Regression test for Sprint 27-003: Notifier Lambda missing unpin on deploy complete
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import json

import pytest

# Add notifier directory to path
NOTIFIER_DIR = Path(__file__).parent.parent / "notifier"
sys.path.insert(0, str(NOTIFIER_DIR))

# Mock boto3 before importing app (app.py initializes dynamodb at module level)
with patch('boto3.resource'):
    import app


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
    with patch('app.history_table') as mock_history, \
         patch('app.locks_table') as mock_locks:
        yield mock_history, mock_locks


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

        with patch('app.update_telegram_message') as mock_update, \
             patch('app.unpin_telegram_message') as mock_unpin:

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

        with patch('app.send_telegram_message') as mock_send, \
             patch('app.unpin_telegram_message') as mock_unpin:

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

        with patch('app.update_telegram_message') as mock_update, \
             patch('app.unpin_telegram_message') as mock_unpin:

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

        with patch('app.send_telegram_message') as mock_send, \
             patch('app.unpin_telegram_message') as mock_unpin:

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
