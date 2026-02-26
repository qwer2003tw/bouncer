"""
Bouncer - MCP History & Stats Tools

bouncer_history: 整合查詢 requests + command-history，支援分頁和豐富 filter
bouncer_stats:   最近 24h 的統計資訊
"""

import base64
import json
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

from constants import TABLE_NAME
from db import table
from utils import decimal_to_native, mcp_error, mcp_result

# ============================================================================
# Constants
# ============================================================================

HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 50
HISTORY_DEFAULT_SINCE_HOURS = 24

COMMAND_HISTORY_TABLE_NAME = "bouncer-prod-command-history"
REQUESTS_TABLE_NAME = TABLE_NAME

# ============================================================================
# Helpers
# ============================================================================


def _get_dynamodb_resource():
    """Return boto3 DynamoDB resource (injectable for tests)."""
    return boto3.resource("dynamodb")


def _get_command_history_table(dynamodb=None):
    """Return command-history table if it exists, else None."""
    if dynamodb is None:
        dynamodb = _get_dynamodb_resource()
    try:
        tbl = dynamodb.Table(COMMAND_HISTORY_TABLE_NAME)
        tbl.load()  # raises if the table doesn't exist
        return tbl
    except Exception:
        return None


def _encode_page_token(last_evaluated_key: dict) -> str:
    """Encode DynamoDB LastEvaluatedKey to a page token string."""
    raw = json.dumps(decimal_to_native(last_evaluated_key), separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_page_token(token: str) -> dict | None:
    """Decode page token back to DynamoDB ExclusiveStartKey. Returns None on error."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        data = json.loads(raw)
        return data
    except Exception:
        return None


def _compute_duration(item: dict) -> float | None:
    """Compute duration_seconds = approved_at - created_at."""
    try:
        approved_at = item.get("approved_at") or item.get("decided_at")
        created_at = item.get("created_at")
        if approved_at and created_at:
            return round(float(Decimal(str(approved_at))) - float(Decimal(str(created_at))), 3)
    except Exception:
        pass
    return None


def _format_item(item: dict, source_table: str = "requests") -> dict:
    """Normalise a DynamoDB item for history output."""
    native = decimal_to_native(item)
    native["_source_table"] = source_table

    duration = _compute_duration(item)
    native["duration_seconds"] = duration

    # Redact large fields that aren't useful in list view
    for field in ("content", "files"):
        if field in native:
            native[field] = "<redacted>"

    return native


def _build_filter_expression(
    source: str | None,
    action: str | None,
    status: str | None,
    account_id: str | None,
    since_ts: int,
):
    """Build boto3 Attr filter expression for scan."""
    expr = Attr("created_at").gte(since_ts)

    if source:
        expr = expr & Attr("source").eq(source)

    if action:
        expr = expr & Attr("action").eq(action)

    if status:
        expr = expr & Attr("status").eq(status)

    if account_id:
        expr = expr & Attr("account_id").eq(account_id)

    return expr


# ============================================================================
# bouncer_history
# ============================================================================


def _query_requests_table(
    limit: int,
    source: str | None,
    action: str | None,
    status: str | None,
    account_id: str | None,
    since_ts: int,
    exclusive_start_key: dict | None,
) -> tuple[list[dict], int, dict | None]:
    """Scan requests table with filters. Returns (items, scanned_count, last_key)."""
    filter_expr = _build_filter_expression(source, action, status, account_id, since_ts)

    kwargs: dict = {
        "FilterExpression": filter_expr,
        "Limit": limit * 5,  # over-scan to get enough filtered results
    }
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    all_items: list[dict] = []
    total_scanned = 0
    last_key = None

    try:
        resp = table.scan(**kwargs)
        total_scanned += resp.get("ScannedCount", 0)
        all_items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
    except Exception as e:
        print(f"[history] scan requests error: {e}")
        return [], 0, None

    # Trim to desired limit
    items = all_items[:limit]
    # If we got exactly limit items and there's a last_key, pass it along
    if len(all_items) < limit:
        last_key = None  # No more pages

    return items, total_scanned, last_key


def _query_command_history_table(
    limit: int,
    source: str | None,
    account_id: str | None,
    since_ts: int,
    exclusive_start_key: dict | None,
) -> tuple[list[dict], int]:
    """Scan command-history table. Returns (items, scanned_count)."""
    dynamodb = _get_dynamodb_resource()
    cmd_table = _get_command_history_table(dynamodb)
    if cmd_table is None:
        return [], 0

    expr = Attr("created_at").gte(since_ts)
    if source:
        expr = expr & Attr("source").eq(source)
    if account_id:
        expr = expr & Attr("account_id").eq(account_id)

    kwargs: dict = {
        "FilterExpression": expr,
        "Limit": limit * 5,
    }
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    try:
        resp = cmd_table.scan(**kwargs)
        items = resp.get("Items", [])[:limit]
        scanned = resp.get("ScannedCount", 0)
        return items, scanned
    except Exception as e:
        print(f"[history] scan command-history error: {e}")
        return [], 0


def mcp_tool_history(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_history

    Queries the requests table (and optionally command-history) and returns
    paginated history with rich filtering.
    """
    # --- Parse & validate arguments ---
    try:
        raw_limit = int(arguments.get("limit", HISTORY_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return mcp_error(req_id, -32602, "limit must be an integer")

    limit = max(1, min(raw_limit, HISTORY_MAX_LIMIT))

    source = arguments.get("source") or None
    action = arguments.get("action") or None
    status = arguments.get("status") or None
    account_id = arguments.get("account_id") or None

    try:
        since_hours = int(arguments.get("since_hours", HISTORY_DEFAULT_SINCE_HOURS))
    except (TypeError, ValueError):
        since_hours = HISTORY_DEFAULT_SINCE_HOURS

    since_ts = int(time.time()) - since_hours * 3600

    page_token = arguments.get("page_token") or None
    exclusive_start_key = None
    if page_token:
        exclusive_start_key = _decode_page_token(page_token)
        if exclusive_start_key is None:
            return mcp_error(req_id, -32602, "Invalid page_token")

    # --- Query main requests table ---
    req_items, req_scanned, last_key = _query_requests_table(
        limit=limit,
        source=source,
        action=action,
        status=status,
        account_id=account_id,
        since_ts=since_ts,
        exclusive_start_key=exclusive_start_key,
    )
    formatted_items = [_format_item(i, "requests") for i in req_items]
    total_scanned = req_scanned

    # --- If action==execute, also query command-history ---
    cmd_items: list[dict] = []
    cmd_scanned = 0
    if action == "execute" or action is None:
        cmd_items, cmd_scanned = _query_command_history_table(
            limit=limit,
            source=source,
            account_id=account_id,
            since_ts=since_ts,
            exclusive_start_key=None,  # separate table, independent pagination
        )
        total_scanned += cmd_scanned
        for item in cmd_items:
            formatted_items.append(_format_item(item, "command-history"))

    # Sort combined results by created_at desc
    formatted_items.sort(
        key=lambda x: x.get("created_at") or 0,
        reverse=True,
    )
    # Trim to limit after merge
    formatted_items = formatted_items[:limit]

    # --- Build next_page_token ---
    next_page_token = None
    if last_key:
        next_page_token = _encode_page_token(last_key)

    result = {
        "items": formatted_items,
        "total_scanned": total_scanned,
        "next_page_token": next_page_token,
        "filters_applied": {
            "limit": limit,
            "since_hours": since_hours,
            "source": source,
            "action": action,
            "status": status,
            "account_id": account_id,
        },
    }

    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
    })


# ============================================================================
# bouncer_stats
# ============================================================================


def mcp_tool_stats(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_stats

    Returns summary statistics for the last 24 hours:
    - Count by status (approved, denied, pending, error, …)
    - Count by source
    """
    since_ts = int(time.time()) - 24 * 3600

    try:
        resp = table.scan(
            FilterExpression=Attr("created_at").gte(since_ts),
        )
        items = resp.get("Items", [])
        scanned = resp.get("ScannedCount", 0)

        # Handle pagination for stats (full scan)
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                FilterExpression=Attr("created_at").gte(since_ts),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
            scanned += resp.get("ScannedCount", 0)

    except Exception as e:
        return mcp_error(req_id, -32603, f"Internal error: {e}")

    # Tally by status
    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}

    for item in items:
        status_val = str(item.get("status", "unknown"))
        source_val = str(item.get("source", "__anonymous__"))
        action_val = str(item.get("action", "execute"))

        status_counts[status_val] = status_counts.get(status_val, 0) + 1
        source_counts[source_val] = source_counts.get(source_val, 0) + 1
        action_counts[action_val] = action_counts.get(action_val, 0) + 1

    # Friendly top-level numbers
    approved_count = sum(
        v for k, v in status_counts.items()
        if k in ("approved", "auto_approved", "trust_approved", "grant_approved")
    )
    denied_count = sum(
        v for k, v in status_counts.items()
        if k in ("denied", "blocked", "compliance_violation")
    )
    pending_count = sum(
        v for k, v in status_counts.items()
        if k in ("pending", "pending_approval")
    )

    result = {
        "window_hours": 24,
        "total_requests": len(items),
        "total_scanned": scanned,
        "summary": {
            "approved": approved_count,
            "denied": denied_count,
            "pending": pending_count,
        },
        "by_status": status_counts,
        "by_source": source_counts,
        "by_action": action_counts,
    }

    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
    })
