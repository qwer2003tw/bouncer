"""Tests for improved trust_scope missing error message (sprint9-005, closes #36)."""
import json
import pytest
from unittest.mock import patch


class TestTrustScopeMissingErrorMessage:
    """Verify that a missing trust_scope returns a helpful, example-rich error message."""

    def _call_execute(self, arguments: dict, app_module) -> dict:
        """Call bouncer_execute via MCP JSON-RPC and return the parsed response."""
        import json as _json
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': _json.dumps({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': arguments,
                },
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        return _json.loads(result['body'])

    def test_missing_trust_scope_returns_error(self, app_module):
        """When trust_scope is absent, response must be an error."""
        body = self._call_execute({'command': 'aws s3 ls', 'reason': 'test'}, app_module)
        # MCP error in body or content isError
        assert body.get('error') or (
            body.get('result', {}).get('isError') is True
        ), f"Expected error response: {body}"

    def test_missing_trust_scope_message_header(self, app_module):
        """Error message should contain the new header text."""
        body = self._call_execute({'command': 'aws s3 ls', 'reason': 'test'}, app_module)
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'Missing required parameter: trust_scope' in msg, f"msg={msg!r}"

    def test_missing_trust_scope_message_contains_description(self, app_module):
        """Error message should explain what trust_scope is."""
        body = self._call_execute({'command': 'aws s3 ls', 'reason': 'test'}, app_module)
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'stable caller identifier' in msg, f"msg={msg!r}"
        assert 'trust session matching' in msg, f"msg={msg!r}"

    def test_missing_trust_scope_message_contains_examples(self, app_module):
        """Error message should include concrete examples."""
        body = self._call_execute({'command': 'aws s3 ls', 'reason': 'test'}, app_module)
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'private-bot-main' in msg, f"msg={msg!r}"
        assert 'private-bot-deploy' in msg, f"msg={msg!r}"
        assert 'private-bot-kubectl' in msg, f"msg={msg!r}"

    def test_missing_trust_scope_message_contains_usage_hint(self, app_module):
        """Error message should tell users to use a consistent value."""
        body = self._call_execute({'command': 'aws s3 ls', 'reason': 'test'}, app_module)
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'consistent value' in msg, f"msg={msg!r}"
        assert 'auto-approval' in msg, f"msg={msg!r}"

    def test_empty_string_trust_scope_treated_as_missing(self, app_module):
        """An empty string trust_scope should trigger the same error."""
        body = self._call_execute(
            {'command': 'aws s3 ls', 'reason': 'test', 'trust_scope': ''},
            app_module,
        )
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'Missing required parameter: trust_scope' in msg, f"msg={msg!r}"

    def test_whitespace_only_trust_scope_treated_as_missing(self, app_module):
        """A whitespace-only trust_scope should trigger the same error."""
        body = self._call_execute(
            {'command': 'aws s3 ls', 'reason': 'test', 'trust_scope': '   '},
            app_module,
        )
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'Missing required parameter: trust_scope' in msg, f"msg={msg!r}"

    def test_missing_command_still_errors_on_command(self, app_module):
        """When both command and trust_scope are missing, command error comes first."""
        body = self._call_execute({'reason': 'test'}, app_module)
        msg = (
            body.get('error', {}).get('message', '')
            or json.loads(
                body.get('result', {}).get('content', [{}])[0].get('text', '{}')
            ).get('error', '')
        )
        assert 'command' in msg.lower(), f"msg={msg!r}"
