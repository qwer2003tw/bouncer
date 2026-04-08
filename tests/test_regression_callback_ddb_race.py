"""
Regression test for #242: callback 更新 UI 但 DDB status 不變

Bug: callbacks_command.py 的 _execute_and_store_result 和 _update_request_status
沒有 ConditionExpression，導致並發 callback 可能覆蓋已處理的記錄。
UI (answer_callback + update_message) 在 DDB update 之前執行。

Fix: 加入 ConditionExpression '#s = :pending' 確保 status 仍為 pending_approval，
並在 stale 時回傳錯誤。
"""
import time
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestRegressionCallbackDdbRace:
    """#242: DDB update should fail gracefully when status is no longer pending_approval."""

    @patch('callbacks_command.update_message')
    @patch('callbacks_command.answer_callback')
    @patch('callbacks_command.send_chat_action')
    @patch('callbacks_command.execute_command', return_value='ok')
    @patch('callbacks_command.store_paged_output')
    @patch('callbacks_command._get_table')
    @patch('callbacks_command.emit_metric')
    def test_execute_and_store_returns_stale_on_condition_failure(
        self, mock_metric, mock_table_fn, mock_paged, mock_exec, mock_chat,
        mock_answer, mock_update_msg,
    ):
        """_execute_and_store_result returns stale=True when DDB condition fails."""
        from botocore.exceptions import ClientError
        from callbacks_command import _execute_and_store_result

        mock_table = MagicMock()
        mock_table_fn.return_value = mock_table
        mock_table.update_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'condition not met'}},
            'UpdateItem'
        )
        mock_paged.return_value = {'paged': False, 'result': 'ok'}

        item = {
            'created_at': int(time.time()) - 10,
            'status': 'pending_approval',
        }
        result = _execute_and_store_result(
            command='aws s3 ls',
            assume_role=None,
            request_id='req-001',
            item=item,
            user_id='user-001',
            source_ip='1.2.3.4',
            action='approve',
        )

        assert result.get('stale') is True
        assert result.get('result') == 'ok'

    @patch('callbacks_command.update_message')
    @patch('callbacks_command.answer_callback')
    @patch('callbacks_command.send_chat_action')
    @patch('callbacks_command.execute_command', return_value='ok')
    @patch('callbacks_command.store_paged_output')
    @patch('callbacks_command._get_table')
    @patch('callbacks_command.emit_metric')
    def test_handle_command_callback_shows_stale_message(
        self, mock_metric, mock_table_fn, mock_paged, mock_exec, mock_chat,
        mock_answer, mock_update_msg,
    ):
        """handle_command_callback shows stale message when DDB update fails."""
        from botocore.exceptions import ClientError
        from callbacks_command import handle_command_callback

        mock_table = MagicMock()
        mock_table_fn.return_value = mock_table
        mock_table.update_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'condition not met'}},
            'UpdateItem'
        )
        mock_paged.return_value = {'paged': False, 'result': 'ok'}

        item = {
            'command': 'aws s3 ls',
            'assume_role': None,
            'source': 'test',
            'trust_scope': '',
            'reason': 'test',
            'context': '',
            'account_id': '111111111111',
            'account_name': 'Default',
            'status': 'pending_approval',
            'created_at': int(time.time()) - 10,
        }

        handle_command_callback(
            action='approve',
            request_id='req-001',
            item=item,
            message_id=12345,
            callback_id='cb-001',
            user_id='user-001',
        )

        # Should show stale message
        stale_calls = [
            call for call in mock_update_msg.call_args_list
            if '已被處理' in str(call)
        ]
        assert len(stale_calls) > 0, "Expected stale message in update_message calls"

    @patch('callbacks_command._get_table')
    def test_update_request_status_returns_false_on_stale(self, mock_table_fn):
        """_update_request_status returns False when DDB condition fails."""
        from botocore.exceptions import ClientError
        from callbacks_command import _update_request_status

        mock_table = MagicMock()
        mock_table_fn.return_value = mock_table
        mock_table.update_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'condition not met'}},
            'UpdateItem'
        )

        result = _update_request_status(mock_table, 'req-001', 'denied', 'user-001')
        assert result is False

    @patch('callbacks_command._get_table')
    def test_update_request_status_returns_true_on_success(self, mock_table_fn):
        """_update_request_status returns True on successful update."""
        from callbacks_command import _update_request_status

        mock_table = MagicMock()
        mock_table_fn.return_value = mock_table
        mock_table.update_item.return_value = {}

        result = _update_request_status(mock_table, 'req-001', 'denied', 'user-001')
        assert result is True

        # Verify ConditionExpression was passed
        call_kwargs = mock_table.update_item.call_args.kwargs
        assert 'ConditionExpression' in call_kwargs
        assert ':pending' in str(call_kwargs['ExpressionAttributeValues'])
