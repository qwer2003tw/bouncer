# Logging Modernization — Sprint 25

## Overview
Migrate from stdlib `logging` to `aws-lambda-powertools` Logger for structured JSON logging with CloudWatch Insights compatibility. Replace `[TAG]` string prefixes with structured `extra` fields like `{"subsystem": "GRANT"}`.

## User Stories
1. As an SRE, I want structured JSON logs so that I can query CloudWatch Logs Insights by subsystem, event type, and custom fields without parsing string messages.
2. As a developer, I want consistent logger initialization so that all modules emit logs in the same structured format.
3. As a security auditor, I want correlation IDs automatically added to logs so that I can trace a request across multiple function invocations.

## Acceptance Scenarios

### Scenario 1: Structured Logging with Subsystem
**Given** the logging migration is complete
**When** `grant.py` logs an error with `[GRANT]` prefix
**Then** the CloudWatch log event contains JSON like:
```json
{
  "level": "ERROR",
  "message": "match_pattern error for pattern='aws s3 *'",
  "subsystem": "GRANT",
  "timestamp": "2026-03-10T17:30:00.123Z",
  "function_request_id": "abc-123"
}
```

### Scenario 2: No Markdown Escape Issues
**Given** a log message contains special characters like `*`, `_`, `[`, `]`
**When** the message is logged
**Then** the message appears in CloudWatch without broken Markdown escaping
**And** the message is stored as plain text in the JSON `message` field

### Scenario 3: Backward Compatibility with Tests
**Given** existing tests mock `logging.getLogger()`
**When** the logging migration is complete
**Then** all existing tests pass without modification (or require minimal mock updates)

### Scenario 4: metrics.py Unchanged
**Given** `metrics.py` uses `print(json.dumps(...))` for EMF metrics
**When** the logging migration is complete
**Then** `metrics.py` continues to use stdout for EMF
**And** EMF metrics are not affected by the Logger change

## Interface Contract

### Logger Initialization (Before)
```python
import logging
logger = logging.getLogger(__name__)
```

### Logger Initialization (After)
```python
from aws_lambda_powertools import Logger
logger = Logger(service="bouncer")
```

### Structured Logging (Before)
```python
logger.error(f"[GRANT] match_pattern error for pattern={pattern!r}: {e}")
```

### Structured Logging (After)
```python
logger.error("match_pattern error", subsystem="GRANT", pattern=pattern, error=str(e))
```

## Implementation Notes

### Files to Modify
- `src/requirements.txt` — Add `aws-lambda-powertools>=2.30.0`
- **All Python modules in `src/`** that use `logging.getLogger()`:
  - `app.py`
  - `mcp_execute.py`
  - `mcp_confirm.py`
  - `grant.py`
  - `deployer.py`
  - `telegram.py`
  - `metrics.py` — **DO NOT CHANGE** (it uses print for EMF, not logger)
  - `risk_scorer.py`
  - ~15-20 other modules (run grep to find all)

### Migration Pattern

**Step 1:** Replace logger initialization
```python
# OLD
import logging
logger = logging.getLogger(__name__)

# NEW
from aws_lambda_powertools import Logger
logger = Logger(service="bouncer", child=True)  # child=True for module-level loggers
```

**Step 2:** Convert [TAG] prefixes to structured fields
```python
# OLD
logger.error(f"[GRANT] match_pattern error for pattern={pattern!r}: {e}")

# NEW
logger.error("match_pattern error", subsystem="GRANT", pattern=pattern, error=str(e))
```

**Step 3:** Convert f-strings with dynamic data to keyword args
```python
# OLD
logger.info(f"[deployer] Created deploy record {deploy_id}")

# NEW
logger.info("Created deploy record", subsystem="deployer", deploy_id=deploy_id)
```

**Step 4:** Replace logger.debug with timing context where appropriate
```python
# OLD
logger.debug(f"[TIMING] Telegram {method}: {elapsed:.0f}ms")

# NEW
logger.debug("Telegram API call", subsystem="TIMING", method=method, elapsed_ms=elapsed)
```

### New Files
- None (in-place migration)

### DynamoDB Changes
- None

### Security Considerations
- **No secrets in logs**: Ensure structured logging doesn't accidentally expose secrets via keyword args (e.g., don't log `token=TELEGRAM_TOKEN`)
- **Sanitize exceptions**: When logging exceptions, use `error=str(e)` not `error=e` to avoid leaking stack traces with sensitive paths
- **Log level review**: Keep existing log levels (INFO/DEBUG/ERROR) unchanged to avoid creating new CloudWatch costs

### Testing Strategy
- **Unit tests**: Update mocks from `logging.getLogger()` to `Logger` (may need `monkeypatch` in pytest)
- **Integration tests**: Verify CloudWatch logs contain expected JSON structure (check in AWS Console after deploy)
- **Backward compatibility**: Ensure `metrics.py` EMF output is unchanged (run existing metrics tests)

### Rollout Plan
1. **Phase 1**: Add aws-lambda-powertools to requirements.txt
2. **Phase 2**: Migrate 3-5 modules as pilot (e.g., grant.py, deployer.py, telegram.py)
3. **Phase 3**: Run tests, verify CloudWatch logs format
4. **Phase 4**: Migrate remaining modules
5. **Phase 5**: Remove all `import logging` statements (except metrics.py)

## TCS Score
| D1 Files | D2 Cross-module | D3 Testing | D4 Infrastructure | D5 External | Total |
|----------|-----------------|------------|-------------------|-------------|-------|
| 3        | 1               | 2          | 1                 | 0           | 7     |

**TCS = 7 (Simple)**

**Breakdown:**
- D1 (Files): 3 — Modify ~20 Python modules (mechanical find-replace pattern)
- D2 (Cross-module): 1 — All changes are local (logger initialization per module)
- D3 (Testing): 2 — Update logger mocks in tests, verify EMF unchanged
- D4 (Infrastructure): 1 — Add dependency to requirements.txt (no infra change)
- D5 (External): 0 — aws-lambda-powertools is a library, no external service
