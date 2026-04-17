"""Regression test: bouncer_query_logs / bouncer_logs_allowlist must verify
log group existence before sending approval or adding to allowlist.

Bug: a non-existent log group could be approved and added to the allowlist
because the system never checked describe_log_groups before proceeding.
"""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock


pytestmark = pytest.mark.xdist_group("query_logs_validate")


# ---------------------------------------------------------------------------
# Shared env + module reload fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env_and_modules():
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')
    os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
    os.environ.setdefault('APPROVED_CHAT_ID', '999999999')
    os.environ.setdefault('REQUEST_SECRET', 'test-secret')

    for mod in list(sys.modules):
        if mod in ('mcp_query_logs',):
            del sys.modules[mod]
    yield


# ---------------------------------------------------------------------------
# Import helper — pre-mock heavy deps that fail on Python <3.10
# ---------------------------------------------------------------------------

def _import_mcp_query_logs():
    """Import mcp_query_logs with heavy transitive deps mocked."""
    stubs = {}
    for mod_name in ('commands', 'notifications'):
        if mod_name not in sys.modules:
            stubs[mod_name] = sys.modules[mod_name] = MagicMock()

    sys.modules.pop('mcp_query_logs', None)
    import mcp_query_logs

    for mod_name, stub in stubs.items():
        if sys.modules.get(mod_name) is stub:
            del sys.modules[mod_name]

    return mcp_query_logs


def _parse_body(http_resp):
    """Parse the JSON-RPC body from an HTTP response dict."""
    return json.loads(http_resp['body'])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logs_client_stub(existing_groups: list):
    """Return a mock logs client whose describe_log_groups returns only listed groups."""
    client = MagicMock()

    def _describe(logGroupNamePrefix='', limit=1, **kw):
        matched = [g for g in existing_groups if g.startswith(logGroupNamePrefix)]
        return {'logGroups': [{'logGroupName': g} for g in matched[:limit]]}

    client.describe_log_groups.side_effect = _describe
    return client


# ---------------------------------------------------------------------------
# Tests — query_logs
# ---------------------------------------------------------------------------

class TestQueryLogsValidateGroupExists:
    """bouncer_query_logs should reject non-existent log groups before approval."""

    def test_regression_nonexistent_group_returns_error(self):
        """Non-existent log group -> error, no Telegram approval sent."""
        mod = _import_mcp_query_logs()

        with patch.object(mod, '_get_logs_client', return_value=_make_logs_client_stub([])), \
             patch.object(mod, 'initialize_default_allowlist'), \
             patch.object(mod, '_check_allowlist', return_value=False), \
             patch.object(mod, 'table') as mock_table, \
             patch.object(mod, 'send_telegram_message') as mock_tg, \
             patch.object(mod, 'post_notification_setup'):

            result = mod.mcp_tool_query_logs('req-1', {
                'log_group': '/aws/lambda/does-not-exist',
                'start_time': '-1h',
            })

            body = _parse_body(result)
            assert 'error' in body
            assert '不存在' in body['error']['message']

            mock_tg.assert_not_called()
            mock_table.put_item.assert_not_called()

    def test_existing_group_proceeds_to_approval(self):
        """Existing log group (not in allowlist) -> approval flow proceeds."""
        mod = _import_mcp_query_logs()

        with patch.object(mod, '_get_logs_client', return_value=_make_logs_client_stub(
                ['/aws/lambda/my-function'])), \
             patch.object(mod, 'initialize_default_allowlist'), \
             patch.object(mod, '_check_allowlist', return_value=False), \
             patch.object(mod, 'table'), \
             patch.object(mod, 'send_telegram_message',
                          return_value={'ok': True, 'result': {'message_id': 1}}) as mock_tg, \
             patch.object(mod, 'post_notification_setup'):

            result = mod.mcp_tool_query_logs('req-2', {
                'log_group': '/aws/lambda/my-function',
                'start_time': '-1h',
            })

            body = _parse_body(result)
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'pending_approval'

            mock_tg.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — allowlist add
# ---------------------------------------------------------------------------

class TestAllowlistAddValidateGroupExists:
    """bouncer_logs_allowlist add should reject non-existent log groups."""

    def test_regression_add_nonexistent_group_rejected(self):
        mod = _import_mcp_query_logs()

        with patch.object(mod, '_get_logs_client', return_value=_make_logs_client_stub([])), \
             patch.object(mod, 'table') as mock_table:

            result = mod.mcp_tool_logs_allowlist('req-3', {
                'action': 'add',
                'log_group': '/aws/lambda/does-not-exist',
            })

            body = _parse_body(result)
            assert 'error' in body
            assert '不存在' in body['error']['message']
            mock_table.put_item.assert_not_called()

    def test_add_existing_group_succeeds(self):
        mod = _import_mcp_query_logs()

        with patch.object(mod, '_get_logs_client', return_value=_make_logs_client_stub(
                ['/aws/lambda/my-function'])), \
             patch.object(mod, 'table') as mock_table:

            result = mod.mcp_tool_logs_allowlist('req-4', {
                'action': 'add',
                'log_group': '/aws/lambda/my-function',
            })

            body = _parse_body(result)
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'added'
            mock_table.put_item.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — allowlist add_batch
# ---------------------------------------------------------------------------

class TestAllowlistAddBatchValidateGroupExists:
    """bouncer_logs_allowlist add_batch should skip non-existent log groups."""

    def test_regression_add_batch_filters_nonexistent(self):
        mod = _import_mcp_query_logs()

        with patch.object(mod, '_get_logs_client', return_value=_make_logs_client_stub(
                ['/aws/lambda/exists-one'])), \
             patch.object(mod, 'table'):

            result = mod.mcp_tool_logs_allowlist('req-5', {
                'action': 'add_batch',
                'log_groups': [
                    '/aws/lambda/exists-one',
                    '/aws/lambda/does-not-exist',
                ],
            })

            body = _parse_body(result)
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert content['added'] == ['/aws/lambda/exists-one']
            assert len(content['errors']) == 1
            assert '不存在' in content['errors'][0]['error']
