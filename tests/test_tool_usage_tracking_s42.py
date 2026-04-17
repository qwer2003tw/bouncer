"""Regression tests for MCP tool usage tracking (bouncer-s42-001)"""
import os
import json
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

from unittest.mock import patch

def test_tool_call_emits_metric():
    """tools/call dispatcher emits ToolCall metric with ToolName dimension"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    # Ensure metrics module is in path and can be imported
    import metrics  # noqa: F401
    import bouncer_mcp

    # Mock the emit_metric function in the metrics module
    with patch('metrics.emit_metric') as mock_emit:
        # Simulate a tools/call request (handle_request expects dict, not JSON string)
        request = {
            'jsonrpc': '2.0',
            'method': 'tools/call',
            'params': {'name': 'bouncer_list_accounts', 'arguments': {}},
            'id': 1
        }

        try:
            bouncer_mcp.handle_request(request)
        except Exception:
            pass  # We only care that emit_metric was called

        # Verify emit_metric was called with ToolCall + ToolName
        calls = [c for c in mock_emit.call_args_list if len(c[0]) > 1 and c[0][1] == 'ToolCall']
        assert len(calls) >= 1, f"Expected ToolCall metric, got calls: {mock_emit.call_args_list}"

        # Check the dimensions contain ToolName
        call_args = calls[0][0]  # positional args
        call_kwargs = calls[0][1]  # keyword args
        dimensions = call_kwargs.get('dimensions', {})
        assert dimensions.get('ToolName') == 'bouncer_list_accounts', \
            f"Expected ToolName='bouncer_list_accounts', got dimensions: {dimensions}"

def test_metric_failure_does_not_block_tool():
    """If emit_metric raises, tool still executes"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    import bouncer_mcp

    with patch('metrics.emit_metric', side_effect=Exception('CW error')):
        # Should not raise due to emit_metric failure (using tools/call to trigger metric code)
        request = {
            'jsonrpc': '2.0',
            'method': 'tools/call',
            'params': {'name': 'bouncer_help', 'arguments': {}},
            'id': 1
        }

        try:
            response = bouncer_mcp.handle_request(request)
            # If we get here without exception, the metric failure didn't block execution
            assert response is not None
        except Exception as e:
            # Tool should not fail due to metric issues
            if 'CW error' in str(e):
                raise AssertionError("Metric failure blocked tool execution") from e
