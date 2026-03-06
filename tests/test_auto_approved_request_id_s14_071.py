"""
Sprint 14 #71: Regression tests — request_id present in auto-approved MCP responses.

Validates that _check_auto_approve(), _check_trust_session(), and
_check_grant_session() all include 'request_id' in their response content.
"""
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


@pytest.fixture(scope='module')
def mcp_execute_mod():
    import importlib
    import mcp_execute
    return mcp_execute


class TestAutoApprovedRequestId:
    """#71: auto_approved response must include request_id."""

    def test_auto_approved_has_request_id(self, app_module):
        """_check_auto_approve() response must contain request_id."""
        import mcp_execute
        ctx = MagicMock()
        ctx.command = 'aws s3 ls'
        ctx.account_id = '111111111111'
        ctx.account_name = 'Test'
        ctx.reason = 'test reason'
        ctx.source = 'test-source'
        ctx.req_id = 'test-req-001'
        ctx.assume_role = None
        ctx.smart_decision = None

        with patch.object(mcp_execute, 'is_auto_approve', return_value=True), \
             patch.object(mcp_execute, 'execute_command', return_value='bucket-list'), \
             patch.object(mcp_execute, 'emit_metric'), \
             patch.object(mcp_execute, 'store_paged_output',
                          return_value={'result': 'bucket-list', 'paged': False}), \
             patch.object(mcp_execute, 'send_telegram_message_silent'), \
             patch.object(mcp_execute, 'log_decision'), \
             patch.object(mcp_execute, 'table', MagicMock()):
            result = mcp_execute._check_auto_approve(ctx)

        assert result is not None
        body = json.loads(result['content'][0]['text'])
        assert 'request_id' in body, \
            f"auto_approved response missing request_id. Keys: {list(body.keys())}"
        assert body['status'] == 'auto_approved'

    def test_trust_auto_approved_has_request_id(self, app_module):
        """_check_trust_session() response must contain request_id."""
        import mcp_execute
        ctx = MagicMock()
        ctx.command = 'aws s3 cp file.txt s3://bucket/'
        ctx.account_id = '111111111111'
        ctx.account_name = 'Test'
        ctx.reason = 'test reason'
        ctx.source = 'trust-test-source'
        ctx.req_id = 'test-req-002'
        ctx.assume_role = None
        ctx.smart_decision = None
        ctx.trust_scope = 'trust-test-source'

        trust_session = {
            'request_id': 'trust-session-abc123',
            'source': 'trust-test-source',
            'account_id': '111111111111',
            'expires_at': 9999999999,
            'command_count': 0,
        }

        with patch.object(mcp_execute, 'should_trust_approve', return_value=True), \
             patch.object(mcp_execute, 'get_active_trust_session', return_value=trust_session), \
             patch.object(mcp_execute, 'execute_command', return_value='copied'), \
             patch.object(mcp_execute, 'emit_metric'), \
             patch.object(mcp_execute, 'store_paged_output',
                          return_value={'result': 'copied', 'paged': False}), \
             patch.object(mcp_execute, 'send_telegram_message_silent'), \
             patch.object(mcp_execute, 'log_decision'), \
             patch.object(mcp_execute, 'track_command_executed'), \
             patch.object(mcp_execute, 'increment_trust_command_count',
                          return_value=(1, '29 minutes')), \
             patch.object(mcp_execute, 'table', MagicMock()):
            result = mcp_execute._check_trust_session(ctx)

        assert result is not None
        body = json.loads(result['content'][0]['text'])
        assert 'request_id' in body, \
            f"trust_auto_approved response missing request_id. Keys: {list(body.keys())}"
        assert body['status'] == 'trust_auto_approved'

    def test_grant_auto_approved_has_request_id(self, app_module):
        """_check_grant_session() response must contain request_id."""
        import mcp_execute
        ctx = MagicMock()
        ctx.command = 'aws s3 ls s3://bucket/'
        ctx.account_id = '111111111111'
        ctx.account_name = 'Test'
        ctx.reason = 'test reason'
        ctx.source = 'grant-test-source'
        ctx.req_id = 'test-req-003'
        ctx.assume_role = None
        ctx.smart_decision = None
        ctx.trust_scope = 'grant-test-source'

        grant_session = {
            'grant_id': 'grant-xyz789',
            'commands': ['aws s3 ls', 'aws s3 ls s3://bucket/'],
            'source': 'grant-test-source',
            'account_id': '111111111111',
            'expires_at': 9999999999,
            'command_count': 0,
            'max_commands': 10,
        }

        with patch.object(mcp_execute, 'get_active_grant_session', return_value=grant_session), \
             patch.object(mcp_execute, 'is_command_grantable', return_value=True), \
             patch.object(mcp_execute, 'execute_command', return_value='bucket-contents'), \
             patch.object(mcp_execute, 'emit_metric'), \
             patch.object(mcp_execute, 'store_paged_output',
                          return_value={'result': 'bucket-contents', 'paged': False}), \
             patch.object(mcp_execute, 'send_telegram_message_silent'), \
             patch.object(mcp_execute, 'log_decision'), \
             patch.object(mcp_execute, 'increment_grant_command_count',
                          return_value=(1, '9 remaining')), \
             patch.object(mcp_execute, 'table', MagicMock()):
            result = mcp_execute._check_grant_session(ctx)

        assert result is not None
        body = json.loads(result['content'][0]['text'])
        assert 'request_id' in body, \
            f"grant_auto_approved response missing request_id. Keys: {list(body.keys())}"
        assert body['status'] == 'grant_auto_approved'
