"""
Sprint 14 #71: Regression tests — request_id present in auto-approved MCP responses.

Validates that _check_auto_approve(), _check_trust_session(), and
_check_grant_session() all include 'request_id' in their response content.
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock, call


from paging import PaginatedOutput


def _parse_mcp_response(result: dict) -> dict:
    """Extract content dict from mcp_result response (Lambda body → jsonrpc result → content text)."""
    body = json.loads(result['body'])
    text = body['result']['content'][0]['text']
    return json.loads(text)


class TestAutoApprovedRequestId:
    """#71: auto_approved/trust_auto_approved/grant_auto_approved must include request_id."""

    def test_auto_approved_has_request_id(self, app_module):
        """_check_auto_approve() response must contain request_id."""
        import mcp_execute

        ctx = MagicMock()
        ctx.command = 'aws s3 ls'
        ctx.account_id = '111111111111'
        ctx.account_name = 'TestAccount'
        ctx.reason = 'regression test'
        ctx.source = 'test-source'
        ctx.req_id = 'req-auto-001'
        ctx.assume_role = None
        ctx.smart_decision = None
        ctx.is_native = False

        with patch('mcp_execute.is_auto_approve', return_value=True), \
             patch('mcp_execute.execute_command', return_value='output'), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output',
                   return_value=PaginatedOutput(paged=False, result='output')), \
             patch('mcp_execute.send_telegram_message_silent'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error'), \
             patch('mcp_execute.table', MagicMock()):
            result = mcp_execute._check_auto_approve(ctx)

        assert result is not None, "_check_auto_approve should return a result"
        content = _parse_mcp_response(result)
        assert content['status'] == 'auto_approved'
        assert 'request_id' in content, \
            f"auto_approved response missing 'request_id'. Keys: {sorted(content.keys())}"

    def test_trust_auto_approved_has_request_id(self, app_module):
        """_check_trust_session() response must contain request_id."""
        import mcp_execute

        ctx = MagicMock()
        ctx.command = 'aws s3 cp file.txt s3://bucket/'
        ctx.account_id = '111111111111'
        ctx.account_name = 'TestAccount'
        ctx.reason = 'regression test trust'
        ctx.source = 'trust-test-source'
        ctx.req_id = 'req-trust-001'
        ctx.assume_role = None
        ctx.smart_decision = None
        ctx.is_native = False
        ctx.trust_scope = 'trust-test-source'

        trust_session = {
            'request_id': 'trust-session-abc123',
            'source': 'trust-test-source',
            'account_id': '111111111111',
            'expires_at': 9999999999,
            'command_count': 0,
        }

        with patch('mcp_execute.should_trust_approve',
                   return_value=(True, trust_session, 'active_session')), \
             patch('mcp_execute.increment_trust_command_count', return_value=1), \
             patch('mcp_execute.execute_command', return_value='copied'), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output',
                   return_value=PaginatedOutput(paged=False, result='copied')), \
             patch('mcp_execute.send_trust_auto_approve_notification'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.track_command_executed'), \
             patch('mcp_execute.record_execution_error'), \
             patch('mcp_execute.table', MagicMock()):
            result = mcp_execute._check_trust_session(ctx)

        assert result is not None, "_check_trust_session should return a result"
        content = _parse_mcp_response(result)
        assert content['status'] == 'trust_auto_approved'
        assert 'request_id' in content, \
            f"trust_auto_approved response missing 'request_id'. Keys: {sorted(content.keys())}"

    def test_grant_auto_approved_has_request_id(self, app_module):
        """_check_grant_session() response must contain request_id."""
        import mcp_execute

        ctx = MagicMock()
        ctx.command = 'aws s3 ls s3://bucket/'
        ctx.account_id = '111111111111'
        ctx.account_name = 'TestAccount'
        ctx.reason = 'regression test grant'
        ctx.source = 'grant-test-source'
        ctx.req_id = 'req-grant-001'
        ctx.assume_role = None
        ctx.smart_decision = None
        ctx.is_native = False
        ctx.trust_scope = 'grant-test-source'
        ctx.grant_id = 'grant-xyz789'

        grant_session = {
            'grant_id': 'grant-xyz789',
            'status': 'active',
            'source': 'grant-test-source',
            'account_id': '111111111111',
            'expires_at': 9999999999,
            'granted_commands': ['aws s3 ls s3://bucket/'],
            'used_commands': {},
            'allow_repeat': True,
            'total_executions': 0,
            'max_total_executions': 50,
        }

        with patch('mcp_execute.GRANT_SESSION_ENABLED', True), \
             patch('grant.get_grant_session', return_value=grant_session), \
             patch('grant.normalize_command', side_effect=lambda cmd: cmd.strip()), \
             patch('grant.is_command_in_grant', return_value=True), \
             patch('grant.try_use_grant_command', return_value=True), \
             patch('mcp_execute.execute_command', return_value='bucket-contents'), \
             patch('mcp_execute.emit_metric'), \
             patch('mcp_execute.store_paged_output',
                   return_value=PaginatedOutput(paged=False, result='bucket-contents')), \
             patch('mcp_execute.send_grant_execute_notification'), \
             patch('mcp_execute.log_decision'), \
             patch('mcp_execute.record_execution_error'), \
             patch('mcp_execute.table', MagicMock()):
            result = mcp_execute._check_grant_session(ctx)

        assert result is not None, "_check_grant_session should return a result"
        content = _parse_mcp_response(result)
        assert content['status'] == 'grant_auto_approved'
        assert 'request_id' in content, \
            f"grant_auto_approved response missing 'request_id'. Keys: {sorted(content.keys())}"
