# Logging: [TAG] Prefix → Structured Extra Fields 收尾

## Feature
將 `src/` 下所有仍使用 `[TAG]` prefix 或純 f-string 格式的 `logger.*` 呼叫，收尾為帶 `extra={}` 的 structured logging 格式，符合 AWS Powertools Logger 標準。

## User Stories
- As a Bouncer developer, I want all log entries to use structured `extra={}` fields instead of `[TAG]` prefix strings, so that I can filter and query logs in CloudWatch Logs Insights using field-based queries.
- As a DevOps engineer, I want consistent log format across all modules, so that I can build reliable dashboards and alerts.

## Acceptance Scenarios

### Scenario 1: [TAG] prefix removed, structured extra added
Given a logger call: `logger.error(f"[CLEANUP] DynamoDB error for {request_id}: {e}")`
When the change is applied
Then it becomes: `logger.error("DynamoDB error for request", extra={"module": "cleanup", "operation": "expiry_cleanup", "request_id": request_id, "error": str(e)})`

### Scenario 2: Plain string logger without [TAG] — also converted
Given a logger call: `logger.error(f"get_paged_output error: {e}")`
When the change is applied
Then it becomes: `logger.error("get_paged_output error", extra={"module": "paging", "operation": "get_paged_output", "error": str(e)})`

### Scenario 3: Powertools Logger still outputs structured JSON
Given the Logger is `Logger(service="bouncer")`
When a `logger.info(...)` with `extra=` is called
Then CloudWatch receives a JSON log entry with `service`, `module`, `operation` fields queryable by Logs Insights

### Scenario 4: Exception loggers use exc_info or logger.exception
Given a logger call using `logger.exception(...)`
When converted
Then it remains as `logger.exception("...", extra={...})` (exc_info is included automatically)

## Edge Cases
- Some calls are multi-line or use `%s` format style — must handle both f-string and %-style
- `logger.exception` already includes traceback — don't add `exc_info=True` again
- Calls inside `except` blocks: extract exception info into `extra={'error': str(e)}`
- Some [TAG] values map to existing `module` extra keys — must be consistent
- 176 calls is the total; priority order: P0 modules first (app.py, mcp_execute.py, callbacks.py), then others

## Requirements

### Functional
- Remove `[TAG]` prefix from log message strings
- Add `extra={"module": "<module_slug>", "operation": "<func_or_action>", ...relevant_fields}` to all converted calls
- `module` value conventions: "cleanup", "trust", "execute", "deploy", "history", "risk_scorer", "scheduler", "presigned", "callbacks", "notifications", etc.
- `operation` value: snake_case function or action name
- Keep log message itself brief and human-readable (remove redundant ID interpolation from message; put IDs in extra)
- Do NOT change logger calls that already have `extra=` (43 calls already done)

### Non-functional
- No functional behavior change (logging is observability-only)
- No new dependencies
- Batch the changes per file to minimize diff noise
- Aim to reduce total without-extra count from 176 to <10 (some may be acceptable one-liners)

## Interface Contract
- CloudWatch log schema: new fields `module`, `operation` queryable in Logs Insights
- No DDB or API changes
