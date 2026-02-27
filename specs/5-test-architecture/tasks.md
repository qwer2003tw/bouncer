# Sprint 5: Test Architecture Improvement — Task List

## Priority Legend
- **P0** = Critical path, must complete first
- **P1** = Important, can parallel after P0
- **P2** = Enhancement, after P1
- **[P]** = Parallelizable with other [P] tasks at same priority

## Dependency Graph

```
T1 (conftest.py)
 ├── T2 [P] (Batch A: 4 files)
 ├── T3 [P] (Batch B: 4 files)
 ├── T4 [P] (Batch C: 4 files)
 └── T5 (Batch D: 2 files + verify + delete)
      ├── T6 [P] (notifications coverage)
      ├── T7 [P] (mcp_execute coverage)
      ├── T8 [P] (callbacks/upload/deployer coverage)
      └── T9 (freezegun fix)
```

---

## Tasks

### [T1] [P0] conftest.py 建立 + Fixture 遷移

**Estimated:** 30 min

**Steps:**
1. Create `tests/conftest.py`
2. Copy lines 1-199 from `test_bouncer.py` (imports, `mock_dynamodb`, `app_module`, `_ALL_TABLE_KEYS`, `_cleanup_tables`)
3. Remove the fixture definitions from `test_bouncer.py` header (replace with `import` of needed stdlib only)
4. Verify `pytest tests/test_bouncer.py -q --co` still collects all tests
5. Verify `pytest tests/test_bouncer.py -q -x --tb=short` still passes (run first 10 tests)

**Done when:**
- `tests/conftest.py` exists
- `test_bouncer.py` still works with fixtures from conftest.py
- All pre-existing test files (`test_grant.py`, `test_history.py`, etc.) still pass

**Output:** `tests/conftest.py` (≈200 lines)

---

### [T2] [P0] [P] test_bouncer.py 拆分 — Batch A（4 files, 234 tests）

**Estimated:** 1 hour | **Depends on:** T1

**Files to create:**

| File | Classes to Extract | Line Ranges |
|---|---|---|
| `test_commands.py` | TestCommandClassification (L690), TestSecurity (L804), TestSecurityWhitespaceBypass (L831), TestSecurityFileProtocol (L900), TestCommandClassificationExtended (L1142), TestAwsCliSplit (L1202), TestCommandsModule (L1558), TestCommandClassificationEdgeCases (L1910), TestAwsCliSplitEdgeCases (L1939), TestExecuteCommand (L1981), TestCommandsModuleAdditional (L3867), TestCommandsModuleFull (L4372), TestCommandsMore (L5093), TestCommandsExtra (L5445), TestExecuteCommandAdditional (L3793), TestHelpCommand (L5663) | 16 classes, ~87 tests |
| `test_mcp_execute.py` | TestMCPInitialize (L202), TestMCPToolsList (L230), TestMCPExecuteSafelist (L259), TestMCPExecuteApproval (L327), TestMCPListSafelist (L401), TestMCPErrors (L430), TestMCPToolStatus (L1599), TestMCPToolGetPage (L1608), TestMCPToolExecuteEdgeCases (L1617), TestMCPFormatting (L1721), TestMCPToolCallRouting (L1792), TestMCPRequestHandler (L1845), TestMCPToolHandlersAdditional (L3285), TestMCPRequestValidation (L3920), TestMCPToolsListAdditional (L3957), TestMCPAddAccount (L5638), TestMCPAddAccountFull (L4538), TestMCPRemoveAccount (L5330), TestMCPListPending (L5363), TestMCPStatus (L5495), TestMCPSafelist (L5515), TestSyncModeExecute (L6343), TestSyncAsyncMode (L6639), TestCrossAccountExecuteFlow (L6410), TestCrossAccountExecuteErrors (L6664) | 25 classes, ~49 tests |
| `test_deployer_main.py` | TestDeployerModule (L2683), TestMCPDeployTools (L3047), TestDeployerMCPTools (L3466), TestDeployerMCPToolsAdditional (L4108), TestDeployerAdditional (L3987), TestDeployerExtra (L5527), TestDeployerFull (L4299), TestDeployerMore (L4701), TestDeployerMoreExtended (L5125), TestCrossAccountDeploy (L6022), TestDeployNotificationFallback (L6112), TestBlockedCommandPath (L2157), TestBlockedCommands (L3735), TestMCPExecuteBlocked (L295), TestRESTBlocked (L542), TestSecurityBlockedFlags (L867) | 16 classes, ~53 tests |
| `test_app.py` | TestRESTSafelist (L518), TestRESTApproval (L563), TestRESTHandler (L1884), TestRESTAPIFull (L4575), TestRESTAPIHandlerAdditional (L3579), TestIntegration (L924), TestStatusQuery (L977), TestStatusQueryEdgeCases (L1994), TestLambdaHandler (L1677), TestLambdaHandlerRouting (L3691), TestLambdaHandlerMore (L4836), TestHMACVerification (L1657), TestHMACVerificationAdditional (L3900), TestValidation (L5595), TestSendApprovalRequest (L1962), TestAppModuleMore (L4600), TestAccountValidationErrorPaths (L2047), TestAdditionalCoverage (L4040), TestCoverage80Sprint (L4880) | 19 classes, ~45 tests |

**Per-file steps:**
1. Create file with header imports
2. Copy all classes (preserve exact indentation)
3. Run `pytest tests/<file> -q -x` — must pass
4. Count test methods — must match expected

**Done when:** All 4 files pass individually

---

### [T3] [P0] [P] test_bouncer.py 拆分 — Batch B（4 files, 101 tests）

**Estimated:** 45 min | **Depends on:** T1

| File | Classes to Extract | Tests |
|---|---|---|
| `test_trust.py` | TestTrustSession (L1018), TestTrustExcluded (L1458), TestTrustCommandHandler (L1820), TestTrustModuleAdditional (L3083), TestTrustModuleFull (L4243), TestTrustMore (L5478), TestTrustAutoApprove (L3230), TestTrustSessionLimits (L6264), TestTrustSessionExpiry (L6543), TestCallbackTrust (L5554) | ~27 |
| `test_telegram_main.py` | TestTelegramWebhook (L592), TestTelegramModule (L1498), TestTelegramCommands (L1635), TestTelegramCommandHandler (L1770), TestTelegramWebhookHandler (L1748), TestTelegramCommandsAdditional (L3623), TestTelegramModuleFull (L4199), TestTelegramMore (L5179), TestTelegramMessageFunctions (L3834), TestWebhookMessage (L5406) | ~25 |
| `test_callbacks_main.py` | TestTelegramCallbackHandlers (L2291), TestPendingCommandHandler (L1830), TestCallbackHandlersFull (L4399), TestCallbackHandlers (L5215), TestAlreadyProcessedDisplay (L6972), TestOrphanApprovalCleanup (L7117) | ~27 |
| `test_notifications_main.py` | TestGenerateDisplaySummary (L6715), TestDisplaySummaryInItems (L6805) | ~19 |

---

### [T4] [P0] [P] test_bouncer.py 拆分 — Batch C（4 files, 52 tests）

**Estimated:** 30 min | **Depends on:** T1

| File | Classes | Tests |
|---|---|---|
| `test_mcp_upload_main.py` | TestUploadFunctionality (L3147), TestCrossAccountUpload (L5721), TestCrossAccountUploadExecution (L5872), TestCrossAccountUploadCallback (L5950), TestUploadDenyCallbackAccount (L6207) | ~16 |
| `test_accounts_main.py` | TestAccounts (L1396), TestAccountsModuleFull (L4519), TestAccountsMore (L5198), TestAccountValidationErrorPaths (L2047) | ~13 |
| `test_paging.py` | TestOutputPaging (L1096), TestPagingModule (L1536), TestPagingModuleFull (L4146), TestPagingMore (L4806), TestPagingMoreExtended (L5394), TestPagedOutputAdditional (L3813) | ~14 |
| `test_rate_limit.py` | TestRateLimiting (L1073), TestRateLimitEdgeCases (L2006), TestRateLimitErrors (L2218), TestRateLimitFull (L4278), TestRateLimitMore (L5158) | ~9 |

---

### [T5] [P0] test_bouncer.py 拆分 — Batch D + 驗證 + 刪除

**Estimated:** 45 min | **Depends on:** T2, T3, T4

**Steps:**
1. Create `test_constants.py` (TestConstants, L2024) — ~3 tests
2. Create `test_utils.py` (TestHelperFunctions L1698, TestHelperFunctionsAdditional L3652, TestDecimalConversion L5615) — ~10 tests
3. **Full verification:**
   ```bash
   # Count tests in all new files
   grep -c "def test_" tests/test_commands.py tests/test_mcp_execute.py ... | awk -F: '{sum+=$2} END {print sum}'
   # Must equal: grep -c "def test_" tests/test_bouncer.py  (currently ~400)
   
   # Run all new files
   pytest tests/test_commands.py tests/test_mcp_execute.py tests/test_deployer_main.py \
     tests/test_app.py tests/test_trust.py tests/test_telegram_main.py \
     tests/test_callbacks_main.py tests/test_notifications_main.py \
     tests/test_mcp_upload_main.py tests/test_accounts_main.py \
     tests/test_paging.py tests/test_rate_limit.py \
     tests/test_constants.py tests/test_utils.py -q
   
   # Run ALL tests (new + pre-existing)
   pytest tests/ -q --ignore=tests/test_bouncer.py
   ```
4. Delete `test_bouncer.py`
5. Run `pytest tests/ -q` — final confirmation

**Done when:** All tests pass, test_bouncer.py deleted, zero test loss

> **Note on TestAccountValidationErrorPaths:** This class appears in both Batch A (test_app.py) and Batch C (test_accounts_main.py) mapping above. During implementation, place it in `test_accounts_main.py` only (it tests accounts validation). Remove from test_app.py list.

---

### [T6] [P1] [P] notifications.py 測試補寫

**Estimated:** 1 hour | **Depends on:** T5

**Target file:** `tests/test_notifications_main.py` (append to file created in T3)

**Tests to add (~25):**
- `send_approval_request`: happy path, long command truncation, markdown escaping, keyboard structure
- `send_account_approval_request`: add/remove variants, with/without context
- `send_trust_auto_approve_notification`: with display_summary, various count values
- `send_grant_request_notification`: multi-command, cross-account, allow_repeat variants
- `send_grant_execute_notification`: success/failure paths
- `send_grant_complete_notification`: happy path
- `send_blocked_notification`: various block reasons
- `send_trust_upload_notification`: with/without display_summary
- `send_batch_upload_notification`: multi-file, single file, cross-account
- `send_presigned_notification`: happy path
- `send_presigned_batch_notification`: happy path
- `_escape_markdown`: special characters
- Error handling: `_send_message` raises exception

**Coverage target:** 59% → 80%+

---

### [T7] [P1] [P] mcp_execute.py 測試補寫（L882-1020）

**Estimated:** 45 min | **Depends on:** T5

**Target file:** `tests/test_mcp_execute.py` (append to file created in T2)

**Tests to add (~15):**
- `mcp_tool_request_grant`:
  - Happy path (all params valid)
  - Missing commands → -32602
  - Missing reason → -32602
  - Missing source → -32602
  - Invalid account → error result
  - Non-existent account → error result
  - With ttl_minutes → passed through
  - With allow_repeat=True → passed through
  - Notification failure → still returns success
  - ValueError from grant module → error result
  - Internal exception → -32603
- `mcp_tool_grant_status`:
  - Happy path
  - Not found
  - Missing params
- `mcp_tool_revoke_grant`:
  - Happy path
  - Failure
  - Missing grant_id

**Coverage target:** 72% → 80%+

---

### [T8] [P1] [P] callbacks / mcp_upload / deployer 測試補寫

**Estimated:** 1 hour | **Depends on:** T5

**callbacks.py tests (in `test_callbacks_main.py`):**
- `handle_grant_approve` / `handle_grant_approve_all` / `handle_grant_approve_safe`
- `handle_grant_deny`
- `_auto_execute_pending_requests` with multiple pending
- `_send_status_update` edge cases (missing fields)
- ~10 new tests

**mcp_upload.py tests (in `test_mcp_upload_main.py`):**
- `_check_upload_rate_limit` edge cases
- `_check_upload_trust` expired trust
- `execute_upload` error paths
- ~5 new tests

**deployer.py tests (in `test_deployer_main.py`):**
- `acquire_lock` when lock already held
- `release_lock` when no lock
- `cancel_deploy` when already complete
- `get_deploy_history` empty
- ~5 new tests

**Coverage targets:**
- callbacks.py: 79% → 85%+
- mcp_upload.py: maintain or improve
- deployer.py: maintain or improve

---

### [T9] [P2] Flaky Tests freezegun 修復

**Estimated:** 1 hour | **Depends on:** T5

**Steps:**
1. Add `freezegun>=1.2.0` to test dependencies
2. Define constants:
   ```python
   # tests/conftest.py (or each file)
   FROZEN_TIME = "2025-01-15T12:00:00Z"
   FROZEN_EPOCH = 1736942400
   ```
3. Apply `@freeze_time` to all 60 `time.time()` hotspots:
   - Trust classes: TestTrustSession, TestTrustSessionExpiry, TestTrustSessionLimits
   - Callback classes: TestTelegramCallbackHandlers, TestCallbackHandlers, TestCallbackTrust
   - Deployer classes: TestDeployerModule (lock timestamps)
   - Rate limit classes: TestRateLimitMore
4. Replace `time.time()` in test data with `FROZEN_EPOCH` arithmetic
5. Replace `str(time.time())` in unique IDs with fixed strings
6. Verify: run each affected file 10 times:
   ```bash
   for i in $(seq 10); do pytest tests/test_trust.py -q || echo "FAIL on run $i"; done
   ```

**Done when:** Zero flaky failures in 10 consecutive runs across all affected files

---

## Summary

| Task | Priority | Parallel? | Est. Time | Tests |
|---|---|---|---|---|
| T1 | P0 | No | 30 min | — |
| T2 | P0 | Yes (with T3, T4) | 1 hr | 234 |
| T3 | P0 | Yes (with T2, T4) | 45 min | 101 |
| T4 | P0 | Yes (with T2, T3) | 30 min | 52 |
| T5 | P0 | No (after T2-T4) | 45 min | 13 + verify |
| T6 | P1 | Yes (with T7, T8) | 1 hr | ~25 new |
| T7 | P1 | Yes (with T6, T8) | 45 min | ~15 new |
| T8 | P1 | Yes (with T6, T7) | 1 hr | ~20 new |
| T9 | P2 | No | 1 hr | — (fix) |

**Total estimated:** ~7.5 hours of work, reducible to ~4 hours with 3 parallel agents on T2/T3/T4 and T6/T7/T8.
