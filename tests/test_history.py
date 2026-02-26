"""
tests/test_history.py — bouncer_history + bouncer_stats MCP tool tests

Strategy: use moto mock_aws so that DynamoDB is real (in-memory) and
we don't need to talk to AWS.
"""

import base64
import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Helpers to bootstrap the src/ path before importing any bouncer module
# ---------------------------------------------------------------------------

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REQUESTS_TABLE = "clawdbot-approval-requests"
CMD_HISTORY_TABLE = "bouncer-prod-command-history"
ACCOUNTS_TABLE = "bouncer-accounts"


@pytest.fixture(scope="module")
def aws_env():
    """Set env vars and clean module cache once per module."""
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("DEFAULT_ACCOUNT_ID", "111111111111")
    os.environ.setdefault("TABLE_NAME", REQUESTS_TABLE)
    os.environ.setdefault("REQUEST_SECRET", "test-secret")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    os.environ.setdefault("APPROVED_CHAT_ID", "999999999")
    yield


@pytest.fixture()
def mock_ddb(aws_env):
    """Spin up fresh moto DynamoDB tables for each test."""
    # Purge any cached boto3 / module refs
    for mod in list(sys.modules.keys()):
        if mod in ("db", "mcp_history", "constants", "utils") or mod.startswith("src."):
            del sys.modules[mod]

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        # Main requests table
        requests_tbl = dynamodb.create_table(
            TableName=REQUESTS_TABLE,
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "request_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "N"},
                {"AttributeName": "source", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-created-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "source-created-index",
                    "KeySchema": [
                        {"AttributeName": "source", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        requests_tbl.wait_until_exists()

        # Command-history table (optional — tests can skip it)
        cmd_tbl = dynamodb.create_table(
            TableName=CMD_HISTORY_TABLE,
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "request_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        cmd_tbl.wait_until_exists()

        # Accounts table (needed by db.py import chain)
        dynamodb.create_table(
            TableName=ACCOUNTS_TABLE,
            KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "account_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield dynamodb, requests_tbl, cmd_tbl


def _seed_requests(requests_tbl, items):
    """Bulk-write items to the requests table."""
    with requests_tbl.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def _seed_cmd_history(cmd_tbl, items):
    """Bulk-write items to the command-history table."""
    with cmd_tbl.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def _make_request(
    req_id,
    status="approved",
    action="execute",
    source="test-bot",
    account_id="111111111111",
    created_offset=-100,
    approved_offset=None,
):
    now = int(time.time())
    item = {
        "request_id": req_id,
        "status": status,
        "action": action,
        "source": source,
        "account_id": account_id,
        "created_at": now + created_offset,
        "command": f"aws ec2 describe-instances --req {req_id}",
        "reason": "test",
    }
    if approved_offset is not None:
        item["approved_at"] = now + approved_offset
    return item


# ---------------------------------------------------------------------------
# Import helper — always re-import inside moto context
# ---------------------------------------------------------------------------


def _import_mcp_history(requests_tbl):
    """Import mcp_history with db.table patched to the mock table."""
    # Clear any cached modules so the new import picks up the moto context
    for mod in list(sys.modules.keys()):
        if "mcp_history" in mod or mod == "db":
            del sys.modules[mod]

    import mcp_history as mh

    # Patch the module-level `table` reference
    mh.table = requests_tbl
    return mh


# ===========================================================================
# bouncer_history tests
# ===========================================================================


class TestBouncerHistoryBasic:
    def test_returns_empty_when_no_data(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        result = mh.mcp_tool_history("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["items"] == []
        assert data["total_scanned"] == 0
        assert data["next_page_token"] is None

    def test_returns_items_within_time_window(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        now = int(time.time())
        # Recent item (within 24h)
        _seed_requests(requests_tbl, [
            _make_request("r1", created_offset=-3600),       # 1h ago
            _make_request("r2", created_offset=-90000),      # 25h ago (outside window)
        ])

        mh = _import_mcp_history(requests_tbl)
        # Patch _get_command_history_table to return None so we don't need cmd table
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"since_hours": 24})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert len(data["items"]) == 1
        assert data["items"][0]["request_id"] == "r1"

    def test_limit_respected(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        items = [_make_request(f"r{i}", created_offset=-i * 10) for i in range(10)]
        _seed_requests(requests_tbl, items)

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"limit": 3})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert len(data["items"]) <= 3

    def test_limit_capped_at_50(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"limit": 999})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert data["filters_applied"]["limit"] == 50

    def test_filter_by_status(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", status="approved", created_offset=-100),
            _make_request("r2", status="denied", created_offset=-200),
            _make_request("r3", status="approved", created_offset=-300),
        ])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"status": "denied"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert all(item["status"] == "denied" for item in data["items"])
        assert len(data["items"]) == 1

    def test_filter_by_source(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", source="bot-a", created_offset=-100),
            _make_request("r2", source="bot-b", created_offset=-200),
        ])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"source": "bot-a"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert all(item["source"] == "bot-a" for item in data["items"])

    def test_filter_by_action(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", action="upload", created_offset=-100),
            _make_request("r2", action="execute", created_offset=-200),
        ])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"action": "upload"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert all(item["action"] == "upload" for item in data["items"])
        assert len(data["items"]) == 1

    def test_filter_by_account_id(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", account_id="111111111111", created_offset=-100),
            _make_request("r2", account_id="222222222222", created_offset=-200),
        ])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"account_id": "222222222222"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert all(item["account_id"] == "222222222222" for item in data["items"])

    def test_duration_seconds_computed(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        now = int(time.time())
        item = {
            "request_id": "r1",
            "status": "approved",
            "action": "execute",
            "source": "bot",
            "account_id": "111111111111",
            "created_at": now - 3600,
            "approved_at": now - 3590,  # 10s duration
            "command": "aws s3 ls",
            "reason": "test",
        }
        _seed_requests(requests_tbl, [item])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert len(data["items"]) == 1
        assert data["items"][0]["duration_seconds"] == pytest.approx(10.0, abs=1)

    def test_duration_none_when_not_approved(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [_make_request("r1", status="pending_approval")])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert data["items"][0]["duration_seconds"] is None

    def test_source_table_annotation(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [_make_request("r1")])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert data["items"][0]["_source_table"] == "requests"

    def test_invalid_limit_type_returns_error(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        result = mh.mcp_tool_history("req-1", {"limit": "abc"})
        body = json.loads(result["body"])
        assert "error" in body

    def test_invalid_page_token_returns_error(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        result = mh.mcp_tool_history("req-1", {"page_token": "not-valid-base64!!!"})
        body = json.loads(result["body"])
        assert "error" in body

    def test_items_sorted_by_created_at_desc(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("old", created_offset=-3000),
            _make_request("new", created_offset=-100),
            _make_request("mid", created_offset=-1500),
        ])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        ids = [item["request_id"] for item in data["items"]]
        assert ids == ["new", "mid", "old"]

    def test_filters_applied_in_response(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {
                "source": "my-bot",
                "action": "execute",
                "status": "approved",
                "account_id": "111111111111",
                "since_hours": 48,
                "limit": 10,
            })

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        f = data["filters_applied"]
        assert f["source"] == "my-bot"
        assert f["action"] == "execute"
        assert f["status"] == "approved"
        assert f["account_id"] == "111111111111"
        assert f["since_hours"] == 48
        assert f["limit"] == 10


class TestBouncerHistoryCommandHistory:
    def test_execute_action_queries_command_history(self, mock_ddb):
        """When action=execute, command-history items should be included."""
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        now = int(time.time())
        cmd_item = {
            "request_id": "ch1",
            "status": "approved",
            "action": "execute",
            "source": "bot",
            "account_id": "111111111111",
            "created_at": now - 500,
            "command": "aws ec2 describe-instances",
        }
        _seed_cmd_history(cmd_tbl, [cmd_item])

        mh = _import_mcp_history(requests_tbl)
        # Patch _get_command_history_table to return our mock cmd_tbl
        with patch.object(mh, "_get_command_history_table", return_value=cmd_tbl):
            result = mh.mcp_tool_history("req-1", {"action": "execute"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        cmd_items = [i for i in data["items"] if i["_source_table"] == "command-history"]
        assert len(cmd_items) == 1
        assert cmd_items[0]["request_id"] == "ch1"

    def test_non_execute_action_skips_command_history(self, mock_ddb):
        """When action=upload, command-history should NOT be queried."""
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_cmd_history(cmd_tbl, [
            {
                "request_id": "ch1",
                "status": "approved",
                "action": "execute",
                "source": "bot",
                "created_at": int(time.time()) - 100,
            }
        ])

        mh = _import_mcp_history(requests_tbl)
        mock_get_cmd = MagicMock(return_value=cmd_tbl)
        with patch.object(mh, "_get_command_history_table", mock_get_cmd):
            result = mh.mcp_tool_history("req-1", {"action": "upload"})

        # Should not have called _get_command_history_table at all for action=upload
        mock_get_cmd.assert_not_called()

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        cmd_items = [i for i in data["items"] if i["_source_table"] == "command-history"]
        assert len(cmd_items) == 0

    def test_no_crash_when_command_history_table_missing(self, mock_ddb):
        """If command-history table doesn't exist, we gracefully return only requests."""
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [_make_request("r1")])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"action": "execute"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert len(data["items"]) == 1
        assert data["items"][0]["_source_table"] == "requests"

    def test_command_history_source_table_annotation(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        now = int(time.time())
        _seed_cmd_history(cmd_tbl, [{
            "request_id": "ch2",
            "status": "approved",
            "action": "execute",
            "source": "bot",
            "created_at": now - 200,
        }])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=cmd_tbl):
            result = mh.mcp_tool_history("req-1", {"action": "execute"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        ch_items = [i for i in data["items"] if i["_source_table"] == "command-history"]
        assert len(ch_items) == 1

    def test_merged_results_sorted_correctly(self, mock_ddb):
        """Items from both tables should be merged and sorted by created_at desc."""
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        now = int(time.time())
        _seed_requests(requests_tbl, [
            _make_request("req-old", created_offset=-3000),
        ])
        _seed_cmd_history(cmd_tbl, [{
            "request_id": "cmd-new",
            "status": "approved",
            "action": "execute",
            "source": "bot",
            "created_at": now - 100,
        }])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=cmd_tbl):
            result = mh.mcp_tool_history("req-1", {"action": "execute"})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        ids = [i["request_id"] for i in data["items"]]
        assert ids[0] == "cmd-new"
        assert ids[-1] == "req-old"


class TestBouncerHistoryPaging:
    def test_page_token_encode_decode_roundtrip(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        key = {"request_id": "abc123"}
        token = mh._encode_page_token(key)
        assert isinstance(token, str)
        decoded = mh._decode_page_token(token)
        assert decoded == key

    def test_invalid_page_token(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        result = mh._decode_page_token("not-valid!!")
        assert result is None

    def test_next_page_token_absent_when_no_more_pages(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [_make_request("r1")])

        mh = _import_mcp_history(requests_tbl)
        with patch.object(mh, "_get_command_history_table", return_value=None):
            result = mh.mcp_tool_history("req-1", {"limit": 20})

        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])
        assert data["next_page_token"] is None


# ===========================================================================
# bouncer_stats tests
# ===========================================================================


class TestBouncerStats:
    def test_empty_returns_zeros(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["total_requests"] == 0
        assert data["summary"]["approved"] == 0
        assert data["summary"]["denied"] == 0
        assert data["summary"]["pending"] == 0

    def test_counts_by_status(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", status="approved", created_offset=-100),
            _make_request("r2", status="approved", created_offset=-200),
            _make_request("r3", status="denied", created_offset=-300),
            _make_request("r4", status="pending_approval", created_offset=-400),
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["summary"]["approved"] == 2
        assert data["summary"]["denied"] == 1
        assert data["summary"]["pending"] == 1
        assert data["total_requests"] == 4

    def test_counts_by_source(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", source="bot-a", created_offset=-100),
            _make_request("r2", source="bot-a", created_offset=-200),
            _make_request("r3", source="bot-b", created_offset=-300),
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["by_source"]["bot-a"] == 2
        assert data["by_source"]["bot-b"] == 1

    def test_counts_by_action(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", action="execute", created_offset=-100),
            _make_request("r2", action="upload", created_offset=-200),
            _make_request("r3", action="execute", created_offset=-300),
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["by_action"]["execute"] == 2
        assert data["by_action"]["upload"] == 1

    def test_excludes_old_requests(self, mock_ddb):
        """Items older than 24h should not appear in stats."""
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", created_offset=-3600),    # recent
            _make_request("r2", created_offset=-90000),   # >24h old
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["total_requests"] == 1

    def test_auto_approved_counted_as_approved(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", status="auto_approved", created_offset=-100),
            _make_request("r2", status="trust_approved", created_offset=-200),
            _make_request("r3", status="grant_approved", created_offset=-300),
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["summary"]["approved"] == 3

    def test_blocked_counted_as_denied(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", status="blocked", created_offset=-100),
            _make_request("r2", status="compliance_violation", created_offset=-200),
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["summary"]["denied"] == 2

    def test_window_hours_field_present(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert data["window_hours"] == 24

    def test_by_status_breakdown(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb

        _seed_requests(requests_tbl, [
            _make_request("r1", status="approved", created_offset=-100),
            _make_request("r2", status="error", created_offset=-200),
        ])

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats("req-1", {})
        body = json.loads(result["body"])
        data = json.loads(body["result"]["content"][0]["text"])

        assert "approved" in data["by_status"]
        assert "error" in data["by_status"]


# ===========================================================================
# _compute_duration tests (unit)
# ===========================================================================


class TestComputeDuration:
    def test_normal_case(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        duration = mh._compute_duration({
            "created_at": 1000,
            "approved_at": 1015,
        })
        assert duration == pytest.approx(15.0)

    def test_uses_decided_at_fallback(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        duration = mh._compute_duration({
            "created_at": 1000,
            "decided_at": 1030,
        })
        assert duration == pytest.approx(30.0)

    def test_none_when_no_approved_at(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        duration = mh._compute_duration({"created_at": 1000})
        assert duration is None

    def test_works_with_decimal_values(self, mock_ddb):
        dynamodb, requests_tbl, cmd_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)

        duration = mh._compute_duration({
            "created_at": Decimal("1000"),
            "approved_at": Decimal("1025"),
        })
        assert duration == pytest.approx(25.0)
