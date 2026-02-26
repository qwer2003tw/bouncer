"""
Bouncer - History Query Tool

Provides `bouncer_history` MCP tool to query past approval requests
from DynamoDB with filtering and pagination support.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from db import table
from utils import mcp_result, mcp_error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HISTORY_MAX_LIMIT = 50
HISTORY_DEFAULT_LIMIT = 20
HISTORY_DEFAULT_SINCE_HOURS = 24
COMMAND_DISPLAY_MAX_LEN = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_command(text: str, max_len: int = COMMAND_DISPLAY_MAX_LEN) -> str:
    """Truncate command string to max_len characters, appending '…' if cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _ts_to_iso8601(ts) -> str:
    """Convert a Unix timestamp (int/float/Decimal) to ISO 8601 UTC string."""
    ts_float = float(ts)
    dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_item(item: dict) -> dict:
    """Format a raw DynamoDB item into a history record."""
    # Prefer display_summary over command for the display field
    raw_command = item.get("command", "")
    display_summary = item.get("display_summary", "")

    if display_summary:
        display = display_summary
    else:
        display = _truncate_command(str(raw_command))

    created_at_raw = item.get("created_at", 0)
    created_at_iso = _ts_to_iso8601(created_at_raw)

    return {
        "request_id": item.get("request_id", ""),
        "action": item.get("action", item.get("type", "")),
        "source": item.get("source", ""),
        "status": item.get("status", ""),
        "display": display,
        "command": _truncate_command(str(raw_command)) if raw_command else "",
        "created_at": created_at_iso,
        "reason": item.get("reason", ""),
        "account_id": item.get("account_id", ""),
    }


# ---------------------------------------------------------------------------
# Core query function (testable independently)
# ---------------------------------------------------------------------------

def query_history(
    limit: int = HISTORY_DEFAULT_LIMIT,
    source: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    since_hours: int = HISTORY_DEFAULT_SINCE_HOURS,
) -> dict:
    """
    Query DynamoDB for history records with optional filters.

    Returns a dict with keys:
        items  — list of formatted history records (sorted newest-first)
        count  — number of records returned
        error  — present only on validation / DynamoDB errors
    """
    # -----------------------------------------------------------------------
    # Input validation
    # -----------------------------------------------------------------------
    if limit == 0:
        return {"error": "limit must be >= 1"}

    if limit < 0:
        return {"error": "limit must be >= 1"}

    # Clamp to max
    if limit > HISTORY_MAX_LIMIT:
        limit = HISTORY_MAX_LIMIT

    if since_hours <= 0:
        return {"error": "since_hours must be >= 1"}

    # -----------------------------------------------------------------------
    # Build DynamoDB Scan with FilterExpression
    # -----------------------------------------------------------------------
    cutoff_ts = int(time.time()) - since_hours * 3600

    from boto3.dynamodb.conditions import Attr

    filter_expr = Attr("created_at").gte(cutoff_ts)

    if source:
        filter_expr = filter_expr & Attr("source").eq(source)
    if action:
        filter_expr = filter_expr & Attr("action").eq(action)
    if status:
        filter_expr = filter_expr & Attr("status").eq(status)

    # -----------------------------------------------------------------------
    # Scan (paginate to respect limit without over-fetching indefinitely)
    # -----------------------------------------------------------------------
    try:
        scan_kwargs = {
            "FilterExpression": filter_expr,
        }

        items = []
        last_key = None

        # We scan in pages; stop early once we have enough
        while True:
            if last_key:
                scan_kwargs["ExclusiveStartKey"] = last_key

            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")

            if not last_key:
                break
            # Optimisation: stop scanning once we have way more than needed
            # (we'll sort + slice afterwards)
            if len(items) >= limit * 10:
                break

    except Exception as exc:  # noqa: BLE001
        return {"error": f"DynamoDB scan failed: {exc}"}

    # -----------------------------------------------------------------------
    # Sort newest-first, slice to limit
    # -----------------------------------------------------------------------
    items.sort(key=lambda x: float(x.get("created_at", 0)), reverse=True)
    items = items[:limit]

    formatted = [_format_item(i) for i in items]

    return {
        "items": formatted,
        "count": len(formatted),
    }


# ---------------------------------------------------------------------------
# MCP tool handler
# ---------------------------------------------------------------------------

def mcp_tool_history(params: dict, req_id=None) -> dict:
    """
    MCP tool: bouncer_history

    Accepted params:
        limit        (int, optional, default 20, max 50)
        source       (str, optional)
        action       (str, optional)
        status       (str, optional)
        since_hours  (int, optional, default 24)
    """
    limit = params.get("limit", HISTORY_DEFAULT_LIMIT)
    source = params.get("source")
    action = params.get("action")
    status = params.get("status")
    since_hours = params.get("since_hours", HISTORY_DEFAULT_SINCE_HOURS)

    # Type coercion / basic guard
    try:
        limit = int(limit)
        since_hours = int(since_hours)
    except (TypeError, ValueError):
        return mcp_error(req_id, -32602, "limit and since_hours must be integers")

    result = query_history(
        limit=limit,
        source=source,
        action=action,
        status=status,
        since_hours=since_hours,
    )

    if "error" in result:
        return mcp_error(req_id, -32602, result["error"])

    return mcp_result(req_id, {
        "history": result["items"],
        "count": result["count"],
    })
