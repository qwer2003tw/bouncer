"""
tests/test_history.py
Approach C — 測試導向

Comprehensive test suite for mcp_history.py (bouncer_history MCP tool).
Covers all 15+ required edge cases.
"""

import sys
import os
import time
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from moto import mock_aws
import boto3

# ---------------------------------------------------------------------------
# Sys-path & env setup (must happen before importing src modules)
# ---------------------------------------------------------------------------

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Minimal env so constants / db don't explode
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("TABLE_NAME", "clawdbot-approval-requests")
os.environ.setdefault("ACCOUNTS_TABLE_NAME", "bouncer-accounts")
os.environ.setdefault("REQUEST_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("APPROVED_CHAT_ID", "999")
os.environ.setdefault("DEFAULT_ACCOUNT_ID", "111111111111")


# ---------------------------------------------------------------------------
# Helpers to build mock DynamoDB table
# ---------------------------------------------------------------------------

def _create_table(dynamodb):
    table = dynamodb.create_table(
        TableName="clawdbot-approval-requests",
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
    table.wait_until_exists()
    return table


def _seed(table, records):
    """Batch-write records into the mock table."""
    with table.batch_writer() as batch:
        for r in records:
            batch.put_item(Item=r)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NOW = int(time.time())


def _rec(
    req_id,
    source="BotA",
    action="execute",
    status="approved",
    created_offset=0,
    command="aws s3 ls",
    display_summary="",
    account_id="111111111111",
):
    item = {
        "request_id": req_id,
        "source": source,
        "action": action,
        "status": status,
        "created_at": Decimal(str(NOW - created_offset)),
        "command": command,
        "account_id": account_id,
        "reason": "test reason",
    }
    if display_summary:
        item["display_summary"] = display_summary
    return item


# ---------------------------------------------------------------------------
# Context manager that sets up a fresh mock table for each test
# ---------------------------------------------------------------------------

@pytest.fixture
def history_module():
    """
    Spin up a fresh moto DynamoDB context, seed some default records,
    then import (or reload) mcp_history so it picks up the mock table.
    """
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(dynamodb)
        # Also need bouncer-accounts table (imported by db.py)
        dynamodb.create_table(
            TableName="bouncer-accounts",
            KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "account_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()

        # Reload all dependent modules so they pick up the mock boto3 resource
        _clear_modules()

        import mcp_history  # noqa: PLC0415
        yield mcp_history


def _clear_modules():
    for mod in list(sys.modules.keys()):
        if mod in (
            "db", "constants", "utils", "mcp_history",
            "accounts", "trust", "grant",
        ):
            del sys.modules[mod]


# ===========================================================================
# Test Cases
# ===========================================================================


class TestHappyPath:
    """TC-01: Normal path — returns all fields."""

    def test_all_fields_present(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [_rec("req-001")])

        result = history_module.query_history(since_hours=48)
        assert result["count"] == 1
        item = result["items"][0]
        assert "request_id" in item
        assert "action" in item
        assert "source" in item
        assert "status" in item
        assert "display" in item
        assert "command" in item
        assert "created_at" in item
        assert "reason" in item
        assert "account_id" in item

    def test_returns_dict_with_items_and_count(self, history_module):
        result = history_module.query_history()
        assert isinstance(result, dict)
        assert "items" in result
        assert "count" in result


class TestLimitFilter:
    """TC-02: limit filtering."""

    def test_limit_respected(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        records = [_rec(f"req-limit-{i}", created_offset=i * 10) for i in range(10)]
        _seed(table, records)

        result = history_module.query_history(limit=3, since_hours=48)
        assert result["count"] == 3
        assert len(result["items"]) == 3

    def test_limit_default_is_20(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        records = [_rec(f"req-def-{i}", created_offset=i * 5) for i in range(25)]
        _seed(table, records)

        result = history_module.query_history(since_hours=48)
        assert result["count"] == 20


class TestSourceFilter:
    """TC-03: source filtering."""

    def test_source_filter(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec("req-src-a1", source="AgentA"),
            _rec("req-src-a2", source="AgentA"),
            _rec("req-src-b1", source="AgentB"),
        ])
        result = history_module.query_history(source="AgentA", since_hours=48)
        assert result["count"] == 2
        for item in result["items"]:
            assert item["source"] == "AgentA"


class TestActionFilter:
    """TC-04: action filtering."""

    def test_action_filter(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec("req-act-e1", action="execute"),
            _rec("req-act-e2", action="execute"),
            _rec("req-act-u1", action="upload"),
        ])
        result = history_module.query_history(action="upload", since_hours=48)
        assert result["count"] == 1
        assert result["items"][0]["action"] == "upload"


class TestStatusFilter:
    """TC-05: status filtering."""

    def test_status_filter(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec("req-st-ap1", status="approved"),
            _rec("req-st-ap2", status="approved"),
            _rec("req-st-dn1", status="denied"),
        ])
        result = history_module.query_history(status="denied", since_hours=48)
        assert result["count"] == 1
        assert result["items"][0]["status"] == "denied"


class TestSinceHours:
    """TC-06: since_hours time range filtering."""

    def test_since_hours_excludes_old_records(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec("req-new-1", created_offset=3600),        # 1 hour ago — within 2h window
            _rec("req-old-1", created_offset=10 * 3600),   # 10 hours ago — outside 2h window
        ])
        result = history_module.query_history(since_hours=2)
        assert result["count"] == 1
        assert result["items"][0]["request_id"] == "req-new-1"

    def test_since_hours_includes_all_within_window(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec("req-h-1", created_offset=3600),
            _rec("req-h-2", created_offset=7200),
            _rec("req-h-3", created_offset=25 * 3600),  # outside 24h
        ])
        result = history_module.query_history(since_hours=24)
        ids = {i["request_id"] for i in result["items"]}
        assert "req-h-1" in ids
        assert "req-h-2" in ids
        assert "req-h-3" not in ids


class TestLimitEdgeCases:
    """TC-07 & TC-08: limit boundary conditions."""

    def test_limit_over_max_clamped_to_50(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        records = [_rec(f"req-max-{i}", created_offset=i * 5) for i in range(60)]
        _seed(table, records)

        result = history_module.query_history(limit=999, since_hours=48)
        # Should be clamped to 50
        assert result["count"] <= 50

    def test_limit_zero_returns_error(self, history_module):
        result = history_module.query_history(limit=0)
        assert "error" in result
        assert "limit" in result["error"].lower()

    def test_limit_negative_returns_error(self, history_module):
        result = history_module.query_history(limit=-5)
        assert "error" in result


class TestEmptyResults:
    """TC-09: empty result when no records match."""

    def test_no_matching_records(self, history_module):
        # Table is empty (fresh fixture)
        result = history_module.query_history(source="nonexistent-source", since_hours=1)
        assert result["count"] == 0
        assert result["items"] == []

    def test_empty_table(self, history_module):
        result = history_module.query_history()
        assert result["count"] == 0
        assert isinstance(result["items"], list)


class TestCombinedFilters:
    """TC-10: combined filters (source + action + status)."""

    def test_combined_filters(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec("req-combo-1", source="BotX", action="execute", status="approved"),
            _rec("req-combo-2", source="BotX", action="execute", status="denied"),
            _rec("req-combo-3", source="BotY", action="execute", status="approved"),
            _rec("req-combo-4", source="BotX", action="upload", status="approved"),
        ])
        result = history_module.query_history(
            source="BotX", action="execute", status="approved", since_hours=48
        )
        assert result["count"] == 1
        assert result["items"][0]["request_id"] == "req-combo-1"


class TestDynamoDBErrorHandling:
    """TC-11: DynamoDB scan error handling."""

    def test_dynamodb_error_returns_error_dict(self, history_module):
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Rate exceeded"}
        }
        mock_exc = ClientError(error_response, "Scan")

        with patch.object(history_module.table, "scan", side_effect=mock_exc):
            result = history_module.query_history()
        assert "error" in result
        assert "DynamoDB scan failed" in result["error"]

    def test_generic_exception_handled(self, history_module):
        with patch.object(history_module.table, "scan", side_effect=RuntimeError("boom")):
            result = history_module.query_history()
        assert "error" in result


class TestTimestampFormat:
    """TC-12: returned timestamps are ISO 8601."""

    def test_created_at_is_iso8601(self, history_module):
        import re

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [_rec("req-ts-001")])
        result = history_module.query_history(since_hours=48)
        assert result["count"] == 1
        ts = result["items"][0]["created_at"]
        # Expect format: 2026-02-26T01:43:00Z
        iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        assert iso_re.match(ts), f"Unexpected timestamp format: {ts}"

    def test_created_at_utc_zone(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [_rec("req-ts-002")])
        result = history_module.query_history(since_hours=48)
        ts = result["items"][0]["created_at"]
        assert ts.endswith("Z"), "Timestamp must end with Z (UTC)"


class TestCommandTruncation:
    """TC-13: oversized command strings are truncated to 200 chars."""

    def test_long_command_truncated(self, history_module):
        long_cmd = "aws s3 cp " + "x" * 300  # 310 chars
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [_rec("req-trunc-001", command=long_cmd)])
        result = history_module.query_history(since_hours=48)
        assert result["count"] == 1
        item = result["items"][0]
        # command field should be truncated (200 chars + ellipsis = 201)
        assert len(item["command"]) <= 201
        assert item["command"].endswith("…")

    def test_short_command_not_truncated(self, history_module):
        short_cmd = "aws s3 ls"
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [_rec("req-short-001", command=short_cmd)])
        result = history_module.query_history(since_hours=48)
        assert result["items"][0]["command"] == short_cmd

    def test_truncate_helper_exact_boundary(self, history_module):
        exactly_200 = "a" * 200
        result = history_module._truncate_command(exactly_200)
        assert result == exactly_200  # no truncation

    def test_truncate_helper_over_boundary(self, history_module):
        over = "a" * 201
        result = history_module._truncate_command(over)
        assert len(result) == 201  # 200 + ellipsis char
        assert result.endswith("…")


class TestDisplaySummaryPriority:
    """TC-14: display_summary takes priority over command for display field."""

    def test_display_summary_used_when_present(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [
            _rec(
                "req-ds-001",
                command="aws s3 ls s3://my-bucket",
                display_summary="List bucket contents",
            )
        ])
        result = history_module.query_history(since_hours=48)
        assert result["count"] == 1
        item = result["items"][0]
        assert item["display"] == "List bucket contents"
        # command field still has the actual command
        assert item["command"] == "aws s3 ls s3://my-bucket"

    def test_command_used_when_no_display_summary(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        _seed(table, [_rec("req-ds-002", command="aws ec2 describe-instances")])
        result = history_module.query_history(since_hours=48)
        item = result["items"][0]
        assert item["display"] == "aws ec2 describe-instances"

    def test_empty_display_summary_falls_back_to_command(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        # Explicitly set display_summary to empty string
        rec = _rec("req-ds-003", command="aws iam list-users")
        rec["display_summary"] = ""
        _seed(table, [rec])
        result = history_module.query_history(since_hours=48)
        item = result["items"][0]
        assert item["display"] == "aws iam list-users"


class TestSortOrder:
    """TC-15: records sorted newest-first by created_at."""

    def test_newest_first_ordering(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        records = [
            _rec("req-sort-oldest", created_offset=7200),   # 2h ago
            _rec("req-sort-middle", created_offset=3600),   # 1h ago
            _rec("req-sort-newest", created_offset=600),    # 10min ago
        ]
        _seed(table, records)
        result = history_module.query_history(since_hours=48)
        ids = [i["request_id"] for i in result["items"]]
        assert ids[0] == "req-sort-newest"
        assert ids[1] == "req-sort-middle"
        assert ids[2] == "req-sort-oldest"

    def test_sort_stable_with_many_records(self, history_module):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "clawdbot-approval-requests"
        )
        records = [_rec(f"req-many-{i}", created_offset=i * 100) for i in range(15)]
        _seed(table, records)
        result = history_module.query_history(since_hours=48)
        timestamps = [r["created_at"] for r in result["items"]]
        assert timestamps == sorted(timestamps, reverse=True)


# ===========================================================================
# MCP tool wrapper tests
# ===========================================================================


class TestMcpToolWrapper:
    """Tests for the MCP JSON-RPC wrapper (mcp_tool_history)."""

    def test_mcp_tool_success(self, history_module):
        result = history_module.mcp_tool_history({}, req_id="test-1")
        body = result.get("body", "{}")
        import json
        parsed = json.loads(body) if isinstance(body, str) else body
        assert parsed.get("id") == "test-1"
        assert "result" in parsed
        assert "history" in parsed["result"]
        assert "count" in parsed["result"]

    def test_mcp_tool_limit_zero_returns_error(self, history_module):
        result = history_module.mcp_tool_history({"limit": 0}, req_id="test-2")
        import json
        body = result.get("body", "{}")
        parsed = json.loads(body) if isinstance(body, str) else body
        assert "error" in parsed

    def test_mcp_tool_invalid_limit_type(self, history_module):
        result = history_module.mcp_tool_history({"limit": "banana"}, req_id="test-3")
        import json
        body = result.get("body", "{}")
        parsed = json.loads(body) if isinstance(body, str) else body
        assert "error" in parsed

    def test_mcp_tool_over_limit_clamped(self, history_module):
        result = history_module.mcp_tool_history({"limit": 100}, req_id="test-4")
        import json
        body = result.get("body", "{}")
        parsed = json.loads(body) if isinstance(body, str) else body
        # Should succeed (no error) — limit clamped to 50
        assert "result" in parsed
