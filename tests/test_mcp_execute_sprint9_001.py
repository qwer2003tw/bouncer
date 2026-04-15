"""
Tests for sprint9-001: Execution Error Persistence to DDB

Verifies:
- execute_command failure (❌ output) triggers record_execution_error
- DDB update_item called with correct fields
- MCP response includes exit_code on failure
- Success path unchanged (no error fields written)
- record_execution_error handles truncation, None exit_code, empty output
- log_decision accepts new exit_code / error_output optional params
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, call

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paging import PaginatedOutput


# ---------------------------------------------------------------------------
# Unit tests: record_execution_error in utils.py
# ---------------------------------------------------------------------------

class TestRecordExecutionError:
    def _make_table(self):
        table = MagicMock()
        table.update_item = MagicMock()
        return table

    def test_calls_update_item_with_correct_fields(self):
        from utils import record_execution_error
        table = self._make_table()
        record_execution_error(table, 'req-abc', exit_code=1, error_output='fatal error')

        table.update_item.assert_called_once()
        kwargs = table.update_item.call_args[1]
        assert kwargs['Key'] == {'request_id': 'req-abc'}
        assert 'SET' in kwargs['UpdateExpression']
        values = kwargs['ExpressionAttributeValues']
        assert values[':s'] == 'executed_error'
        assert values[':ec'] == 1
        assert values[':eo'] == 'fatal error'
        assert isinstance(values[':ea'], int)

    def test_truncates_long_error_output(self):
        from utils import record_execution_error
        table = self._make_table()
        long_output = 'x' * 3000
        record_execution_error(table, 'req-trunc', exit_code=2, error_output=long_output)

        values = table.update_item.call_args[1]['ExpressionAttributeValues']
        assert len(values[':eo']) <= 2011  # 2000 + len('[truncated]')
        assert values[':eo'].endswith('[truncated]')

    def test_empty_error_output_becomes_placeholder(self):
        from utils import record_execution_error
        table = self._make_table()
        record_execution_error(table, 'req-empty', exit_code=1, error_output='')

        values = table.update_item.call_args[1]['ExpressionAttributeValues']
        assert values[':eo'] == '(no output)'

    def test_none_exit_code_becomes_minus_one(self):
        from utils import record_execution_error
        table = self._make_table()
        record_execution_error(table, 'req-none', exit_code=None, error_output='some error')

        values = table.update_item.call_args[1]['ExpressionAttributeValues']
        assert values[':ec'] == -1

    def test_ddb_exception_does_not_propagate(self):
        """DDB failure must be swallowed -- must not raise."""
        from utils import record_execution_error
        table = self._make_table()
        table.update_item.side_effect = Exception('DynamoDB unreachable')

        # Should not raise
        record_execution_error(table, 'req-fail', exit_code=1, error_output='err')

    def test_status_set_to_executed_error(self):
        from utils import record_execution_error
        table = self._make_table()
        record_execution_error(table, 'req-status', exit_code=3, error_output='bad')

        names = table.update_item.call_args[1]['ExpressionAttributeNames']
        values = table.update_item.call_args[1]['ExpressionAttributeValues']
        assert '#s' in names
        assert names['#s'] == 'status'
        assert values[':s'] == 'executed_error'


# ---------------------------------------------------------------------------
# Unit tests: log_decision new params (backward-compat)
# ---------------------------------------------------------------------------

class TestLogDecisionNewParams:
    def _make_table(self):
        table = MagicMock()
        table.put_item = MagicMock()
        return table

    def _call(self, table, **extra):
        from utils import log_decision
        return log_decision(
            table=table,
            request_id='req-test',
            command='aws s3 ls',
            reason='test',
            source='test-source',
            account_id='123456789012',
            decision_type='auto_approved',
            **extra
        )

    def test_existing_callers_unaffected(self):
        """Calling without exit_code/error_output still works."""
        table = self._make_table()
        item = self._call(table)
        table.put_item.assert_called_once()
        assert 'exit_code' not in item
        assert 'error_output' not in item

    def test_exit_code_written_when_provided(self):
        table = self._make_table()
        item = self._call(table, exit_code=1, error_output='something went wrong')
        assert item['exit_code'] == 1
        assert item['error_output'] == 'something went wrong'

    def test_error_output_truncated_in_log_decision(self):
        table = self._make_table()
        item = self._call(table, exit_code=2, error_output='e' * 3000)
        assert len(item['error_output']) <= 2011
        assert item['error_output'].endswith('[truncated]')


# ---------------------------------------------------------------------------
# Integration tests: _check_auto_approve with mocked dependencies
# ---------------------------------------------------------------------------

class TestCheckAutoApproveErrorPath:
    """Test that _check_auto_approve records error when execute_command fails."""

    def _run_auto_approve(self, cmd_result):
        """Helper: run _check_auto_approve with mocked dependencies."""
        mock_table = MagicMock()
        mock_table.put_item = MagicMock()
        mock_table.update_item = MagicMock()

        ctx_mock = MagicMock()
        ctx_mock.command = 'aws s3 cp /nonexistent s3://bucket/'
        ctx_mock.assume_role = None
        ctx_mock.reason = 'test reason'
        ctx_mock.source = 'test-source'
        ctx_mock.account_id = '123456789012'
        ctx_mock.account_name = 'Test'
        ctx_mock.req_id = 1
        ctx_mock.smart_decision = None
        ctx_mock.is_native = False  # ensure non-native path

        with patch('mcp_execute.is_auto_approve', return_value=True), \
             patch('mcp_execute.execute_command', return_value=cmd_result), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output', return_value=PaginatedOutput(
                 paged=False, result=cmd_result
             )), \
             patch('mcp_execute.send_telegram_message_silent'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error') as mock_record, \
             patch('mcp_execute.table', mock_table):
            from mcp_execute import _check_auto_approve
            result = _check_auto_approve(ctx_mock)
            return result, mock_record

    def test_error_path_calls_record_execution_error(self):
        result, mock_record = self._run_auto_approve('\u274c fatal: no such file')
        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args
        assert call_kwargs[1]['exit_code'] == -1
        assert '\u274c' in call_kwargs[1]['error_output']

    def test_error_path_includes_exit_code_in_response(self):
        result, mock_record = self._run_auto_approve('\u274c fatal: no such file')
        body = json.loads(result['body'])
        content_text = json.loads(body['result']['content'][0]['text'])
        assert content_text['exit_code'] == -1

    def test_success_path_does_not_call_record_execution_error(self):
        result, mock_record = self._run_auto_approve('2026-01-01 my-bucket\n')
        mock_record.assert_not_called()

    def test_success_path_has_no_exit_code_in_response(self):
        result, mock_record = self._run_auto_approve('2026-01-01 my-bucket\n')
        body = json.loads(result['body'])
        content_text = json.loads(body['result']['content'][0]['text'])
        assert 'exit_code' not in content_text


# ---------------------------------------------------------------------------
# Integration tests: _check_trust_session with mocked dependencies
# ---------------------------------------------------------------------------

class TestCheckTrustSessionErrorPath:
    def _run_trust_session(self, cmd_result):
        mock_table = MagicMock()
        mock_table.put_item = MagicMock()
        mock_table.update_item = MagicMock()

        ctx_mock = MagicMock()
        ctx_mock.command = 'aws ec2 describe-bad-thing'
        ctx_mock.assume_role = None
        ctx_mock.reason = 'test'
        ctx_mock.source = 'test-source'
        ctx_mock.account_id = '123456789012'
        ctx_mock.account_name = 'Test'
        ctx_mock.req_id = 2
        ctx_mock.trust_scope = 'scope-abc'
        ctx_mock.smart_decision = None
        ctx_mock.is_native = False  # ensure non-native path

        trust_session = {'request_id': 'trust-001', 'expires_at': 9999999999}

        with patch('mcp_execute.should_trust_approve', return_value=(True, trust_session, 'ok')), \
             patch('mcp_execute.increment_trust_command_count', return_value=1), \
             patch('mcp_execute.execute_command', return_value=cmd_result), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output', return_value=PaginatedOutput(
                 paged=False, result=cmd_result
             )), \
             patch('mcp_execute.send_trust_auto_approve_notification'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error') as mock_record, \
             patch('mcp_execute.table', mock_table):
            from mcp_execute import _check_trust_session
            result = _check_trust_session(ctx_mock)
            return result, mock_record

    def test_trust_error_path_calls_record_execution_error(self):
        result, mock_record = self._run_trust_session('\u274c invalid command')
        mock_record.assert_called_once()

    def test_trust_error_path_includes_exit_code_in_response(self):
        result, mock_record = self._run_trust_session('\u274c invalid command')
        body = json.loads(result['body'])
        content_text = json.loads(body['result']['content'][0]['text'])
        assert content_text['exit_code'] == -1

    def test_trust_success_path_no_record_execution_error(self):
        result, mock_record = self._run_trust_session('us-east-1')
        mock_record.assert_not_called()
