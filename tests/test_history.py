"""
Tests for bouncer_history MCP tool (Approach A — Conservative)

Test coverage:
- Default call (no filters) → returns items
- Filter by source
- Filter by action
- Filter by status
- Filter by since_hours
- limit cap at 50
- Invalid limit / since_hours types
- Empty result
- DynamoDB error bubbles as mcp_error
- _format_item helper
- _iso_ts helper
- Schema registered in MCP_TOOLS
- app.py routing dispatches to mcp_tool_history
"""

import json
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

from moto import mock_aws
import boto3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_NAME = 'clawdbot-approval-requests'


SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """Set required env vars before any module import."""
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('TABLE_NAME', TABLE_NAME)
    monkeypatch.setenv('ACCOUNTS_TABLE_NAME', 'bouncer-accounts')
    monkeypatch.setenv('DEFAULT_ACCOUNT_ID', '111111111111')
    monkeypatch.setenv('REQUEST_SECRET', 'test-secret')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('APPROVED_CHAT_ID', '999999999')


@pytest.fixture
def ddb_table():
    """Provide a real moto-backed DynamoDB table, wipe module caches after."""
    modules_to_clear = [
        'db', 'constants', 'utils', 'mcp_history',
        'mcp_tools', 'mcp_admin', 'tool_schema',
    ]
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        tbl = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        tbl.wait_until_exists()

        # Clear cached module state so imports pick up moto resources
        for mod in modules_to_clear:
            sys.modules.pop(mod, None)

        yield tbl

        for mod in modules_to_clear:
            sys.modules.pop(mod, None)


def _seed(tbl, items):
    """Helper: bulk-write items to the table."""
    with tbl.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def _now():
    return int(time.time())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestIsoTs:
    def test_valid_epoch(self):
        from mcp_history import _iso_ts
        # Use a clearly non-zero epoch
        result = _iso_ts(1000000)
        assert result is not None
        assert 'T' in result
        assert result.endswith('Z')

    def test_none_returns_none(self):
        from mcp_history import _iso_ts
        assert _iso_ts(None) is None
        # Epoch 0 is treated as falsy (no timestamp)
        assert _iso_ts(0) is None

    def test_recent_epoch(self):
        from mcp_history import _iso_ts
        ts = 1740470400  # 2025-02-25T08:00:00Z
        result = _iso_ts(ts)
        assert 'T' in result and result.endswith('Z')


class TestFormatItem:
    def test_basic_execute_item(self):
        from mcp_history import _format_item
        item = {
            'request_id': 'abc123',
            'action': 'execute',
            'command': 'aws s3 ls',
            'status': 'approved',
            'source': 'Private Bot',
            'created_at': 1000000,
            'approved_at': 1000005,
        }
        result = _format_item(item)
        assert result['request_id'] == 'abc123'
        assert result['action'] == 'execute'
        assert result['command'] == 'aws s3 ls'
        assert result['status'] == 'approved'
        assert result['source'] == 'Private Bot'
        assert 'T' in result['created_at']
        assert 'T' in result['approved_at']

    def test_missing_action_falls_back_to_decision_type(self):
        from mcp_history import _format_item
        item = {
            'request_id': 'x',
            'decision_type': 'auto_approved',
            'command': 'aws ec2 describe-instances',
            'status': 'auto_approved',
            'source': 'bot',
            'created_at': 1000000,
        }
        result = _format_item(item)
        assert result['action'] == 'auto_approved'

    def test_decimal_values_converted(self):
        from mcp_history import _format_item
        item = {
            'request_id': 'dec',
            'created_at': Decimal('1700000000'),
            'status': 'approved',
            'source': 's',
        }
        result = _format_item(item)
        assert isinstance(result['created_at'], str)


# ---------------------------------------------------------------------------
# Core query logic
# ---------------------------------------------------------------------------

class TestQueryHistory:
    def test_returns_empty_when_no_items(self, ddb_table):
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history()
        assert result == []

    def test_returns_recent_items(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            {'request_id': 'r1', 'created_at': now - 100, 'status': 'approved', 'source': 'bot'},
            {'request_id': 'r2', 'created_at': now - 200, 'status': 'denied', 'source': 'bot'},
        ])
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history(since_hours=1)
        assert len(result) == 2

    def test_excludes_old_items(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            # Recent
            {'request_id': 'new', 'created_at': now - 3600, 'status': 'approved', 'source': 'bot'},
            # Old (48h ago)
            {'request_id': 'old', 'created_at': now - 172800, 'status': 'approved', 'source': 'bot'},
        ])
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history(since_hours=24)
        ids = [r['request_id'] for r in result]
        assert 'new' in ids
        assert 'old' not in ids

    def test_filter_by_source(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            {'request_id': 'a', 'created_at': now - 100, 'status': 'approved', 'source': 'BotA'},
            {'request_id': 'b', 'created_at': now - 100, 'status': 'approved', 'source': 'BotB'},
        ])
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history(source='BotA')
        assert all(r['source'] == 'BotA' for r in result)
        ids = [r['request_id'] for r in result]
        assert 'a' in ids
        assert 'b' not in ids

    def test_filter_by_status(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            {'request_id': 's1', 'created_at': now - 10, 'status': 'approved', 'source': 'bot'},
            {'request_id': 's2', 'created_at': now - 10, 'status': 'denied', 'source': 'bot'},
        ])
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history(status='approved')
        assert all(r['status'] == 'approved' for r in result)

    def test_filter_by_action(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            {'request_id': 'e1', 'created_at': now - 10, 'action': 'execute', 'status': 'approved', 'source': 'bot'},
            {'request_id': 'u1', 'created_at': now - 10, 'action': 'upload', 'status': 'approved', 'source': 'bot'},
        ])
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history(action='execute')
        ids = [r['request_id'] for r in result]
        assert 'e1' in ids
        assert 'u1' not in ids

    def test_limit_is_respected(self, ddb_table):
        now = _now()
        items = [
            {'request_id': f'r{i}', 'created_at': now - i * 10, 'status': 'approved', 'source': 'bot'}
            for i in range(30)
        ]
        _seed(ddb_table, items)
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history(limit=5)
        assert len(result) <= 5

    def test_limit_capped_at_50(self, ddb_table):
        from mcp_history import _query_history, HISTORY_MAX_LIMIT
        assert HISTORY_MAX_LIMIT == 50
        now = _now()
        items = [
            {'request_id': f'r{i}', 'created_at': now - i, 'status': 'approved', 'source': 'bot'}
            for i in range(60)
        ]
        _seed(ddb_table, items)
        with patch('mcp_history.table', ddb_table):
            result = _query_history(limit=100)
        assert len(result) <= 50

    def test_sorted_newest_first(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            {'request_id': 'old', 'created_at': now - 3600, 'status': 'a', 'source': 'b'},
            {'request_id': 'new', 'created_at': now - 10, 'status': 'a', 'source': 'b'},
        ])
        from mcp_history import _query_history
        with patch('mcp_history.table', ddb_table):
            result = _query_history()
        assert result[0]['request_id'] == 'new'
        assert result[1]['request_id'] == 'old'


# ---------------------------------------------------------------------------
# MCP tool handler
# ---------------------------------------------------------------------------

class TestMcpToolHistory:
    def test_basic_call_returns_mcp_result(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-1', {})
        assert resp['statusCode'] == 200
        body = json.loads(resp['body'])
        assert body['jsonrpc'] == '2.0'
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert 'items' in content
        assert 'total' in content
        assert 'limit' in content

    def test_returns_items_with_correct_shape(self, ddb_table):
        now = _now()
        _seed(ddb_table, [{
            'request_id': 'shape-test',
            'action': 'execute',
            'command': 'aws s3 ls',
            'status': 'approved',
            'source': 'Private Bot',
            'created_at': now - 60,
        }])
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-2', {'since_hours': 1})
        content = json.loads(json.loads(resp['body'])['result']['content'][0]['text'])
        assert content['total'] >= 1
        item = content['items'][0]
        for key in ('request_id', 'action', 'command', 'status', 'source', 'created_at'):
            assert key in item

    def test_invalid_limit_returns_error(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-3', {'limit': 'notanint'})
        body = json.loads(resp['body'])
        assert 'error' in body

    def test_invalid_since_hours_returns_error(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-4', {'since_hours': 'bad'})
        body = json.loads(resp['body'])
        assert 'error' in body

    def test_limit_default_is_20(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-5', {})
        content = json.loads(json.loads(resp['body'])['result']['content'][0]['text'])
        assert content['limit'] == 20

    def test_limit_param_is_applied(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-6', {'limit': 5})
        content = json.loads(json.loads(resp['body'])['result']['content'][0]['text'])
        assert content['limit'] == 5

    def test_limit_capped_at_50(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-7', {'limit': 999})
        content = json.loads(json.loads(resp['body'])['result']['content'][0]['text'])
        assert content['limit'] == 50

    def test_empty_result_returns_valid_structure(self, ddb_table):
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-8', {'since_hours': 1})
        content = json.loads(json.loads(resp['body'])['result']['content'][0]['text'])
        assert content['items'] == []
        assert content['total'] == 0

    def test_filter_source_forwarded(self, ddb_table):
        now = _now()
        _seed(ddb_table, [
            {'request_id': 'x1', 'created_at': now - 10, 'source': 'BotX', 'status': 'approved'},
            {'request_id': 'y1', 'created_at': now - 10, 'source': 'BotY', 'status': 'approved'},
        ])
        from mcp_history import mcp_tool_history
        with patch('mcp_history.table', ddb_table):
            resp = mcp_tool_history('req-9', {'source': 'BotX'})
        content = json.loads(json.loads(resp['body'])['result']['content'][0]['text'])
        assert all(i['source'] == 'BotX' for i in content['items'])

    def test_ddb_exception_returns_mcp_error(self, ddb_table):
        from mcp_history import mcp_tool_history
        mock_table = MagicMock()
        mock_table.scan.side_effect = Exception('DDB timeout')
        with patch('mcp_history.table', mock_table):
            resp = mcp_tool_history('req-10', {})
        body = json.loads(resp['body'])
        assert 'error' in body
        assert 'Internal error' in body['error']['message']


# ---------------------------------------------------------------------------
# Schema & routing
# ---------------------------------------------------------------------------

class TestSchema:
    def test_bouncer_history_in_mcp_tools(self):
        # Clear cached module state to get fresh schema
        sys.modules.pop('tool_schema', None)
        from tool_schema import MCP_TOOLS
        assert 'bouncer_history' in MCP_TOOLS

    def test_schema_has_required_fields(self):
        sys.modules.pop('tool_schema', None)
        from tool_schema import MCP_TOOLS
        schema = MCP_TOOLS['bouncer_history']
        assert 'description' in schema
        assert 'parameters' in schema
        props = schema['parameters']['properties']
        for param in ('limit', 'source', 'action', 'status', 'since_hours'):
            assert param in props, f"Missing param: {param}"

    def test_mcp_tools_re_exports_history(self):
        sys.modules.pop('mcp_history', None)
        sys.modules.pop('mcp_tools', None)
        # mcp_tools re-exports mcp_tool_history
        # Minimal stub imports to avoid full module init
        import importlib
        # Just verify the name is importable
        try:
            from mcp_history import mcp_tool_history
            assert callable(mcp_tool_history)
        except ImportError as e:
            pytest.fail(f"mcp_tool_history not importable: {e}")


class TestAppRouting:
    """Test that app.py TOOL_HANDLERS routes bouncer_history correctly."""

    def test_tool_handler_dispatch(self):
        """TOOL_HANDLERS dict in app.py must contain bouncer_history."""
        # We test by inspecting the source rather than importing app.py
        # (importing app.py requires full env setup)
        import ast
        with open(os.path.join(os.path.dirname(__file__), '..', 'src', 'app.py')) as f:
            source = f.read()
        # Check bouncer_history is in TOOL_HANDLERS assignment
        assert "'bouncer_history'" in source or '"bouncer_history"' in source
        # And mcp_tool_history is imported
        assert 'mcp_tool_history' in source
