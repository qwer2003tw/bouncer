# Sprint 30-002: Modification Plan

## Conversion Rules

1. **Remove `[TAG]` prefix** from log message string
2. **Add `extra={}` dict** with structured fields
3. **Preserve log level** (error/warning/info) — no changes
4. **Preserve exception handling** — return values, re-raise behavior unchanged
5. **Preserve `exc_info=True`** where already present
6. **f-string → plain string** for the message; dynamic values move to `extra=`
7. **%-format → plain string** where applicable; positional args move to `extra=`

### Field Naming Convention

- `module`: always lowercase, matches the tag (`"grant"`, `"trust"`, `"upload"`)
- `operation`: snake_case, derived from function name or log context
- `error`: always `str(e)`, never `repr(e)`
- `command`: truncated to 100 chars max (matching existing `[:100]` patterns)
- `pattern`: use `repr()` to match existing `{pattern!r}` format

---

## File 1: `grant.py` (15 changes)

### Line 194 — match_pattern error
```python
# Before
logger.error(f"[GRANT] match_pattern error for pattern={pattern!r}: {e}")

# After
logger.error("match_pattern error", extra={
    "module": "grant",
    "operation": "match_pattern",
    "pattern": repr(pattern),
    "error": str(e),
})
```

### Line 217 — normalize_command error
```python
# Before
logger.error(f"[GRANT] normalize_command error: {e}")

# After
logger.error("normalize_command error", extra={
    "module": "grant",
    "operation": "normalize_command",
    "error": str(e),
})
```

### Line 315 — create_grant_request error
```python
# Before
logger.error(f"[GRANT] create_grant_request error: {e}")

# After
logger.error("create_grant_request error", extra={
    "module": "grant",
    "operation": "create_grant_request",
    "error": str(e),
})
```

### Line 382 — risk scoring error
```python
# Before
logger.error(f"[GRANT] risk scoring error: {e}")

# After
logger.error("risk scoring error", extra={
    "module": "grant",
    "operation": "risk_scoring",
    "error": str(e),
})
```

### Line 387 — precheck error
```python
# Before
logger.error(f"[GRANT] precheck error for command '{command[:100]}': {e}")

# After
logger.error("precheck error", extra={
    "module": "grant",
    "operation": "precheck",
    "command": command[:100],
    "error": str(e),
})
```

### Line 413 — get_grant_session error
```python
# Before
logger.error(f"[GRANT] get_grant_session error: {e}")

# After
logger.error("get_grant_session error", extra={
    "module": "grant",
    "operation": "get_grant_session",
    "error": str(e),
})
```

### Line 482 — approve_grant error
```python
# Before
logger.error(f"[GRANT] approve_grant error: {e}")

# After
logger.error("approve_grant error", extra={
    "module": "grant",
    "operation": "approve_grant",
    "error": str(e),
})
```

### Line 507 — deny_grant error
```python
# Before
logger.error(f"[GRANT] deny_grant error: {e}")

# After
logger.error("deny_grant error", extra={
    "module": "grant",
    "operation": "deny_grant",
    "error": str(e),
})
```

### Line 532 — revoke_grant error
```python
# Before
logger.error(f"[GRANT] revoke_grant error: {e}")

# After
logger.error("revoke_grant error", extra={
    "module": "grant",
    "operation": "revoke_grant",
    "error": str(e),
})
```

### Line 565 — is_command_in_grant error
```python
# Before
logger.error(f"[GRANT] is_command_in_grant error: {e}")

# After
logger.error("is_command_in_grant error", extra={
    "module": "grant",
    "operation": "is_command_in_grant",
    "error": str(e),
})
```

### Line 602 — SEC-009 warning (dangerous command repeat limit)
```python
# Before
logger.warning(f"[GRANT][SEC-009] Dangerous command repeat limit reached: {normalized_cmd[:80]!r}")

# After
logger.warning("Dangerous command repeat limit reached", extra={
    "module": "grant",
    "operation": "try_use_grant_command",
    "sec_rule": "SEC-009",
    "command": normalized_cmd[:80],
})
```

### Line 605 — SEC-009 error (failed to read repeat count)
```python
# Before
logger.error(f"[GRANT][SEC-009] Failed to read repeat count: {e}")

# After
logger.error("Failed to read repeat count", extra={
    "module": "grant",
    "operation": "try_use_grant_command",
    "sec_rule": "SEC-009",
    "error": str(e),
})
```

### Line 652 — try_use_grant_command ClientError
```python
# Before
logger.error(f"[GRANT] try_use_grant_command ClientError: {e}")

# After
logger.error("try_use_grant_command ClientError", extra={
    "module": "grant",
    "operation": "try_use_grant_command",
    "error": str(e),
})
```

### Line 655 — try_use_grant_command error
```python
# Before
logger.error(f"[GRANT] try_use_grant_command error: {e}")

# After
logger.error("try_use_grant_command error", extra={
    "module": "grant",
    "operation": "try_use_grant_command",
    "error": str(e),
})
```

### Line 705 — get_grant_status error
```python
# Before
logger.error(f"[GRANT] get_grant_status error: {e}")

# After
logger.error("get_grant_status error", extra={
    "module": "grant",
    "operation": "get_grant_status",
    "error": str(e),
})
```

---

## File 2: `callbacks.py` (6 changes)

### Line 106 — handle_grant_approve error
```python
# Before
logger.error(f"[GRANT] handle_grant_approve error (mode={mode}): {e}")

# After
logger.error("handle_grant_approve error", extra={
    "module": "grant",
    "operation": "handle_grant_approve",
    "mode": mode,
    "error": str(e),
})
```

### Line 147 — handle_grant_deny error
```python
# Before
logger.error(f"[GRANT] handle_grant_deny error: {e}")

# After
logger.error("handle_grant_deny error", extra={
    "module": "grant",
    "operation": "handle_grant_deny",
    "error": str(e),
})
```

### Line 407 — TRUST query pending items warning
```python
# Before
logger.warning("[TRUST] Failed to query pending items for trust_scope=%s, skipping",
               trust_scope, exc_info=True)

# After
logger.warning("Failed to query pending items, skipping", extra={
    "module": "trust",
    "operation": "query_pending_items",
    "trust_scope": trust_scope,
}, exc_info=True)
```
**Note:** `exc_info=True` is preserved.

### Line 422 — TRUST auto-execute pending error
```python
# Before
logger.error(f"[TRUST] Auto-execute pending error: {e}")

# After
logger.error("Auto-execute pending error", extra={
    "module": "trust",
    "operation": "auto_execute_pending",
    "error": str(e),
})
```

### Line 1696 — SEC-013 compliance warning
```python
# Before
logger.warning(f"[TRUST][SEC-013] Pending request {req_id} failed compliance: "
               f"{violation.rule_id if violation else 'unknown'}")

# After
logger.warning("Pending request failed compliance", extra={
    "module": "trust",
    "operation": "auto_execute_pending",
    "sec_rule": "SEC-013",
    "request_id": req_id,
    "violation_rule": violation.rule_id if violation else "unknown",
})
```

### Line 1767 — TRUST auto-executed info
```python
# Before
logger.info(f"[TRUST] Auto-executed {executed} pending requests for trust_scope={trust_scope}")

# After
logger.info("Auto-executed pending requests", extra={
    "module": "trust",
    "operation": "auto_execute_pending",
    "executed_count": executed,
    "trust_scope": trust_scope,
})
```

---

## File 3: `notifications.py` (4 changes)

### Line 488-489 — post_notification_setup failed
```python
# Before
logger.error(
    "[GRANT] post_notification_setup failed for %s: %s", grant_id, pns_exc
)

# After
logger.error("post_notification_setup failed", extra={
    "module": "grant",
    "operation": "send_grant_request_notification",
    "grant_id": grant_id,
    "error": str(pns_exc),
})
```

### Line 492 — send_grant_request_notification error
```python
# Before
logger.error(f"[GRANT] send_grant_request_notification error: {e}")

# After
logger.error("send_grant_request_notification error", extra={
    "module": "grant",
    "operation": "send_grant_request_notification",
    "error": str(e),
})
```

### Line 539 — send_grant_execute_notification error
```python
# Before
logger.error(f"[GRANT] send_grant_execute_notification error: {e}")

# After
logger.error("send_grant_execute_notification error", extra={
    "module": "grant",
    "operation": "send_grant_execute_notification",
    "error": str(e),
})
```

### Line 556 — send_grant_complete_notification error
```python
# Before
logger.error(f"[GRANT] send_grant_complete_notification error: {e}")

# After
logger.error("send_grant_complete_notification error", extra={
    "module": "grant",
    "operation": "send_grant_complete_notification",
    "error": str(e),
})
```

---

## File 4: `mcp_execute.py` (2 changes)

### Line 602 — _check_grant_session error
```python
# Before
logger.error(f"[GRANT] _check_grant_session error: {e}")

# After
logger.error("_check_grant_session error", extra={
    "module": "grant",
    "operation": "check_grant_session",
    "error": str(e),
})
```

### Line 1116 — Failed to send notification
```python
# Before
logger.error(f"[GRANT] Failed to send notification: {e}")

# After
logger.error("Failed to send grant notification", extra={
    "module": "grant",
    "operation": "send_notification",
    "error": str(e),
})
```

---

## File 5: `mcp_upload.py` (1 change)

### Line 1013 — Staging cleanup failed
```python
# Before
logger.warning("[UPLOAD] Staging cleanup failed for key=%s (non-critical, TTL will handle it)",
               content_s3_key)

# After
logger.warning("Staging cleanup failed (non-critical, TTL will handle it)", extra={
    "module": "upload",
    "operation": "staging_cleanup",
    "s3_key": content_s3_key,
})
```

---

## Test Synchronization Strategy

### Analysis Result: No Test Changes Required

Verified via grep:
- `grep -rn '\[GRANT\]\|\[TRUST\]\|\[UPLOAD\]' tests/` → **0 matches**
- `grep -rn 'assert.*GRANT\|assert.*TRUST\|assert.*UPLOAD' tests/` → **0 matches**
- No tests assert on log message content containing `[TAG]` patterns

The SEC-009 and SEC-013 tests (`test_security_sprint.py`) test **behavior** (return values, DDB state),
not log message format.

### Verification Step (post-implementation)

```bash
# Run full test suite to confirm no regressions
cd /home/ec2-user/projects/bouncer && python -m pytest tests/ -x -q

# Spot-check that extra fields appear in Powertools JSON output
python -c "
from aws_lambda_powertools import Logger
import json, io, sys

logger = Logger(service='test', stream=io.StringIO())
logger.error('test message', extra={'module': 'grant', 'operation': 'test', 'error': 'boom'})
output = logger.registered_handler.stream.getvalue()
parsed = json.loads(output)
assert parsed['module'] == 'grant'
assert parsed['operation'] == 'test'
assert parsed['error'] == 'boom'
assert '[GRANT]' not in parsed['message']
print('✅ Extra fields serialized correctly')
"
```

---

## Rollback Plan

If issues are discovered post-deploy:
1. Revert the single commit (all changes are log format only)
2. No data migration needed
3. No CloudFormation changes
4. No DynamoDB schema changes

Zero-risk rollback — the old format and new format both work with Powertools Logger.
