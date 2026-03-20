"""
Tests for sprint10-002: Fix execution error tracking — use exit code from output

Root Cause: 3 execution paths used `output.startswith('❌')` to detect failures,
but AWS CLI failure output like:
    "An error occurred (AccessDenied)...(exit code: 255)"
does not start with ❌, so record_execution_error() was never called.

Fix: use `extract_exit_code(output)` — matches (exit code: N) pattern or ❌ prefix.

Regression tests:
- AWS CLI failure output (no ❌, has exit code) → triggers record_execution_error
- ❌-prefixed Bouncer error still triggers (backward compat)
- success output → no record
- exit_code in response is the actual value (not hardcoded -1)
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ---------------------------------------------------------------------------
# Unit tests: extract_exit_code() in utils.py
# ---------------------------------------------------------------------------

class TestExtractExitCode:
    """Unit tests for the new extract_exit_code() helper."""

    def test_aws_cli_access_denied_exit_255(self):
        from utils import extract_exit_code
        output = (
            "An error occurred (AccessDenied) when calling the "
            "ListObjectsV2 operation: Access Denied\n\n(exit code: 255)"
        )
        assert extract_exit_code(output) == 255

    def test_aws_cli_exit_code_1(self):
        from utils import extract_exit_code
        assert extract_exit_code("Some error\n(exit code: 1)") == 1

    def test_aws_cli_exit_code_127(self):
        from utils import extract_exit_code
        assert extract_exit_code("command not found: aws (exit code: 127)") == 127

    def test_aws_cli_exit_code_0_means_success(self):
        from utils import extract_exit_code
        assert extract_exit_code("output\n(exit code: 0)") == 0

    def test_bouncer_formatted_error_returns_minus_one(self):
        from utils import extract_exit_code
        assert extract_exit_code("\u274c Command blocked by compliance") == -1

    def test_success_output_returns_none(self):
        from utils import extract_exit_code
        assert extract_exit_code("2024-01-01  my-bucket\n2024-01-02  other-bucket") is None

    def test_empty_output_returns_none(self):
        from utils import extract_exit_code
        assert extract_exit_code("") is None

    def test_whitespace_in_exit_code_pattern(self):
        from utils import extract_exit_code
        assert extract_exit_code("error\n(exit code:  255)") == 255

    def test_exit_code_in_middle_of_output(self):
        from utils import extract_exit_code
        output = "prefix text (exit code: 2) suffix text"
        assert extract_exit_code(output) == 2

    def test_bouncer_error_with_exit_code_uses_exit_code(self):
        from utils import extract_exit_code
        output = "\u274c Some error (exit code: 1)"
        assert extract_exit_code(output) == 1


# ---------------------------------------------------------------------------
# Integration: _check_auto_approve — AWS CLI failure (REGRESSION)
# ---------------------------------------------------------------------------

class TestAutoApproveAwsCliFailure:

    AWS_CLI_FAILURE = (
        "An error occurred (AccessDenied) when calling the "
        "ListObjectsV2 operation: Access Denied\n\n(exit code: 255)"
    )
    AWS_CLI_SUCCESS = "2024-01-01 00:00:00   my-bucket\n"
    BOUNCER_ERROR = "\u274c Command blocked: compliance violation"

    def _run_auto_approve(self, cmd_result):
        ctx_mock = MagicMock()
        ctx_mock.command = 'aws s3 ls s3://nonexistent-bucket'
        ctx_mock.assume_role = None
        ctx_mock.reason = 'test'
        ctx_mock.source = 'test-source'
        ctx_mock.account_id = '123456789012'
        ctx_mock.account_name = 'Test'
        ctx_mock.req_id = 1
        ctx_mock.smart_decision = None
        ctx_mock.is_native = False

        with patch('mcp_execute.is_auto_approve', return_value=True), \
             patch('mcp_execute.execute_command', return_value=cmd_result), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output', return_value={
                 'result': cmd_result, 'paged': False
             }), \
             patch('mcp_execute.send_telegram_message_silent'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error') as mock_record, \
             patch('mcp_execute.table', MagicMock()):
            from mcp_execute import _check_auto_approve
            result = _check_auto_approve(ctx_mock)
            return result, mock_record

    def test_aws_cli_failure_triggers_record_execution_error(self):
        """REGRESSION: AWS CLI exit code 255 (no emoji prefix) must trigger DDB write."""
        result, mock_record = self._run_auto_approve(self.AWS_CLI_FAILURE)
        mock_record.assert_called_once()
        kwargs = mock_record.call_args[1]
        assert kwargs['exit_code'] == 255

    def test_aws_cli_failure_exit_code_in_response(self):
        """MCP response must include actual exit code (255), not hardcoded -1."""
        result, mock_record = self._run_auto_approve(self.AWS_CLI_FAILURE)
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])
        assert data['exit_code'] == 255

    def test_bouncer_error_still_triggers_record_execution_error(self):
        """Backward compat: emoji-prefix error still works, exit_code=-1."""
        result, mock_record = self._run_auto_approve(self.BOUNCER_ERROR)
        mock_record.assert_called_once()
        kwargs = mock_record.call_args[1]
        assert kwargs['exit_code'] == -1

    def test_success_does_not_trigger_record_execution_error(self):
        result, mock_record = self._run_auto_approve(self.AWS_CLI_SUCCESS)
        mock_record.assert_not_called()

    def test_success_has_no_exit_code_in_response(self):
        result, mock_record = self._run_auto_approve(self.AWS_CLI_SUCCESS)
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])
        assert 'exit_code' not in data


# ---------------------------------------------------------------------------
# Integration: _check_trust_session — AWS CLI failure (REGRESSION)
# ---------------------------------------------------------------------------

class TestTrustSessionAwsCliFailure:

    AWS_CLI_FAILURE = (
        "An error occurred (NoSuchBucket) when calling the GetObject "
        "operation: The specified bucket does not exist\n\n(exit code: 1)"
    )

    def _run_trust_session(self, cmd_result):
        ctx_mock = MagicMock()
        ctx_mock.command = 'aws s3 cp s3://missing-bucket/x /tmp/x'
        ctx_mock.assume_role = None
        ctx_mock.reason = 'test'
        ctx_mock.source = 'test-source'
        ctx_mock.account_id = '123456789012'
        ctx_mock.account_name = 'Test'
        ctx_mock.req_id = 2
        ctx_mock.trust_scope = 'scope-abc'
        ctx_mock.smart_decision = None
        ctx_mock.is_native = False

        trust_session = {'request_id': 'trust-001', 'expires_at': 9999999999}

        with patch('mcp_execute.should_trust_approve', return_value=(True, trust_session, 'ok')), \
             patch('mcp_execute.increment_trust_command_count', return_value=1), \
             patch('mcp_execute.execute_command', return_value=cmd_result), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output', return_value={
                 'result': cmd_result, 'paged': False
             }), \
             patch('mcp_execute.send_trust_auto_approve_notification'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error') as mock_record, \
             patch('mcp_execute.track_command_executed'), \
             patch('mcp_execute.table', MagicMock()):
            from mcp_execute import _check_trust_session
            result = _check_trust_session(ctx_mock)
            return result, mock_record

    def test_aws_cli_failure_triggers_record_execution_error(self):
        """Scenario 4 REGRESSION: trust session AWS CLI failure triggers DDB write."""
        result, mock_record = self._run_trust_session(self.AWS_CLI_FAILURE)
        mock_record.assert_called_once()
        kwargs = mock_record.call_args[1]
        assert kwargs['exit_code'] == 1

    def test_aws_cli_failure_exit_code_in_response(self):
        result, mock_record = self._run_trust_session(self.AWS_CLI_FAILURE)
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])
        assert data['exit_code'] == 1

    def test_success_does_not_trigger_record_execution_error(self):
        result, mock_record = self._run_trust_session("download: s3://bucket/x to /tmp/x")
        mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: _check_grant_session — AWS CLI failure (REGRESSION)
# ---------------------------------------------------------------------------

class TestGrantSessionAwsCliFailure:

    AWS_CLI_FAILURE = (
        "An error occurred (AccessDenied) when calling the "
        "PutObject operation: Access Denied\n\n(exit code: 255)"
    )

    def _run_grant_session(self, cmd_result):
        ctx_mock = MagicMock()
        ctx_mock.command = 'aws s3 cp /tmp/file.txt s3://denied-bucket/'
        ctx_mock.assume_role = None
        ctx_mock.reason = 'test'
        ctx_mock.source = 'test-source'
        ctx_mock.account_id = '123456789012'
        ctx_mock.account_name = 'Test'
        ctx_mock.req_id = 3
        ctx_mock.grant_id = 'grant-001'
        ctx_mock.smart_decision = None
        ctx_mock.is_native = False

        grant = {
            'grant_id': 'grant-001',
            'status': 'active',
            'source': 'test-source',
            'account_id': '123456789012',
            'granted_commands': ['aws s3 cp /tmp/file.txt s3://denied-bucket/'],
            'used_commands': {},
            'expires_at': 9999999999,
            'allow_repeat': False,
        }

        with patch('mcp_execute.GRANT_SESSION_ENABLED', True), \
             patch('grant.get_grant_session', return_value=grant), \
             patch('grant.normalize_command', return_value=ctx_mock.command), \
             patch('grant.is_command_in_grant', return_value=True), \
             patch('grant.try_use_grant_command', return_value=True), \
             patch('mcp_execute.execute_command', return_value=cmd_result), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output', return_value={
                 'result': cmd_result, 'paged': False
             }), \
             patch('mcp_execute.send_grant_execute_notification'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error') as mock_record, \
             patch('mcp_execute.table', MagicMock()):
            from mcp_execute import _check_grant_session
            result = _check_grant_session(ctx_mock)
            return result, mock_record

    def test_aws_cli_failure_triggers_record_execution_error(self):
        """Scenario 5 REGRESSION: grant session AWS CLI failure triggers DDB write."""
        result, mock_record = self._run_grant_session(self.AWS_CLI_FAILURE)
        mock_record.assert_called_once()
        kwargs = mock_record.call_args[1]
        assert kwargs['exit_code'] == 255

    def test_aws_cli_failure_exit_code_in_response(self):
        result, mock_record = self._run_grant_session(self.AWS_CLI_FAILURE)
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])
        assert data['exit_code'] == 255

    def test_success_does_not_trigger_record_execution_error(self):
        result, mock_record = self._run_grant_session("upload: /tmp/file.txt to s3://bucket/")
        mock_record.assert_not_called()
