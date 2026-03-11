# Powertools Logger Migration - Batch 3

## Summary
Migrate remaining stdlib `logging` modules to AWS Lambda Powertools Logger for consistent structured logging and CloudWatch integration.

## Background / Motivation
Previous batches have migrated core modules to Powertools Logger. Batch 3 completes the migration for remaining modules, eliminating stdlib logging inconsistencies. This ensures:
- **Uniform structured logging** across the entire codebase (JSON format for CloudWatch Insights queries)
- **Consistent log levels** and correlation IDs
- **Better debugging** with automatic Lambda context injection

**Architecture Rationale:** Powertools Logger provides Lambda-native features (cold start tracking, request IDs, X-Ray integration) that stdlib logging lacks. Completing migration removes dual logging systems and simplifies operational troubleshooting.

## User Stories
- **US1:** As an operator, when I query CloudWatch Insights for error logs, then I see consistent JSON-structured logs across all modules (no mixed plaintext logs).
- **US2:** As a developer, when I add logging to a new module, then I follow the established Powertools pattern (no confusion about which logger to use).
- **US3:** As an SRE, when I trace a request across modules, then all logs share the same correlation ID and service name.

## Acceptance Scenarios

### Scenario 1: High-Frequency Module Migration (telegram.py)
- **Given:** `telegram.py` uses stdlib logging with ~50 log calls
- **When:** Module is migrated to Powertools Logger
- **Then:** All log calls use `logger.info()`, `logger.warning()`, `logger.error()`
- **And:** Log output is JSON-structured with `service: bouncer`
- **And:** No `exc_info=True` calls remain (replaced with `logger.exception()`)

### Scenario 2: Utils Module Migration (utils.py)
- **Given:** `utils.py` uses stdlib logging with utility function logs
- **When:** Module is migrated to Powertools Logger
- **Then:** Logger is initialized at module level: `logger = Logger(service="bouncer")`
- **And:** All existing log levels preserved (DEBUG → debug, INFO → info, etc.)

### Scenario 3: No Regressions in Existing Modules
- **Given:** Previous batches migrated `mcp_execute.py`, `callbacks.py`, etc.
- **When:** Batch 3 modules are migrated
- **Then:** No changes to previously migrated modules
- **And:** All modules use consistent `Logger(service="bouncer")` initialization

### Scenario 4: Exception Logging Compatibility
- **Given:** Code uses `logger.error("msg", exc_info=True)` pattern
- **When:** Migrated to Powertools Logger
- **Then:** Replaced with `logger.exception("msg")` (auto-captures exception context)
- **And:** Stack trace appears in CloudWatch logs

## Technical Design

### Files to Change
| File | Change |
|------|--------|
| `src/telegram.py:16` | Replace stdlib logging with Powertools Logger (high-frequency module) |
| `src/utils.py:16` | Replace stdlib logging with Powertools Logger (high-frequency module) |
| `src/accounts.py:17` | Replace stdlib logging with Powertools Logger (medium-frequency module) |
| `src/notifications.py:27` | Replace stdlib logging with Powertools Logger (high-frequency module) |
| `src/mcp_deploy_frontend.py:29` | Replace stdlib logging with Powertools Logger (medium-frequency module) |
| `src/mcp_history.py:23` | Replace stdlib logging with Powertools Logger (medium-frequency module) |
| `src/metrics.py` | Replace stdlib logging with Powertools Logger (if exists) |
| `src/sequence_analyzer.py` | Replace stdlib logging with Powertools Logger (if exists) |
| `src/scheduler_service.py` | Replace stdlib logging with Powertools Logger (if exists) |
| `src/paging.py` | Replace stdlib logging with Powertools Logger (if exists) |
| `src/telegram_commands.py` | Replace stdlib logging with Powertools Logger (if exists) |
| `src/mcp_presigned.py` | Replace stdlib logging with Powertools Logger (if exists) |
| `src/mcp_upload.py` | Replace stdlib logging with Powertools Logger (if exists) |

### Key Implementation Notes

#### Standard Migration Pattern (for all files)
**Before:**
```python
import logging

logger = logging.getLogger(__name__)
```

**After:**
```python
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")
```

**Note:** Remove `import logging` entirely unless used for non-logger purposes (e.g., `logging.WARN` constants).

#### Exception Logging Migration
**Before:**
```python
try:
    risky_operation()
except Exception as e:
    logger.error(f"Operation failed: {e}", exc_info=True)
```

**After:**
```python
try:
    risky_operation()
except Exception as e:
    logger.exception(f"Operation failed: {e}")
```

**Rationale:** Powertools `logger.exception()` automatically captures exception context (stack trace, exception type, args). `exc_info=True` is not supported and will raise `TypeError`.

#### Log Level Mapping (no changes needed)
- `logger.debug()` → `logger.debug()` ✓
- `logger.info()` → `logger.info()` ✓
- `logger.warning()` → `logger.warning()` ✓
- `logger.error()` → `logger.error()` ✓
- `logger.critical()` → `logger.critical()` (rarely used) ✓

#### Structural Logging Examples
Powertools Logger supports both string and dict-style logging:
```python
# Simple string (works as-is)
logger.info("Deploy started")

# Structured fields
logger.info("Deploy started", extra={"project": "openclaw", "branch": "main"})

# Auto-extracts from f-strings (legacy pattern still works)
logger.info(f"Deploy started: project={project}, branch={branch}")
```

### Security Considerations
- **No sensitive data exposure:** Powertools Logger respects existing log sanitization (no new logging of secrets)
- **Audit trail preservation:** All existing log messages retained verbatim (no content changes)
- **CloudWatch permissions:** No IAM changes needed (Lambda already has `logs:CreateLogStream` and `logs:PutLogEvents`)

## Task Complexity Score (TCS)
| D1 Files | D2 Cross-module | D3 Testing | D4 Infra | D5 External | Total |
|----------|-----------------|------------|----------|-------------|-------|
| 5        | 0               | 1          | 0        | 0           | 6     |

**TCS = 6 (Simple)**

- **D1 Files (5):** Modify 13 files, but changes are mechanical (search-replace pattern)
- **D2 Cross-module (0):** No inter-module dependencies (each file is self-contained)
- **D3 Testing (1):** Verify logs appear in CloudWatch (no unit test changes needed)
- **D4 Infra (0):** No template.yaml or dependency changes (Powertools already in requirements)
- **D5 External (0):** No new external dependencies

## Test Requirements

### Manual Verification (Post-Deploy)
1. **Deploy to dev environment** and trigger each migrated module
2. **Check CloudWatch Logs** for JSON-structured output:
   ```json
   {
     "level": "INFO",
     "location": "telegram:42",
     "message": "Sending message to chat",
     "service": "bouncer",
     "timestamp": "2026-03-11T14:23:45.123Z"
   }
   ```
3. **Verify exception logs** include stack traces (test error paths)

### Pre-Deployment Checks
- **Grep check:** Confirm no `import logging` remains in migrated files (except where needed for constants)
  ```bash
  grep -n "^import logging$" src/{telegram,utils,accounts,notifications,mcp_deploy_frontend,mcp_history,metrics,sequence_analyzer,scheduler_service,paging,telegram_commands,mcp_presigned,mcp_upload}.py
  ```
- **Grep check:** Confirm no `exc_info=True` remains in migrated files
  ```bash
  grep -n "exc_info=True" src/{telegram,utils,accounts,notifications,mcp_deploy_frontend,mcp_history,metrics,sequence_analyzer,scheduler_service,paging,telegram_commands,mcp_presigned,mcp_upload}.py
  ```

### Unit Tests (if applicable)
- **No new tests required:** Existing tests that capture logs will see JSON format instead of plaintext
- **Test adaptation:** If any tests assert on log message format, update to parse JSON or use `caplog.records`

### Edge Cases
1. **Logger initialized in function scope** → Move to module level (standard pattern)
2. **Module uses `logging.basicConfig()`** → Remove (conflicts with Powertools)
3. **Module imports logging for non-logger purposes** → Keep import, only replace logger usage
4. **Third-party library logs** → Do not modify (external dependencies retain their own logging)

### Mock Paths (if needed for tests)
- None required (logging is side-effect, not return value)

## Implementation Checklist

For each file in the list:
- [ ] Remove `import logging` line
- [ ] Add `from aws_lambda_powertools import Logger`
- [ ] Replace `logger = logging.getLogger(__name__)` with `logger = Logger(service="bouncer")`
- [ ] Search for `exc_info=True` and replace with `logger.exception()`
- [ ] Verify no other logging-specific code (e.g., `logging.basicConfig()`, custom handlers)
- [ ] Test locally if possible (or deploy to dev)

## Migration Priority
1. **High-frequency modules** (most log volume, highest debugging value):
   - `telegram.py`, `utils.py`, `notifications.py`
2. **Medium-frequency modules** (occasional debugging):
   - `accounts.py`, `mcp_deploy_frontend.py`, `mcp_history.py`
3. **Low-frequency modules** (rarely logged, but complete for consistency):
   - `metrics.py`, `sequence_analyzer.py`, `scheduler_service.py`, `paging.py`, `telegram_commands.py`, `mcp_presigned.py`, `mcp_upload.py`

## Rollback Plan
If structured logging causes issues (e.g., log parsing errors):
1. Revert to previous commit (stdlib logging still works)
2. Investigate specific module causing issue
3. Partial rollback possible (revert one file at a time)

**Risk:** Low (Powertools Logger is battle-tested in AWS Lambda environments)
