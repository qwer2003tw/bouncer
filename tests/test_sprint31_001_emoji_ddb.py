"""
Tests for sprint31-001: Emoji based on exit code + command_status to DDB

Covers:
  - _format_approval_response: title shows ❌ when command fails (manual approve)
  - _format_approval_response: title shows ✅ when command succeeds
  - _execute_and_store_result: command_status='failed' stored in DDB when exit code != 0
  - _execute_and_store_result: command_status='success' stored in DDB when exit code == 0
  - trust_callback DDB update includes command_status
"""
import os
import pytest
from unittest.mock import MagicMock, patch, call


from paging import PaginatedOutput

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


# ---------------------------------------------------------------------------
# Test Case 1: _format_approval_response shows ❌ when command fails
# ---------------------------------------------------------------------------

class TestFormatApprovalResponseEmoji:
    """Test that _format_approval_response uses correct emoji based on exit code."""

    def _make_info(self):
        return {
            'source_line': '',
            'account_line': '',
            'safe_reason': 'test reason',
            'cmd_preview': 'aws s3 ls',
        }

    def test_title_shows_failure_emoji_when_command_fails(self):
        """When _is_execute_failed returns True, title should contain ❌."""
        failed_output = "usage: aws s3 ls <S3Uri>"  # exit code 2

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message') as mock_update:
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve',
                result=failed_output,
                paged={'paged': False},
                trust_line='',
                request_id='req-001',
                info=self._make_info(),
                message_id=123,
            )

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert '❌' in sent_text
        assert '✅' not in sent_text.split('\n')[0]  # First line (title)
        # update_message should also use ❌
        update_text = mock_update.call_args[0][1]
        assert '❌' in update_text

    def test_title_shows_success_emoji_when_command_succeeds(self):
        """When _is_execute_failed returns False, title should contain ✅."""
        success_output = "s3://bucket1\ns3://bucket2"

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message') as mock_update:
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve',
                result=success_output,
                paged={'paged': False},
                trust_line='',
                request_id='req-002',
                info=self._make_info(),
                message_id=124,
            )

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert '✅' in sent_text
        assert '❌' not in sent_text.split('\n')[0]  # title should not have ❌
        # update_message should also use ✅
        update_text = mock_update.call_args[0][1]
        assert '✅' in update_text

    def test_approve_trust_failure_includes_trust_line_and_failure_emoji(self):
        """approve_trust action with failed command → title has ❌ + 🔓."""
        failed_output = "❌ Access Denied (exit code: 1)"

        with patch('callbacks_command.send_telegram_message_silent') as mock_send, \
             patch('callbacks_command.update_message') as mock_update:
            from callbacks_command import _format_approval_response
            _format_approval_response(
                action='approve_trust',
                result=failed_output,
                paged={'paged': False},
                trust_line='',
                request_id='req-003',
                info=self._make_info(),
                message_id=125,
            )

        sent_text = mock_send.call_args[0][0]
        # Should have both ❌ and 🔓
        assert '❌' in sent_text
        assert '🔓' in sent_text


# ---------------------------------------------------------------------------
# Test Case 2 & 3: _execute_and_store_result stores command_status in DDB
# ---------------------------------------------------------------------------

class TestExecuteAndStoreResultCommandStatus:
    """Test that _execute_and_store_result includes command_status in DDB update."""

    def _make_item(self):
        return {'created_at': 1000000}

    def _setup_mocks(self):
        """Return common mock setup for _execute_and_store_result tests."""
        mock_table = MagicMock()
        mock_paged = PaginatedOutput(paged=False, result='test result')
        return mock_table, mock_paged

    def test_command_status_failed_stored_when_exit_code_nonzero(self):
        """command_status='failed' must be in DDB update when command fails."""
        mock_table, mock_paged = self._setup_mocks()
        failed_output = "Command failed (exit code: 2)"

        with patch('callbacks_command._get_table', return_value=mock_table), \
             patch('callbacks_command.execute_command', return_value=failed_output), \
             patch('callbacks_command.store_paged_output', return_value=mock_paged), \
             patch('callbacks_command.emit_metric'):
            from callbacks_command import _execute_and_store_result
            result = _execute_and_store_result(
                command='aws s3 ls bad-syntax',
                assume_role='',
                request_id='req-ddb-001',
                item=self._make_item(),
                user_id='user123',
                source_ip='1.2.3.4',
                action='approve',
            )

        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args[1]
        expr_values = kwargs['ExpressionAttributeValues']
        assert ':cs' in expr_values
        assert expr_values[':cs'] == 'failed'
        assert 'command_status' in kwargs['UpdateExpression']

    def test_command_status_success_stored_when_exit_code_zero(self):
        """command_status='success' must be in DDB update when command succeeds."""
        mock_table, mock_paged = self._setup_mocks()
        success_output = "s3://my-bucket\n"

        with patch('callbacks_command._get_table', return_value=mock_table), \
             patch('callbacks_command.execute_command', return_value=success_output), \
             patch('callbacks_command.store_paged_output', return_value=mock_paged), \
             patch('callbacks_command.emit_metric'):
            from callbacks_command import _execute_and_store_result
            _execute_and_store_result(
                command='aws s3 ls',
                assume_role='',
                request_id='req-ddb-002',
                item=self._make_item(),
                user_id='user123',
                source_ip='1.2.3.4',
                action='approve',
            )

        kwargs = mock_table.update_item.call_args[1]
        expr_values = kwargs['ExpressionAttributeValues']
        assert expr_values[':cs'] == 'success'


# ---------------------------------------------------------------------------
# Test Case 4: trust_callback DDB update includes command_status
# ---------------------------------------------------------------------------

class TestTrustCallbackCommandStatus:
    """Test that trust_callback path stores command_status in DDB via update_item."""

    def test_trust_callback_update_expression_includes_command_status(self):
        """The trust_callback DDB update_item must include command_status = :cs."""
        import re
        # Read the callbacks source and verify the UpdateExpression includes command_status
        src_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'callbacks_command.py')
        with open(src_path) as f:
            source = f.read()

        # Find the trust_callback update_item call region
        # We look for the update expression that follows 'trust_callback' emit_metric call
        trust_update_match = re.search(
            r"Path.*trust_callback.*?UpdateExpression='(.*?)'",
            source,
            re.DOTALL,
        )
        assert trust_update_match is not None, (
            "Could not find trust_callback DDB update expression in callbacks.py"
        )
        update_expr = trust_update_match.group(1)
        assert 'command_status' in update_expr, (
            f"trust_callback UpdateExpression must include command_status, got: {update_expr!r}"
        )
        assert ':cs' in update_expr, (
            f"trust_callback UpdateExpression must use :cs placeholder, got: {update_expr!r}"
        )


# ---------------------------------------------------------------------------
# Test Case 5: _is_execute_failed detection correctness
# ---------------------------------------------------------------------------

class TestIsExecuteFailedDetection:
    """Test _is_execute_failed correctly identifies failure outputs."""

    def test_exit_code_nonzero_is_failed(self):
        from callbacks import _is_execute_failed
        assert _is_execute_failed("error output (exit code: 1)") is True

    def test_exit_code_zero_is_not_failed(self):
        from callbacks import _is_execute_failed
        assert _is_execute_failed("ok (exit code: 0)") is False

    def test_usage_prefix_is_failed(self):
        from callbacks import _is_execute_failed
        assert _is_execute_failed("usage: aws s3 ls <S3Uri>") is True

    def test_red_x_prefix_is_failed(self):
        from callbacks import _is_execute_failed
        assert _is_execute_failed("❌ Access Denied") is True

    def test_normal_success_output_is_not_failed(self):
        from callbacks import _is_execute_failed
        assert _is_execute_failed("s3://bucket1\ns3://bucket2") is False
