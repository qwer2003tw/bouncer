"""
Tests for sprint11-011: send_chat_action typing visual feedback
Covers:
  - telegram.send_chat_action exists and calls _telegram_request('sendChatAction', ...)
  - send_chat_action silently suppresses exceptions (fire-and-forget)
  - handle_mcp_tool_call triggers send_chat_action at the start of processing
"""
import os
import pytest
from unittest.mock import patch, MagicMock, call


os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')


# ---------------------------------------------------------------------------
# Unit tests for telegram.send_chat_action
# ---------------------------------------------------------------------------

class TestSendChatActionFunction:
    """Unit tests for telegram.send_chat_action()"""

    def test_send_chat_action_exists(self):
        """send_chat_action function exists in telegram module"""
        import telegram
        assert hasattr(telegram, 'send_chat_action'), \
            "telegram.send_chat_action function not found"
        assert callable(telegram.send_chat_action)

    def test_send_chat_action_in_all(self):
        """send_chat_action is exported in __all__"""
        import telegram
        assert 'send_chat_action' in telegram.__all__

    def test_send_chat_action_calls_telegram_request(self):
        """send_chat_action calls _telegram_request with sendChatAction"""
        import telegram
        with patch('telegram._telegram_request') as mock_req:
            telegram.send_chat_action('typing')
        mock_req.assert_called_once()
        args = mock_req.call_args[0]
        assert args[0] == 'sendChatAction'

    def test_send_chat_action_passes_action_param(self):
        """send_chat_action passes correct action value in data dict"""
        import telegram
        with patch('telegram._telegram_request') as mock_req:
            telegram.send_chat_action('typing')
        data = mock_req.call_args[0][1]
        assert data.get('action') == 'typing'

    def test_send_chat_action_passes_chat_id(self):
        """send_chat_action includes chat_id from APPROVED_CHAT_ID"""
        import telegram
        with patch('telegram._telegram_request') as mock_req:
            telegram.send_chat_action('typing')
        data = mock_req.call_args[0][1]
        assert 'chat_id' in data

    def test_send_chat_action_default_is_typing(self):
        """Default action argument is 'typing'"""
        import telegram
        with patch('telegram._telegram_request') as mock_req:
            telegram.send_chat_action()
        data = mock_req.call_args[0][1]
        assert data.get('action') == 'typing'

    def test_send_chat_action_does_not_raise_on_exception(self):
        """send_chat_action suppresses exceptions (fire-and-forget)"""
        import telegram
        with patch('telegram._telegram_request', side_effect=Exception('network error')):
            telegram.send_chat_action('typing')

    def test_send_chat_action_returns_none(self):
        """send_chat_action returns None"""
        import telegram
        with patch('telegram._telegram_request', return_value={}):
            result = telegram.send_chat_action('typing')
        assert result is None


# ---------------------------------------------------------------------------
# Integration tests: handle_mcp_tool_call triggers typing indicator
# ---------------------------------------------------------------------------

class TestHandleMcpToolCallTyping:
    """Tests that handle_mcp_tool_call sends typing action at start"""

    def test_handle_mcp_tool_call_sends_chat_action(self):
        """handle_mcp_tool_call calls send_chat_action('typing') once"""
        with patch('app.send_chat_action') as mock_action, \
             patch('app.TOOL_HANDLERS', {'bouncer_status': MagicMock(return_value={'statusCode': 200, 'body': '{}'})}), \
             patch('app.emit_metric'):
            import app
            app.handle_mcp_tool_call('req-001', 'bouncer_status', {})
        mock_action.assert_called_once_with('typing')

    def test_handle_mcp_tool_call_typing_before_handler(self):
        """send_chat_action is called before the tool handler"""
        call_order = []

        def record_typing(*args, **kwargs):
            call_order.append('typing')

        def record_handler(*args, **kwargs):
            call_order.append('handler')
            return {'statusCode': 200, 'body': '{}'}

        with patch('app.send_chat_action', side_effect=record_typing), \
             patch('app.TOOL_HANDLERS', {'bouncer_status': record_handler}), \
             patch('app.emit_metric'):
            import app
            app.handle_mcp_tool_call('req-001', 'bouncer_status', {})

        assert call_order.index('typing') < call_order.index('handler'), \
            "send_chat_action should be called before the tool handler"
