"""
Bouncer - History Query MCP Tool (Approach A — Conservative)

查詢 bouncer-prod-requests (TABLE_NAME) 的歷史操作記錄。
使用 DynamoDB Scan + FilterExpression，簡單過濾，無額外 GSI 依賴。
"""

import json
import time
from typing import Optional

from db import table
from utils import mcp_result, mcp_error, decimal_to_native


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 50
HISTORY_DEFAULT_SINCE_HOURS = 24


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _iso_ts(epoch: int) -> Optional[str]:
    """Convert Unix epoch int to ISO-8601 UTC string, or None if falsy."""
    if not epoch:
        return None
    try:
        import datetime
        return datetime.datetime.utcfromtimestamp(int(epoch)).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return str(epoch)


def _format_item(item: dict) -> dict:
    """Flatten a DynamoDB item into the public history record shape."""
    raw = decimal_to_native(item)
    return {
        'request_id': raw.get('request_id', ''),
        'action': raw.get('action', raw.get('decision_type', 'execute')),
        'command': raw.get('command', raw.get('display_summary', '')),
        'status': raw.get('status', ''),
        'source': raw.get('source', ''),
        'created_at': _iso_ts(raw.get('created_at')),
        'approved_at': _iso_ts(raw.get('approved_at') or raw.get('decided_at')),
    }


# ---------------------------------------------------------------------------
# Core query logic (separated for testability)
# ---------------------------------------------------------------------------

def _query_history(
    limit: int = HISTORY_DEFAULT_LIMIT,
    source: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    since_hours: int = HISTORY_DEFAULT_SINCE_HOURS,
) -> list:
    """
    Scan bouncer-prod-requests table with optional filters.

    Returns a list of raw DynamoDB items (already decimal-converted).
    """
    limit = max(1, min(limit, HISTORY_MAX_LIMIT))
    since_hours = max(1, since_hours)
    cutoff = int(time.time()) - since_hours * 3600

    # Build FilterExpression dynamically
    filter_parts = ['created_at > :cutoff']
    expr_values = {':cutoff': cutoff}
    expr_names = {}

    if source:
        filter_parts.append('#src = :source')
        expr_names['#src'] = 'source'
        expr_values[':source'] = source

    # action maps to the item's 'action' attribute OR 'decision_type'
    if action:
        filter_parts.append('(#act = :action OR decision_type = :action)')
        expr_names['#act'] = 'action'
        expr_values[':action'] = action

    if status:
        filter_parts.append('#st = :status')
        expr_names['#st'] = 'status'
        expr_values[':status'] = status

    scan_kwargs = {
        'FilterExpression': ' AND '.join(filter_parts),
        'ExpressionAttributeValues': expr_values,
    }
    if expr_names:
        scan_kwargs['ExpressionAttributeNames'] = expr_names

    # Paginate until we have `limit` matching items or table exhausted
    items = []
    last_key = None

    while len(items) < limit:
        if last_key:
            scan_kwargs['ExclusiveStartKey'] = last_key

        resp = table.scan(**scan_kwargs)
        batch = resp.get('Items', [])
        items.extend(batch)

        last_key = resp.get('LastEvaluatedKey')
        if not last_key:
            break

    # Sort by created_at descending (newest first), then truncate to limit
    items.sort(key=lambda x: int(x.get('created_at', 0)), reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------
# MCP Tool Handler
# ---------------------------------------------------------------------------

def mcp_tool_history(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_history — 查詢操作歷史記錄"""
    try:
        raw_limit = arguments.get('limit', HISTORY_DEFAULT_LIMIT)
        limit = int(raw_limit) if raw_limit is not None else HISTORY_DEFAULT_LIMIT
        limit = max(1, min(limit, HISTORY_MAX_LIMIT))
    except (TypeError, ValueError):
        return mcp_error(req_id, -32602, 'Invalid parameter: limit must be an integer')

    try:
        raw_since = arguments.get('since_hours', HISTORY_DEFAULT_SINCE_HOURS)
        since_hours = int(raw_since) if raw_since is not None else HISTORY_DEFAULT_SINCE_HOURS
        since_hours = max(1, since_hours)
    except (TypeError, ValueError):
        return mcp_error(req_id, -32602, 'Invalid parameter: since_hours must be an integer')

    source = arguments.get('source') or None
    action = arguments.get('action') or None
    status = arguments.get('status') or None

    try:
        raw_items = _query_history(
            limit=limit,
            source=source,
            action=action,
            status=status,
            since_hours=since_hours,
        )
    except Exception as e:
        return mcp_error(req_id, -32603, f'Internal error: {str(e)}')

    formatted = [_format_item(item) for item in raw_items]

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'items': formatted,
                'total': len(formatted),
                'limit': limit,
            }, ensure_ascii=False, indent=2)
        }]
    })
