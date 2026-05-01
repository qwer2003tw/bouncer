"""Tests for callbacks_query_logs module."""
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from unittest.mock import patch


class TestHandleQueryLogsCallback:
    """Tests for handle_query_logs_callback function."""

    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.safe_get_item')
    def test_request_not_found(self, mock_get_item, mock_answer):
        """Test handling when request doesn't exist in DDB."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = None

        callback = {'id': 'callback-123', 'message': {'message_id': 456}}
        result = handle_query_logs_callback(
            'approve_query_logs', 'req-missing', callback, '789'
        )

        assert result['statusCode'] == 404
        mock_answer.assert_called_once_with('callback-123', '❌ 請求已過期或不存在')

    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.safe_get_item')
    def test_request_already_processed(self, mock_get_item, mock_answer):
        """Test handling when request already processed."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = {'status': 'approved'}

        callback = {'id': 'callback-123', 'message': {'message_id': 456}}
        result = handle_query_logs_callback(
            'approve_query_logs', 'req-processed', callback, '789'
        )

        assert result['statusCode'] == 200
        mock_answer.assert_called_once_with('callback-123', '⚠️ 此請求已處理過')

    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.safe_get_item')
    @patch('callbacks_query_logs.time.time')
    def test_request_expired(self, mock_time, mock_get_item, mock_answer, mock_update):
        """Test handling when request has expired (TTL)."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_time.return_value = 2000
        mock_get_item.return_value = {
            'status': 'pending_approval',
            'ttl': 1500,
            'log_group': '/aws/lambda/test'
        }

        callback = {'id': 'callback-123', 'message': {'message_id': 456}}
        result = handle_query_logs_callback(
            'approve_query_logs', 'req-expired', callback, '789'
        )

        assert result['statusCode'] == 200
        mock_answer.assert_called_once_with('callback-123', '⏰ 此請求已過期')
        mock_update.assert_called_once()

    @patch('callbacks_query_logs.emit_metric')
    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.table')
    @patch('callbacks_query_logs.safe_get_item')
    def test_deny_action(self, mock_get_item, mock_table, mock_answer,
                        mock_update_status, mock_update_msg, mock_metric):
        """Test deny_query_logs action."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = {
            'status': 'pending_approval',
            'log_group': '/aws/lambda/test',
            'account_id': '123456789012'
        }

        callback = {'id': 'callback-deny', 'message': {'message_id': 789}}
        result = handle_query_logs_callback(
            'deny_query_logs', 'req-deny', callback, 'user-123'
        )

        assert result['statusCode'] == 200
        mock_answer.assert_called_once_with('callback-deny', '❌ 已拒絕')
        mock_update_status.assert_called_once_with(
            mock_table, 'req-deny', 'denied', 'user-123'
        )
        mock_update_msg.assert_called_once()
        mock_metric.assert_called_once()

    @patch('callbacks_query_logs.emit_metric')
    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs.execute_log_insights')
    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.table')
    @patch('callbacks_query_logs.safe_get_item')
    def test_approve_one_time_success(self, mock_get_item, mock_table, mock_answer,
                                     mock_execute, mock_update_status,
                                     mock_update_msg, mock_metric):
        """Test approve_query_logs action with successful query."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = {
            'status': 'pending_approval',
            'log_group': '/aws/lambda/test',
            'account_id': '123456789012',
            'assume_role_arn': '',
            'query': 'fields @timestamp | limit 10',
            'start_time': 1000,
            'end_time': 2000,
            'region': 'us-east-1'
        }

        mock_execute.return_value = {
            'status': 'complete',
            'records_matched': 42,
            'statistics': {'bytes_scanned': 1024},
            'results': [{'field': 'value'}]
        }

        callback = {'id': 'callback-approve', 'message': {'message_id': 999}}
        result = handle_query_logs_callback(
            'approve_query_logs', 'req-approve', callback, 'user-456'
        )

        assert result['statusCode'] == 200
        mock_answer.assert_called_once()
        mock_execute.assert_called_once()

        # Verify status update was called with result in extra_attrs
        assert mock_update_status.called
        call_args = mock_update_status.call_args[0]  # positional args
        call_kwargs = mock_update_status.call_args[1]  # keyword args
        assert call_args[2] == 'approved'  # status
        assert 'result' in call_kwargs['extra_attrs']

        # Verify message updates (progress + final)
        assert mock_update_msg.call_count == 2

    @patch('callbacks_query_logs.emit_metric')
    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs._add_to_allowlist')
    @patch('callbacks_query_logs.execute_log_insights')
    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.table')
    @patch('callbacks_query_logs.safe_get_item')
    def test_approve_add_allowlist_success(self, mock_get_item, mock_table, mock_answer,
                                          mock_execute, mock_add_allowlist,
                                          mock_update_status, mock_update_msg, mock_metric):
        """Test approve_add_allowlist action."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = {
            'status': 'pending_approval',
            'log_group': '/aws/lambda/important',
            'account_id': '123456789012',
            'query': 'fields @message',
            'start_time': 1000,
            'end_time': 2000,
            'region': 'us-west-2'
        }

        mock_execute.return_value = {
            'status': 'complete',
            'records_matched': 10,
            'statistics': {'bytes_scanned': 512},
            'results': []
        }

        callback = {'id': 'callback-allowlist', 'message': {'message_id': 111}}
        result = handle_query_logs_callback(
            'approve_add_allowlist', 'req-allowlist', callback, 'user-789'
        )

        assert result['statusCode'] == 200

        # Verify allowlist addition
        mock_add_allowlist.assert_called_once_with(
            '123456789012', '/aws/lambda/important', added_by='telegram:user-789'
        )

        # Verify added_to_allowlist flag in extra_attrs
        call_kwargs = mock_update_status.call_args[1]
        assert call_kwargs['extra_attrs']['added_to_allowlist'] is True

    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs.execute_log_insights')
    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.table')
    @patch('callbacks_query_logs.safe_get_item')
    def test_approve_query_error(self, mock_get_item, mock_table, mock_answer,
                                mock_execute, mock_update_status, mock_update_msg):
        """Test handling query execution error."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = {
            'status': 'pending_approval',
            'log_group': '/aws/lambda/fail',
            'account_id': '123456789012',
            'query': 'invalid query',
            'start_time': 1000,
            'end_time': 2000,
            'region': 'us-east-1'
        }

        mock_execute.return_value = {
            'status': 'error',
            'error': 'Invalid query syntax'
        }

        callback = {'id': 'callback-error', 'message': {'message_id': 222}}
        result = handle_query_logs_callback(
            'approve_query_logs', 'req-error', callback, 'user-999'
        )

        assert result['statusCode'] == 200

        # Verify error is recorded in extra_attrs
        call_kwargs = mock_update_status.call_args[1]
        result_json = json.loads(call_kwargs['extra_attrs']['result'])
        assert result_json['status'] == 'error'

    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs.execute_log_insights')
    @patch('callbacks_query_logs.answer_callback')
    @patch('callbacks_query_logs.table')
    @patch('callbacks_query_logs.safe_get_item')
    def test_approve_query_still_running(self, mock_get_item, mock_table, mock_answer,
                                        mock_execute, mock_update_status, mock_update_msg):
        """Test handling query still running status."""
        from callbacks_query_logs import handle_query_logs_callback

        mock_get_item.return_value = {
            'status': 'pending_approval',
            'log_group': '/aws/lambda/slow',
            'account_id': '123456789012',
            'query': 'fields @message | limit 10000',
            'start_time': 1000,
            'end_time': 2000,
            'region': 'us-east-1'
        }

        mock_execute.return_value = {
            'status': 'running',
            'query_id': 'query-abc-123'
        }

        callback = {'id': 'callback-running', 'message': {'message_id': 333}}
        result = handle_query_logs_callback(
            'approve_query_logs', 'req-running', callback, 'user-111'
        )

        assert result['statusCode'] == 200

        # Verify running status is recorded
        call_kwargs = mock_update_status.call_args[1]
        result_json = json.loads(call_kwargs['extra_attrs']['result'])
        assert result_json['status'] == 'running'
        assert result_json['query_id'] == 'query-abc-123'


class TestHandleQueryError:
    """Tests for _handle_query_error helper."""

    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs.table')
    def test_handle_query_error(self, mock_table, mock_update_status, mock_update_msg):
        """Test _handle_query_error updates status and message."""
        from callbacks_query_logs import _handle_query_error

        result = _handle_query_error(
            'req-err', 'Query timeout', '/aws/lambda/test', 555, 'user-333'
        )

        assert result['statusCode'] == 200

        # Verify status update - checking positional args
        mock_update_status.assert_called_once()
        call_args = mock_update_status.call_args[0]
        assert call_args[2] == 'approved'  # status parameter

        # Verify message update
        mock_update_msg.assert_called_once()


class TestHandleQueryRunning:
    """Tests for _handle_query_running helper."""

    @patch('callbacks_query_logs.update_message')
    @patch('callbacks_query_logs._update_request_status')
    @patch('callbacks_query_logs.table')
    def test_handle_query_running(self, mock_table, mock_update_status, mock_update_msg):
        """Test _handle_query_running updates status and message."""
        from callbacks_query_logs import _handle_query_running

        result = _handle_query_running(
            'req-run', 'query-xyz-789', '/aws/lambda/test', 666, 'user-444'
        )

        assert result['statusCode'] == 200

        # Verify status update
        mock_update_status.assert_called_once()
        call_kwargs = mock_update_status.call_args[1]
        result_json = json.loads(call_kwargs['extra_attrs']['result'])
        assert result_json['status'] == 'running'
        assert result_json['query_id'] == 'query-xyz-789'

        # Verify message update
        mock_update_msg.assert_called_once()
