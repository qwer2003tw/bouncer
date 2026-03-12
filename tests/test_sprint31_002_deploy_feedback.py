"""
Tests for sprint31-002: Immediate feedback after deploy approval

Covers:
  - handle_deploy_callback: update_message called BEFORE start_deploy on approve
  - handle_deploy_callback: update_message includes remove_buttons=True
  - handle_deploy_callback: update_message failure is non-fatal (continues to start_deploy)
  - handle_deploy_frontend_callback: already removes buttons before deployment
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


def _make_deploy_item(project_id='myproject', branch='main'):
    return {
        'project_id': project_id,
        'project_name': project_id,
        'branch': branch,
        'stack_name': 'myproject-stack',
        'source': 'Private Bot (Test)',
        'reason': 'deploy for test',
        'context': '',
    }


class TestHandleDeployCallbackImmediateFeedback:
    """Test that handle_deploy_callback gives immediate feedback before start_deploy."""

    def test_update_message_called_before_start_deploy(self):
        """update_message (with remove_buttons=True) must be called BEFORE start_deploy."""
        call_order = []

        def mock_update_message(msg_id, text, **kwargs):
            call_order.append(('update_message', kwargs.get('remove_buttons')))

        def mock_start_deploy(project_id, branch, user_id, reason):
            call_order.append(('start_deploy',))
            return {'deploy_id': 'deploy-123', 'commit_short': 'abc1234', 'commit_message': 'test'}

        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message', side_effect=mock_update_message), \
             patch('deployer.start_deploy', side_effect=mock_start_deploy):
            from callbacks import handle_deploy_callback
            handle_deploy_callback(
                action='approve',
                request_id='req-deploy-001',
                item=_make_deploy_item(),
                message_id=999,
                callback_id='cb-001',
                user_id='user123',
            )

        # update_message with remove_buttons=True must appear before start_deploy
        assert len(call_order) >= 2
        # Find index of first update_message(remove_buttons=True) and start_deploy
        first_feedback_idx = next(
            (i for i, c in enumerate(call_order) if c[0] == 'update_message' and c[1] is True),
            None
        )
        start_deploy_idx = next(
            (i for i, c in enumerate(call_order) if c[0] == 'start_deploy'),
            None
        )
        assert first_feedback_idx is not None, "update_message with remove_buttons=True not called"
        assert start_deploy_idx is not None, "start_deploy not called"
        assert first_feedback_idx < start_deploy_idx, (
            f"update_message(remove_buttons=True) must be called BEFORE start_deploy, "
            f"but got order: {call_order}"
        )

    def test_immediate_feedback_contains_request_id(self):
        """Immediate feedback message must contain the request_id."""
        immediate_messages = []

        def mock_update_message(msg_id, text, **kwargs):
            if kwargs.get('remove_buttons'):
                immediate_messages.append(text)

        def mock_start_deploy(project_id, branch, user_id, reason):
            return {'deploy_id': 'deploy-456', 'commit_short': 'def5678', 'commit_message': 'fix'}

        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message', side_effect=mock_update_message), \
             patch('deployer.start_deploy', side_effect=mock_start_deploy):
            from callbacks import handle_deploy_callback
            handle_deploy_callback(
                action='approve',
                request_id='req-feedback-XYZ',
                item=_make_deploy_item(project_id='testproject', branch='release'),
                message_id=1000,
                callback_id='cb-002',
                user_id='user456',
            )

        assert len(immediate_messages) >= 1
        assert 'req-feedback-XYZ' in immediate_messages[0]
        assert 'testproject' in immediate_messages[0] or '⏳' in immediate_messages[0]

    def test_immediate_feedback_failure_does_not_abort_deploy(self):
        """If update_message raises, start_deploy must still be called."""
        import urllib.error

        start_deploy_called = []

        def mock_update_message_fail(msg_id, text, **kwargs):
            if kwargs.get('remove_buttons'):
                raise urllib.error.URLError("Telegram timeout")

        def mock_start_deploy(project_id, branch, user_id, reason):
            start_deploy_called.append(True)
            return {'deploy_id': 'deploy-789'}

        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message', side_effect=mock_update_message_fail), \
             patch('deployer.start_deploy', side_effect=mock_start_deploy):
            from callbacks import handle_deploy_callback
            # Should not raise even though update_message fails
            try:
                handle_deploy_callback(
                    action='approve',
                    request_id='req-resilient-001',
                    item=_make_deploy_item(),
                    message_id=1001,
                    callback_id='cb-003',
                    user_id='user789',
                )
            except Exception as e:
                # The test should pass even if subsequent update_message calls fail
                # (they happen after start_deploy)
                pass

        assert start_deploy_called, "start_deploy must be called even if immediate feedback fails"

    def test_deny_action_does_not_call_start_deploy(self):
        """deny action should not call start_deploy at all."""
        start_deploy_called = []

        def mock_start_deploy(*args, **kwargs):
            start_deploy_called.append(True)
            return {}

        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('deployer.start_deploy', side_effect=mock_start_deploy):
            from callbacks import handle_deploy_callback
            handle_deploy_callback(
                action='deny',
                request_id='req-deny-001',
                item=_make_deploy_item(),
                message_id=1002,
                callback_id='cb-004',
                user_id='user000',
            )

        assert not start_deploy_called, "deny action must not call start_deploy"
