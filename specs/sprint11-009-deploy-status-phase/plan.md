# Sprint 11-009: Plan — deploy_status phase fix + SFN inconsistency

> Generated: 2026-03-04

---

## Technical Context

### 現狀分析

1. **DDB record fields**: `deploy_id`, `project_id`, `status` (PENDING/RUNNING/SUCCESS/FAILED), `execution_arn`, `started_at`, `finished_at`, `error_lines`, etc. **No `phase` field**.

2. **`get_deploy_status()`** (`deployer.py:534-612`):
   - Record not found → `{status: 'pending'}` (Sprint 10 fix)
   - `status == 'RUNNING'` + `execution_arn` → SFN `describe_execution` → sync to DDB
   - SFN terminal → update DDB, release lock, send failure notification
   - Adds `elapsed_seconds` (RUNNING) / `duration_seconds` (terminal)

3. **SFN State Machine**: Has states like GitClone → SAMBuild → SAMDeploy → Changeset. Each state name is inferrable from `get_execution_history()`.

4. **Lock system**: `acquire_lock()` / `release_lock()` in `deployer.py`. Released on: SFN sync (poll), cancel, start failure. **NOT released** if nobody polls after SFN completes.

### Design

#### Part 1: Phase Field (#53)

**Approach: Extract phase from SFN execution history**

During `get_deploy_status()` when `status == 'RUNNING'`:
1. Call `get_execution_history(reverseOrder=True, maxResults=5)` to get latest events.
2. Map SFN state names to human-readable phases:
   - `GitClone` → `GIT_CLONE`
   - `SAMBuild` → `BUILDING`
   - `SAMDeploy` / `CreateChangeset` → `DEPLOYING`
   - Default / no events yet → `INITIALIZING`
3. Store `phase` in DDB record for caching.
4. On terminal state: `phase` = `COMPLETED` or `FAILED`.

**Fallback**: If SFN history call fails, `phase` = `UNKNOWN` (don't break the response).

#### Part 2: SFN Inconsistency (#56)

**Approach: Stale lock safety net (lower risk than EventBridge)**

In `get_deploy_status()`, add a stale-check:
1. If `status == 'RUNNING'` and `elapsed > 1800s` (30 min): force SFN sync.
2. If SFN is already terminal: update DDB, release lock (existing logic handles this).
3. If SFN is still running after 30 min: log warning, return as-is (legitimate long deploy).

**Additionally**: In `start_deploy()`, when checking existing lock:
1. If lock exists and age > 30 min: verify SFN status before returning conflict.
2. If SFN is terminal: release stale lock, allow new deploy.

This is a **safety net**, not a replacement for poll-driven sync. It catches the edge case of abandoned polls.

### Files Changed

| File | Change |
|------|--------|
| `src/deployer.py` | `get_deploy_status()`: add phase extraction; stale lock check |
| `src/deployer.py` | `start_deploy()`: stale lock detection before conflict |
| `tests/test_deployer.py` | Tests for phase field, stale lock scenarios |
