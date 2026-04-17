"""Regression tests for #75 — deploy approval request missing expiry schedule.

Before the fix, send_deploy_approval_request() never called
post_notification_setup(), so the inline keyboard was never scheduled for
removal when the request expired.
"""
from unittest.mock import MagicMock, patch
import pytest
import sys
import os



def _make_urlopen_ctx_mock(body: bytes):
    """Create a urlopen mock that properly implements the context manager protocol.

    telegram._telegram_request uses: with urllib.request.urlopen(req, timeout=...) as resp:
    So mock_urlopen.return_value must support __enter__ / __exit__.
    """
    mock_resp = MagicMock()
    mock_resp.read.return_value = body

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_resp)
    cm.__exit__ = MagicMock(return_value=False)

    mock_urlopen = MagicMock(return_value=cm)
    return mock_urlopen, mock_resp


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('APPROVED_CHAT_ID', '99999')
    monkeypatch.setenv('DYNAMODB_TABLE', 'test-table')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('GITHUB_REPO', 'test/repo')
    monkeypatch.setenv('SCHEDULER_ENABLED', 'false')

    table_mock = MagicMock()
    table_mock.get_item.return_value = {}
    table_mock.put_item.return_value = {}
    table_mock.update_item.return_value = {}

    db_mock = MagicMock()
    db_mock.table = table_mock

    # Delete modules to ensure fresh imports
    for mod in ['deployer', 'telegram', 'notifications', 'scheduler_service',
                'db', 'utils', 'safelist']:
        if mod in sys.modules:
            del sys.modules[mod]

    # Create a fresh mock notifications module for each test
    # Explicitly create post_notification_setup with a known identity for proper monkeypatch restore
    notifications_mock = MagicMock()
    # Set a default no-op function that monkeypatch can properly save/restore
    notifications_mock.post_notification_setup = MagicMock(name='post_notification_setup', spec=lambda request_id, telegram_message_id, expires_at: None)

    with patch.dict('sys.modules', {
        'boto3': MagicMock(),
        'botocore': MagicMock(),
        'notifications': notifications_mock,
    }):
        monkeypatch.setitem(sys.modules, 'db', db_mock)
        import deployer
        yield deployer

        # Cleanup after test to ensure isolation
        for mod in ['deployer', 'telegram', 'notifications']:
            if mod in sys.modules:
                del sys.modules[mod]


def _minimal_project(project_id='test-proj'):
    return {
        'project_id': project_id,
        'name': 'Test Project',
        'stack_name': 'test-stack',
    }


class TestDeployExpiryScheduleRegression:
    """Regression: #75 — expiry schedule must be created after deploy approval msg."""

    def test_post_notification_setup_called_with_correct_args(self, app_module, monkeypatch):
        """When expires_at is provided and Telegram returns message_id,
        post_notification_setup() must be called with the correct arguments."""
        from deployer import send_deploy_approval_request
        import notifications

        mock_urlopen, _ = _make_urlopen_ctx_mock(
            b'{"ok":true,"result":{"message_id":42}}'
        )

        mock_pns = MagicMock()
        monkeypatch.setattr(notifications, 'post_notification_setup', mock_pns)

        with patch('urllib.request.urlopen', mock_urlopen):
            send_deploy_approval_request(
                request_id='req-test-001',
                project=_minimal_project(),
                branch='main',
                reason='regression test',
                source='test-bot',
                expires_at=9999999999,
            )

        mock_pns.assert_called_once_with(
            request_id='req-test-001',
            telegram_message_id=42,
            expires_at=9999999999,
        )

    def test_post_notification_setup_not_called_without_expires_at(self, app_module, monkeypatch):
        """Backward compat: if expires_at is None, post_notification_setup is skipped."""
        from deployer import send_deploy_approval_request
        import notifications

        mock_urlopen, _ = _make_urlopen_ctx_mock(
            b'{"ok":true,"result":{"message_id":99}}'
        )

        mock_pns = MagicMock()
        monkeypatch.setattr(notifications, 'post_notification_setup', mock_pns)

        with patch('urllib.request.urlopen', mock_urlopen):
            send_deploy_approval_request(
                request_id='req-test-002',
                project=_minimal_project(),
                branch='main',
                reason='backward compat',
                source='test-bot',
                # expires_at NOT passed
            )

        mock_pns.assert_not_called()

    def test_post_notification_setup_not_called_when_no_message_id(self, app_module, monkeypatch):
        """If Telegram API returns no message_id, skip scheduler call."""
        from deployer import send_deploy_approval_request
        import notifications

        mock_urlopen, _ = _make_urlopen_ctx_mock(b'{"ok":false}')

        mock_pns = MagicMock()
        monkeypatch.setattr(notifications, 'post_notification_setup', mock_pns)

        with patch('urllib.request.urlopen', mock_urlopen):
            send_deploy_approval_request(
                request_id='req-test-003',
                project=_minimal_project(),
                branch='main',
                reason='no message id',
                source='test-bot',
                expires_at=9999999999,
            )

        mock_pns.assert_not_called()

    def test_post_notification_setup_exception_is_swallowed(self, app_module, monkeypatch):
        """If post_notification_setup raises, the error must not propagate."""
        import importlib
        import deployer as deployer_mod
        import telegram as telegram_mod

        # Reload deployer to get a clean module state
        importlib.reload(deployer_mod)
        from deployer import send_deploy_approval_request

        mock_urlopen, _ = _make_urlopen_ctx_mock(
            b'{"ok":true,"result":{"message_id":77}}'
        )

        mock_pns = MagicMock(side_effect=RuntimeError('DDB down'))
        monkeypatch.setattr(deployer_mod.notifications, 'post_notification_setup', mock_pns)
        # Ensure TELEGRAM_TOKEN is non-empty so _telegram_request doesn't short-circuit
        monkeypatch.setattr(telegram_mod, 'TELEGRAM_TOKEN', 'test-token')

        with patch('urllib.request.urlopen', mock_urlopen):
            # Must not raise
            send_deploy_approval_request(
                request_id='req-test-004',
                project=_minimal_project(),
                branch='main',
                reason='error swallowed',
                source='test-bot',
                expires_at=9999999999,
            )

        mock_pns.assert_called_once()
