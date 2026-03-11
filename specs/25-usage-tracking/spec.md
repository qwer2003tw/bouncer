# MCP Tool Usage Tracking — Sprint 25

## Overview
Record every MCP tool invocation to identify unused features and track adoption patterns across tools like `bouncer_execute`, `bouncer_upload`, `bouncer_deploy`, etc.

## User Stories
1. As a platform admin, I want to see which MCP tools are actually being used so that I can identify candidates for deprecation or focus areas for improvement.
2. As a product manager, I want usage metrics per tool and per account so that I can measure feature adoption.
3. As an engineer, I want the tracking to be fire-and-forget so that it doesn't impact request latency or fail the primary operation if tracking fails.

## Acceptance Scenarios

### Scenario 1: Tool Call Recorded
**Given** an MCP tool is invoked via `/mcp` endpoint
**When** the tool handler executes (e.g., `mcp_tool_execute`, `mcp_tool_upload`)
**Then** a record is written to DynamoDB with: `tool_name`, `timestamp`, `account_id`, `source`, `success/fail`
**And** the write happens asynchronously or fire-and-forget
**And** the primary operation is not blocked or failed if tracking write fails

### Scenario 2: Query Usage Stats
**Given** usage data has been collected for multiple tools
**When** an admin calls `bouncer stats tools` (or a new MCP tool like `bouncer_tool_stats`)
**Then** the system returns aggregated counts per tool, optionally filtered by time range or account
**And** the response includes success/failure rates

### Scenario 3: Tracking Failure Doesn't Break Tool
**Given** the DynamoDB usage tracking table is unavailable
**When** an MCP tool is invoked
**Then** the tool completes successfully
**And** a warning is logged about the tracking failure
**And** the user receives the normal tool response

## Interface Contract

### DynamoDB Schema (Option A: New Table)
**Table Name:** `bouncer-prod-mcp-usage`

**Primary Key:**
- `tool_name` (String, HASH)
- `timestamp` (Number, RANGE)

**Attributes:**
- `account_id` (String)
- `source` (String) — e.g., "claude-code", "clawdbot"
- `success` (Boolean)
- `error_type` (String, optional) — e.g., "RateLimitExceeded", "Blocked"
- `ttl` (Number) — expire records after 90 days

**GSI (optional for querying):**
- `account-time-index`: `account_id` (HASH), `timestamp` (RANGE)

### DynamoDB Schema (Option B: Extend CommandHistoryTable)
Add new partition key prefix: `USAGE#<tool_name>` with sort key `<timestamp>`. Reuse existing `ttl` field.

### New MCP Tool: `bouncer_tool_stats`
**Input:**
```json
{
  "tool_name": "bouncer_execute",  // optional filter
  "start_time": 1709856000,        // optional unix timestamp
  "end_time": 1710028800           // optional unix timestamp
}
```

**Output:**
```json
{
  "stats": [
    {
      "tool_name": "bouncer_execute",
      "total_calls": 1523,
      "success_count": 1498,
      "failure_count": 25,
      "failure_rate": 0.016
    },
    ...
  ]
}
```

## Implementation Notes

### Files to Modify
- `src/template.yaml` — Add new DynamoDB table `UsageTrackingTable` (or extend `CommandHistoryTable`)
- `src/mcp_execute.py` — Add tracking call in `mcp_tool_execute` and grant tools
- `src/mcp_upload.py` — Add tracking call in `mcp_tool_upload` and batch upload
- `src/mcp_admin.py` — Add tracking call in admin tools + implement `mcp_tool_tool_stats`
- `src/deployer.py` — Add tracking call in deploy tools
- `src/mcp_presigned.py` — Add tracking call in presigned tools
- `src/app.py` — Register `bouncer_tool_stats` in MCP tools map

### New Files
- `src/usage_tracker.py` — Module with:
  - `track_tool_usage(tool_name, account_id, source, success, error_type=None)` — Fire-and-forget DDB write
  - `get_tool_stats(tool_name=None, start_time=None, end_time=None)` — Query aggregated stats
  - `mcp_tool_tool_stats(req_id, arguments)` — MCP tool handler

### Tracking Call Pattern
```python
# At the end of each tool handler (after success/error is known):
try:
    from usage_tracker import track_tool_usage
    track_tool_usage(
        tool_name='bouncer_execute',
        account_id=arguments.get('account_id', DEFAULT_ACCOUNT_ID),
        source=arguments.get('source', 'unknown'),
        success=(not is_error),
        error_type=error_type if not success else None
    )
except Exception as e:
    logger.debug(f"[usage_tracker] failed (ignored): {e}")
```

### DynamoDB Changes
- **New table** (recommended): `bouncer-prod-mcp-usage` with on-demand billing, TTL enabled
- **IAM policy update**: Grant Lambda `dynamodb:PutItem` and `dynamodb:Query` on new table

### Security Considerations
- **No PII**: Do not track command content or file paths, only tool name and metadata
- **Rate limiting**: Fire-and-forget writes won't trigger DDB throttling alarms but should be monitored
- **Access control**: Only admins (via Telegram approval) can query `bouncer_tool_stats`
- **TTL enforcement**: Ensure old usage records auto-delete to avoid unbounded table growth

## TCS Score
| D1 Files | D2 Cross-module | D3 Testing | D4 Infrastructure | D5 External | Total |
|----------|-----------------|------------|-------------------|-------------|-------|
| 2        | 2               | 2          | 2                 | 0           | 8     |

**TCS = 8 (Simple)**

**Breakdown:**
- D1 (Files): 2 — New `usage_tracker.py` + modify 6 existing MCP modules
- D2 (Cross-module): 2 — Touches all MCP tool modules but calls are fire-and-forget
- D3 (Testing): 2 — Need unit tests for tracking function + mock DDB writes
- D4 (Infrastructure): 2 — New DynamoDB table + IAM policy update in template.yaml
- D5 (External): 0 — No external service dependencies
