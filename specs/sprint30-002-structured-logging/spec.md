# Sprint 30-002: [TAG] → Structured Extra Fields

**Status:** Draft
**Priority:** P2 — Observability improvement
**Estimated TCS:** Simple (per-file conversion, no logic changes)
**Risk:** Low — log format only, no business logic affected

---

## Background

Powertools Logger migration (Sprint 26-29) is complete: 22 modules, 0 stdlib logging calls remain.
However, **28 log statements** across 5 files still use `[TAG]` prefix string format
(e.g., `logger.error(f"[GRANT] approve_grant error: {e}")`).

This prevents effective CloudWatch Log Insights structured queries. With Powertools Logger,
the `extra={}` parameter serializes to top-level JSON keys, enabling field-based filtering.

### Current State

- `deployer.py` already uses `extra={}` (but still has `[DEPLOYER]` prefix — out of scope for this sprint)
- `mcp_deploy_frontend.py` has 17 additional `[DEPLOY-FRONTEND]` patterns — out of scope (separate task)
- `trust.py` already uses clean messages without `[TAG]` prefix — no changes needed

### In-Scope Files (28 lines, 5 files)

| File | Count | Tags |
|------|-------|------|
| `grant.py` | 15 | `[GRANT]`, `[GRANT][SEC-009]` |
| `callbacks.py` | 6 | `[GRANT]`, `[TRUST]`, `[TRUST][SEC-013]` |
| `notifications.py` | 4 | `[GRANT]` |
| `mcp_execute.py` | 2 | `[GRANT]` |
| `mcp_upload.py` | 1 | `[UPLOAD]` |

---

## User Stories

### US-1: Structured module filtering in CloudWatch Log Insights

**As** an operator monitoring Bouncer in production,
**I want** log entries to include structured `module` and `operation` fields,
**So that** I can filter and aggregate logs using CloudWatch Log Insights queries
instead of regex-matching `[TAG]` strings.

**Acceptance Criteria:**

1. All 28 `[TAG]` prefix log lines are converted to use `extra={}` with structured fields
2. No `[TAG]` pattern remains in the 5 in-scope files
3. Each converted log includes at minimum: `module` (string) and human-readable message
4. Error logs include `error` field with `str(e)`
5. Context-specific fields are preserved (e.g., `grant_id`, `pattern`, `command`, `trust_scope`)

### US-2: Security rule ID as structured field

**As** a security auditor,
**I want** security rule violations (SEC-009, SEC-013) to have a dedicated `sec_rule` extra field,
**So that** I can query all security events with `filter sec_rule like /SEC-/` in Log Insights.

**Acceptance Criteria:**

1. `SEC-009` lines in `grant.py` include `extra={"sec_rule": "SEC-009", ...}`
2. `SEC-013` line in `callbacks.py` includes `extra={"sec_rule": "SEC-013", ...}`
3. Security rule IDs are no longer embedded in the log message string

### US-3: No functional regression

**As** a developer,
**I want** log format changes to not affect any business logic or test assertions,
**So that** the existing test suite passes without modification.

**Acceptance Criteria:**

1. All existing tests pass without changes (no tests assert on `[TAG]` string content)
2. Log level (error/warning/info) remains unchanged for each line
3. Exception handling behavior (return value, re-raise) remains unchanged
4. `exc_info=True` is preserved where currently used

---

## Standard Extra Fields Convention

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `module` | `str` | Functional module name | `"grant"`, `"trust"`, `"upload"` |
| `operation` | `str` | Specific operation that failed/logged | `"match_pattern"`, `"approve"`, `"revoke"` |
| `error` | `str` | `str(e)` from caught exception | `"KeyError: 'status'"` |
| `sec_rule` | `str` | Security rule ID (only for SEC-xxx logs) | `"SEC-009"`, `"SEC-013"` |
| `grant_id` | `str` | Grant session ID (when available in scope) | `"grant-abc123"` |
| `trust_scope` | `str` | Trust scope identifier | `"private-bot-deploy"` |
| `request_id` | `str` | Request ID (when available) | `"req-xyz789"` |
| `command` | `str` | Truncated command string (max 100 chars) | `"aws s3 ls s3://bucket"` |
| `pattern` | `str` | Pattern string (for match_pattern errors) | `"aws s3 cp *"` |
| `mode` | `str` | Operation mode (when relevant) | `"all"`, `"safe_only"` |
| `s3_key` | `str` | S3 object key (for upload operations) | `"2026-03-11/uuid/file.txt"` |

### Conversion Examples

```python
# === Before ===
logger.error(f"[GRANT] match_pattern error for pattern={pattern!r}: {e}")

# === After ===
logger.error("match_pattern error", extra={
    "module": "grant",
    "operation": "match_pattern",
    "pattern": repr(pattern),
    "error": str(e),
})

# === Before (SEC tag) ===
logger.warning(f"[GRANT][SEC-009] Dangerous command repeat limit reached: {normalized_cmd[:80]!r}")

# === After ===
logger.warning("Dangerous command repeat limit reached", extra={
    "module": "grant",
    "operation": "try_use_grant_command",
    "sec_rule": "SEC-009",
    "command": normalized_cmd[:80],
})

# === Before (%-format with exc_info) ===
logger.warning("[TRUST] Failed to query pending items for trust_scope=%s, skipping",
               trust_scope, exc_info=True)

# === After ===
logger.warning("Failed to query pending items, skipping", extra={
    "module": "trust",
    "operation": "query_pending_items",
    "trust_scope": trust_scope,
}, exc_info=True)
```

---

## CloudWatch Log Insights Query Examples

After conversion, the following queries become possible:

```sql
-- All grant module errors in the last hour
fields @timestamp, message, operation, error, grant_id
| filter module = "grant" and level = "ERROR"
| sort @timestamp desc
| limit 50

-- Security rule violations
fields @timestamp, message, sec_rule, command, module
| filter sec_rule like /SEC-/
| sort @timestamp desc

-- Grant operation error frequency
fields module, operation
| filter module = "grant" and level = "ERROR"
| stats count(*) by operation
| sort count desc

-- Trust auto-execute activity
fields @timestamp, message, trust_scope
| filter module = "trust" and operation = "auto_execute_pending"
| sort @timestamp desc

-- Upload staging cleanup failures
fields @timestamp, message, s3_key
| filter module = "upload" and operation = "staging_cleanup"
```

---

## Out of Scope (Follow-Up Tasks)

| Item | File | Count | Notes |
|------|------|-------|-------|
| `[DEPLOYER]` prefix (already has `extra=`) | `deployer.py` | ~10 | Remove prefix from message, keep `extra=` |
| `[DEPLOY-FRONTEND]` / `[deploy-frontend]` prefix | `mcp_deploy_frontend.py` | 17 | Full conversion needed |
| Other files with `[TAG]` patterns | TBD | TBD | Full codebase scan recommended |

These should be tracked as sprint30-003 or later.
