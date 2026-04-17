"""
Regression tests for issue #102: auto_approved notification always shows ✅ even on command failure

The bug was that notifications.py used naive string matching (checking for ❌ or 'error' in first 100 chars)
instead of using extract_exit_code. This failed to detect AWS CLI usage errors (exit code 2).

Fix:
- Layer 1: extract_exit_code now detects 'usage:' / 'Usage:' prefix → exit code 2
- Layer 2: send_trust_auto_approve_notification uses extract_exit_code instead of naive string check
"""

import sys
import os
import pytest
from unittest.mock import MagicMock, patch


# Patch constants before importing
import types as _types
if 'constants' not in sys.modules:
    _constants_stub = _types.ModuleType('constants')
    _constants_stub.AUDIT_TTL_SHORT = 86400 * 30
    _constants_stub.AUDIT_TTL_LONG = 86400 * 90
    _constants_stub.TRUST_SESSION_MAX_COMMANDS = 20
    sys.modules['constants'] = _constants_stub

from utils import extract_exit_code  # noqa: E402


def _make_notifications_module():
    """Import and return a fresh notifications module."""
    from unittest.mock import MagicMock

    # Set required environment variables
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
    os.environ.setdefault('APPROVED_CHAT_ID', '99999')
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')

    # Clear cached modules so imports are fresh
    for mod in ['notifications', 'telegram', 'commands', 'constants', 'utils',
                'risk_scorer', 'template_scanner', 'scheduler_service']:
        sys.modules.pop(mod, None)
        sys.modules.pop(f'src.{mod}', None)

    import notifications as _n

    # Mock send_message_with_entities
    _n._telegram.send_message_with_entities = MagicMock(
        return_value={'ok': True, 'result': {'message_id': 99999}}
    )

    return _n


class TestExtractExitCodeUsage:
    """Test extract_exit_code detects AWS CLI usage errors."""

    def test_extract_exit_code_lowercase_usage(self):
        """usage: prefix returns exit code 2."""
        output = "usage: aws [options] <command> <subcommand> [parameters]"
        assert extract_exit_code(output) == 2

    def test_extract_exit_code_uppercase_usage(self):
        """Usage: prefix returns exit code 2."""
        output = "Usage: aws s3 ls <S3Uri>"
        assert extract_exit_code(output) == 2

    def test_extract_exit_code_with_exit_code_marker(self):
        """Explicit (exit code: N) marker takes precedence over usage: prefix."""
        output = "usage: some command (exit code: 1)"
        assert extract_exit_code(output) == 1

    def test_extract_exit_code_red_x_prefix(self):
        """❌ prefix returns exit code -1."""
        output = "❌ Command failed"
        assert extract_exit_code(output) == -1

    def test_extract_exit_code_success(self):
        """Successful output returns None."""
        output = "s3://bucket1\ns3://bucket2"
        assert extract_exit_code(output) is None

    def test_extract_exit_code_explicit_zero(self):
        """(exit code: 0) returns 0."""
        output = "Command completed successfully (exit code: 0)"
        assert extract_exit_code(output) == 0

    def test_extract_exit_code_explicit_nonzero(self):
        """(exit code: N) with N>0 returns N."""
        output = "Command failed (exit code: 127)"
        assert extract_exit_code(output) == 127


class TestRegressionAutoApprovedFailureEmoji:
    """Regression tests for #102: notifications should show correct emoji based on exit code."""

    @pytest.fixture(autouse=True)
    def _reload_notifications(self):
        """Reload notifications fresh for each test."""
        self.notif = _make_notifications_module()

    def _patch_entities_send(self, ok: bool = True):
        """Patch telegram send_message_with_entities."""
        send_ret = {'ok': ok, 'result': {'message_id': 1}}
        send_mock = MagicMock(return_value=send_ret)
        ctx = patch.object(self.notif._telegram, 'send_message_with_entities', send_mock)
        return ctx, send_mock

    def test_regression_auto_approved_failure_emoji(self):
        """AWS CLI usage error (usage: prefix) should show ❌ in notification."""
        ctx, entities_mock = self._patch_entities_send()
        usage_output = "usage: aws s3 ls <S3Uri>\n\nOptional arguments:\n  --help"

        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls invalid syntax',
                trust_id='trust-regression-001',
                remaining='5 min',
                count=1,
                result=usage_output,
            )

        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]

        # Should contain ❌ for failure
        assert '❌' in text, "usage: error should show ❌ in notification"
        # Should not contain ✅
        assert '✅' not in text, "usage: error should not show ✅"

    def test_regression_auto_approved_success_emoji(self):
        """Successful command output should still show ✅ in notification."""
        ctx, entities_mock = self._patch_entities_send()
        success_output = "s3://bucket1\ns3://bucket2\ns3://bucket3"

        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-regression-002',
                remaining='4 min',
                count=2,
                result=success_output,
            )

        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]

        # Should contain ✅ for success
        assert '✅' in text, "successful command should show ✅"
        # Should not contain ❌
        assert '❌' not in text, "successful command should not show ❌"

    def test_regression_auto_approved_red_x_prefix_emoji(self):
        """Output with ❌ prefix should show ❌ in notification."""
        ctx, entities_mock = self._patch_entities_send()
        error_output = "❌ Access Denied: You don't have permission to access this resource"

        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls s3://restricted-bucket',
                trust_id='trust-regression-003',
                remaining='3 min',
                count=3,
                result=error_output,
            )

        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]

        assert '❌' in text, "❌ prefix error should show ❌"
        assert '✅' not in text, "❌ prefix error should not show ✅"

    def test_regression_auto_approved_exit_code_marker(self):
        """Output with (exit code: N) marker should use that exit code."""
        ctx, entities_mock = self._patch_entities_send()
        error_output = "Command failed: file not found (exit code: 127)"

        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='bash nonexistent.sh',
                trust_id='trust-regression-004',
                remaining='2 min',
                count=4,
                result=error_output,
            )

        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]

        assert '❌' in text, "exit code 127 should show ❌"
        assert '✅' not in text, "exit code 127 should not show ✅"

    def test_regression_auto_approved_exit_code_zero(self):
        """Output with (exit code: 0) should show ✅."""
        ctx, entities_mock = self._patch_entities_send()
        success_output = "Operation completed (exit code: 0)"

        with ctx:
            self.notif.send_trust_auto_approve_notification(
                command='aws s3 ls',
                trust_id='trust-regression-005',
                remaining='1 min',
                count=5,
                result=success_output,
            )

        entities_mock.assert_called_once()
        text = entities_mock.call_args[0][0]

        assert '✅' in text, "exit code 0 should show ✅"
        assert '❌' not in text, "exit code 0 should not show ❌"
