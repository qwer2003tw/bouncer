# Sprint 30-002: Tasks

## TCS Assessment: Simple

**Rationale:**
- Pure log format refactoring — no business logic changes
- No new dependencies, no schema changes, no infra changes
- Each file is independent (no cross-file dependencies for this change)
- No test changes required (verified: zero tests assert on `[TAG]` content)
- Powertools Logger `extra=` is already used in `deployer.py` — proven pattern

---

## Sub-Tasks

### Task 1: `grant.py` — Convert 15 [GRANT] / [GRANT][SEC-xxx] log lines

**File:** `src/grant.py`
**Lines:** 194, 217, 315, 382, 387, 413, 482, 507, 532, 565, 602, 605, 652, 655, 705
**Changes:** 15 log statements
**TCS:** Simple
**Estimated:** 15 min

**Checklist:**
- [ ] Convert all 15 f-string `[GRANT]` logs to `extra={}` format
- [ ] SEC-009 lines (602, 605): include `sec_rule: "SEC-009"` in extra
- [ ] Verify `pattern`, `command` context fields are preserved
- [ ] Run `grep -n '\[GRANT\]' src/grant.py` → 0 matches
- [ ] Run `python -m pytest tests/test_grant.py tests/test_security_sprint.py -x -q` → pass

### Task 2: `callbacks.py` — Convert 6 [GRANT] / [TRUST] / [TRUST][SEC-013] log lines

**File:** `src/callbacks.py`
**Lines:** 106, 147, 407, 422, 1696, 1767
**Changes:** 6 log statements
**TCS:** Simple
**Estimated:** 10 min

**Checklist:**
- [ ] Convert 2 `[GRANT]` lines (106, 147)
- [ ] Convert 4 `[TRUST]` lines (407, 422, 1696, 1767)
- [ ] SEC-013 line (1696): include `sec_rule: "SEC-013"`, `request_id`, `violation_rule`
- [ ] Line 407: preserve `exc_info=True`
- [ ] Line 1767: info level with `executed_count` field
- [ ] Run `grep -n '\[GRANT\]\|\[TRUST\]' src/callbacks.py` → 0 matches
- [ ] Run `python -m pytest tests/test_security_sprint.py -x -q` → pass

### Task 3: `notifications.py` — Convert 4 [GRANT] log lines

**File:** `src/notifications.py`
**Lines:** 488, 492, 539, 556
**Changes:** 4 log statements
**TCS:** Simple
**Estimated:** 5 min

**Checklist:**
- [ ] Convert all 4 `[GRANT]` lines to `extra={}` format
- [ ] Line 488: convert from %-format to extra (preserve `grant_id`)
- [ ] Run `grep -n '\[GRANT\]' src/notifications.py` → 0 matches
- [ ] Run `python -m pytest tests/ -k notification -x -q` → pass (or no matching tests → ok)

### Task 4: `mcp_execute.py` — Convert 2 [GRANT] log lines

**File:** `src/mcp_execute.py`
**Lines:** 602, 1116
**Changes:** 2 log statements
**TCS:** Simple
**Estimated:** 5 min

**Checklist:**
- [ ] Convert both `[GRANT]` lines to `extra={}` format
- [ ] Run `grep -n '\[GRANT\]' src/mcp_execute.py` → 0 matches
- [ ] Run `python -m pytest tests/test_mcp_execute*.py -x -q` → pass

### Task 5: `mcp_upload.py` — Convert 1 [UPLOAD] log line

**File:** `src/mcp_upload.py`
**Lines:** 1013
**Changes:** 1 log statement
**TCS:** Simple
**Estimated:** 3 min

**Checklist:**
- [ ] Convert `[UPLOAD]` line to `extra={}` format with `s3_key` field
- [ ] Run `grep -n '\[UPLOAD\]' src/mcp_upload.py` → 0 matches
- [ ] Run `python -m pytest tests/test_mcp_upload*.py -x -q` → pass

---

### Task 6: Final verification + commit

**TCS:** Simple
**Estimated:** 5 min

**Checklist:**
- [ ] `grep -rn '\[GRANT\]\|\[TRUST\]\|\[UPLOAD\]' src/grant.py src/callbacks.py src/notifications.py src/mcp_execute.py src/mcp_upload.py` → 0 matches
- [ ] Full test suite pass: `python -m pytest tests/ -x -q`
- [ ] Single commit: `feat(logging): convert 28 [TAG] prefixes to structured extra fields (#45)`
- [ ] Verify CloudWatch Log Insights queries work after deploy (post-deploy validation)

---

## Summary

| Task | File | Lines Changed | TCS | Est. Time |
|------|------|--------------|-----|-----------|
| 1 | `grant.py` | 15 | Simple | 15 min |
| 2 | `callbacks.py` | 6 | Simple | 10 min |
| 3 | `notifications.py` | 4 | Simple | 5 min |
| 4 | `mcp_execute.py` | 2 | Simple | 5 min |
| 5 | `mcp_upload.py` | 1 | Simple | 3 min |
| 6 | Verification + commit | — | Simple | 5 min |
| **Total** | **5 files** | **28 lines** | **Simple** | **~43 min** |

---

## Dependencies

- None. All tasks are independent and can be done in any order.
- Tasks 1-5 can theoretically be parallelized (different files), but a single agent doing them sequentially is simplest.

## Follow-Up (Out of Scope)

- `deployer.py`: Remove `[DEPLOYER]` prefix from message (already has `extra=`; ~10 lines)
- `mcp_deploy_frontend.py`: Convert `[DEPLOY-FRONTEND]` / `[deploy-frontend]` patterns (~17 lines)
- Full codebase scan for any remaining `[TAG]` patterns in other files
