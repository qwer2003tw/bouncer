# Sprint 5: Test Architecture Improvement — Specification

**Sprint Goal:** Root-fix OOM by splitting the monolithic test file, fill coverage gaps, and eliminate flaky tests.

**Source Data:**
- `tests/test_bouncer.py`: 7,334 lines, 127 classes, ~400 test methods, `app_module` referenced 831 times
- No `tests/conftest.py` exists today
- 60 bare `time.time()` calls in test data setup (flaky risk)
- Source: 31 modules in `src/`, 13,731 total lines

---

## Story 1: Split test_bouncer.py into Module-Aligned Test Files

**Task ID:** `bouncer-test-005` 🔴 Critical Path

### Background

`test_bouncer.py` is a single 7,334-line file containing 127 test classes and ~400 test methods. When pytest collects this file it loads everything into memory at once; combined with moto's DynamoDB/S3 mocks and the module-scoped `app_module` fixture, this is the primary OOM trigger.

### Target Files (14)

| # | Target File | Source Classes (count) | Tests |
|---|---|---|---|
| 1 | `tests/test_app.py` | TestIntegration, TestStatusQuery, TestLambdaHandler, TestHMACVerification, TestRESTSafelist, TestRESTBlocked, TestRESTApproval, TestRESTHandler, TestRESTAPIFull, TestRESTAPIHandlerAdditional, TestLambdaHandlerRouting, TestLambdaHandlerMore, TestHMACVerificationAdditional, TestValidation, TestStatusQueryEdgeCases, TestAppModuleMore, TestAdditionalCoverage, TestCoverage80Sprint, TestSendApprovalRequest | ~45 |
| 2 | `tests/test_mcp_execute.py` | TestMCPInitialize, TestMCPToolsList, TestMCPExecuteSafelist, TestMCPExecuteApproval, TestMCPListSafelist, TestMCPErrors, TestMCPToolStatus, TestMCPToolGetPage, TestMCPToolExecuteEdgeCases, TestMCPFormatting, TestMCPToolCallRouting, TestMCPRequestHandler, TestMCPToolHandlersAdditional, TestMCPRequestValidation, TestMCPToolsListAdditional, TestMCPAddAccount, TestMCPAddAccountFull, TestMCPRemoveAccount, TestMCPListPending, TestMCPStatus, TestMCPSafelist, TestSyncModeExecute, TestSyncAsyncMode, TestCrossAccountExecuteFlow, TestCrossAccountExecuteErrors | ~49 |
| 3 | `tests/test_mcp_upload_main.py` | TestUploadFunctionality, TestCrossAccountUpload, TestCrossAccountUploadExecution, TestCrossAccountUploadCallback, TestUploadDenyCallbackAccount | ~16 |
| 4 | `tests/test_commands.py` | TestCommandClassification, TestCommandClassificationExtended, TestCommandClassificationEdgeCases, TestCommandsModule, TestCommandsModuleAdditional, TestCommandsModuleFull, TestCommandsMore, TestCommandsExtra, TestAwsCliSplit, TestAwsCliSplitEdgeCases, TestSecurity, TestSecurityWhitespaceBypass, TestSecurityFileProtocol, TestExecuteCommand, TestExecuteCommandAdditional, TestHelpCommand | ~87 |
| 5 | `tests/test_trust.py` | TestTrustSession, TestTrustExcluded, TestTrustCommandHandler, TestTrustModuleAdditional, TestTrustModuleFull, TestTrustMore, TestTrustAutoApprove, TestTrustSessionLimits, TestTrustSessionExpiry, TestCallbackTrust | ~27 |
| 6 | `tests/test_telegram_main.py` | TestTelegramWebhook, TestTelegramModule, TestTelegramCommands, TestTelegramCommandHandler, TestTelegramWebhookHandler, TestTelegramCommandsAdditional, TestTelegramModuleFull, TestTelegramMore, TestTelegramMessageFunctions, TestWebhookMessage | ~25 |
| 7 | `tests/test_callbacks_main.py` | TestTelegramCallbackHandlers, TestPendingCommandHandler, TestCallbackHandlersFull, TestCallbackHandlers, TestAlreadyProcessedDisplay, TestOrphanApprovalCleanup | ~27 |
| 8 | `tests/test_deployer_main.py` | TestDeployerModule, TestMCPDeployTools, TestDeployerMCPTools, TestDeployerMCPToolsAdditional, TestDeployerAdditional, TestDeployerExtra, TestDeployerFull, TestDeployerMore, TestDeployerMoreExtended, TestCrossAccountDeploy, TestDeployNotificationFallback, TestBlockedCommandPath, TestBlockedCommands, TestMCPExecuteBlocked, TestRESTBlocked, TestSecurityBlockedFlags | ~53 |
| 9 | `tests/test_accounts_main.py` | TestAccounts, TestAccountsModuleFull, TestAccountsMore, TestAccountValidationErrorPaths | ~13 |
| 10 | `tests/test_paging.py` | TestOutputPaging, TestPagingModule, TestPagingModuleFull, TestPagingMore, TestPagingMoreExtended, TestPagedOutputAdditional | ~14 |
| 11 | `tests/test_rate_limit.py` | TestRateLimiting, TestRateLimitEdgeCases, TestRateLimitErrors, TestRateLimitFull, TestRateLimitMore | ~9 |
| 12 | `tests/test_constants.py` | TestConstants | ~3 |
| 13 | `tests/test_utils.py` | TestHelperFunctions, TestHelperFunctionsAdditional, TestDecimalConversion | ~10 |
| 14 | `tests/test_notifications_main.py` | TestGenerateDisplaySummary, TestDisplaySummaryInItems | ~19 |

> **Naming note:** Files suffixed `_main` avoid collision with existing test files (e.g., `test_upload_trust.py`, `test_history.py`).

### Acceptance Scenarios

**Scenario 1: conftest.py provides shared fixtures**
- **Given** a new `tests/conftest.py` exists with `mock_dynamodb`, `app_module`, and `_cleanup_tables` fixtures
- **When** any individual test file is run with `pytest tests/test_commands.py -q`
- **Then** fixtures are auto-discovered and all tests pass

**Scenario 2: Each file runs independently**
- **Given** all 14 target files are created
- **When** each file is run individually (`pytest tests/test_X.py -q`)
- **Then** all tests pass with exit code 0

**Scenario 3: Full suite passes**
- **Given** all 14 files exist and `test_bouncer.py` is deleted
- **When** `pytest tests/ -q` runs the full suite
- **Then** all ~400 migrated tests pass, plus all pre-existing test files still pass

**Scenario 4: No test loss**
- **Given** the original test_bouncer.py had N test methods
- **When** we count test methods across all 14 new files
- **Then** the total equals N (zero loss)

**Scenario 5: Memory usage drops**
- **Given** the monolithic test_bouncer.py consumed ~5.5GB peak
- **When** we run `pytest tests/test_commands.py` (largest file, ~87 tests)
- **Then** peak memory stays under 2GB

### Edge Cases
- Classes with cross-module dependencies (e.g., `TestCoverage80Sprint` tests multiple modules) → place in `test_app.py` as integration tests
- `TestBlockedCommandPath` and `TestBlockedCommands` reference deployer but are command-classification tests → placed in `test_deployer_main.py` because they test the blocked→deployer interaction
- `TestMCPListPending` uses callback logic but goes through MCP entry point → placed in `test_mcp_execute.py`
- `TestCallbackTrust` bridges callbacks and trust → placed in `test_trust.py`

### Acceptance Criteria
- [ ] `tests/conftest.py` created with all shared fixtures
- [ ] 14 new test files created
- [ ] Zero test methods lost (verified by count)
- [ ] `pytest tests/ -q` passes 100%
- [ ] `test_bouncer.py` deleted only after full verification
- [ ] Each file has correct imports and is independently runnable

---

## Story 2: Fill notifications.py Test Coverage (59% → 80%+)

**Task ID:** `bouncer-test-001`

### Background

`src/notifications.py` (569 lines, 15 functions) has only 59% coverage. The existing tests only cover `generate_display_summary` and related display helpers. Core notification-sending functions are untested.

### Functions Needing Tests

| Function | Lines | Current Coverage | Priority |
|---|---|---|---|
| `send_approval_request` | L33-153 | Partial (via integration) | High |
| `send_account_approval_request` | L155-189 | Low | Medium |
| `send_trust_auto_approve_notification` | L191-233 | Low | Medium |
| `send_grant_request_notification` | L235-325 | Low | High |
| `send_grant_execute_notification` | L327-371 | Low | Medium |
| `send_grant_complete_notification` | L373-388 | Low | Low |
| `send_blocked_notification` | L390-414 | Low | Medium |
| `send_trust_upload_notification` | L416-451 | Low | Medium |
| `send_batch_upload_notification` | L453-511 | Low | High |
| `send_presigned_notification` | L513-541 | Low | Medium |
| `send_presigned_batch_notification` | L543-569 | Low | Medium |
| `_escape_markdown` | L16-18 | Unknown | Low |
| `_send_message` / `_send_message_silent` | L20-31 | Unknown | Low |

### Acceptance Scenarios

**Scenario 1: Approval request notification formatting**
- **Given** a command `"aws s3 ls"` with reason `"list buckets"` and source `"Bot"`
- **When** `send_approval_request()` is called
- **Then** `_send_message()` is called with:
  - Markdown-formatted text containing command, reason, source
  - Inline keyboard with approve/deny buttons
  - Correct callback data format: `cmd_approve_{request_id}` / `cmd_deny_{request_id}`

**Scenario 2: Grant notification with multiple commands**
- **Given** a grant request with 5 commands, account `992382394211`, TTL 30 min
- **When** `send_grant_request_notification()` is called
- **Then** message includes all commands listed, account info, TTL display, approve/deny keyboard

**Scenario 3: Batch upload notification with file details**
- **Given** a batch upload with 3 files (total 150KB), account `992382394211`
- **When** `send_batch_upload_notification()` is called
- **Then** message shows file count, total size, target bucket, approve/deny keyboard

**Scenario 4: Cross-account notifications include account context**
- **Given** any notification function called with `account_id != DEFAULT_ACCOUNT_ID`
- **When** the notification is sent
- **Then** the message text includes the account name/ID prominently

**Scenario 5: Telegram API error handling**
- **Given** `_send_message()` raises `requests.exceptions.RequestException`
- **When** any notification function calls it
- **Then** the exception is caught and logged (not propagated to caller)

### Edge Cases
- `_escape_markdown` with special characters: `_`, `*`, `[`, `]`, `(`, `)`, `~`, `` ` ``, `>`, `#`, `+`, `-`, `=`, `|`, `{`, `}`, `.`, `!`
- `send_approval_request` with very long command (>4096 chars, Telegram limit) → truncation
- `send_grant_request_notification` with `allow_repeat=True` vs `False` → different display
- `context` parameter (optional) in various functions → included when present, omitted when None
- Notification with `source` containing markdown special chars → properly escaped

### Acceptance Criteria
- [ ] 25+ new test methods in `tests/test_notifications_main.py`
- [ ] Coverage: notifications.py 59% → 80%+
- [ ] All notification functions have at least 1 happy-path test
- [ ] Error/edge cases for top 5 functions

---

## Story 3: Fill mcp_execute.py Test Coverage (72% → 80%+, L882-1020 Gap)

**Task ID:** `bouncer-test-002`

### Background

`src/mcp_execute.py` (1,020 lines) has 72% coverage with a notable gap at lines 882-1020. This region contains three MCP tool handlers for the Grant system:
- `mcp_tool_request_grant` (L882-960) — Create grant request
- `mcp_tool_grant_status` (L962-993) — Query grant status
- `mcp_tool_revoke_grant` (L995-1020) — Revoke grant

### Acceptance Scenarios

**Scenario 1: Grant request — happy path**
- **Given** valid `commands`, `reason`, `source` arguments
- **When** `mcp_tool_request_grant(req_id, arguments)` is called
- **Then** returns `pending_approval` status with `grant_request_id` and `summary`

**Scenario 2: Grant request — missing required params**
- **Given** arguments missing `commands` / `reason` / `source`
- **When** `mcp_tool_request_grant()` is called
- **Then** returns MCP error -32602 with descriptive message

**Scenario 3: Grant request — invalid account**
- **Given** `account="999999999999"` (non-existent)
- **When** `mcp_tool_request_grant()` is called
- **Then** returns error result with "帳號 999999999999 未配置"

**Scenario 4: Grant request — notification failure is non-fatal**
- **Given** `send_grant_request_notification` raises Exception
- **When** `mcp_tool_request_grant()` is called
- **Then** still returns success (notification failure is caught and logged)

**Scenario 5: Grant status — happy path**
- **Given** a valid `grant_id` and matching `source`
- **When** `mcp_tool_grant_status()` is called
- **Then** returns grant status JSON

**Scenario 6: Grant status — not found**
- **Given** `grant_id="nonexistent"` 
- **When** `mcp_tool_grant_status()` is called
- **Then** returns error "Grant not found or source mismatch"

**Scenario 7: Revoke grant — happy path**
- **Given** a valid `grant_id`
- **When** `mcp_tool_revoke_grant()` is called
- **Then** returns `{success: true, message: "Grant 已撤銷"}`

**Scenario 8: Revoke grant — failure**
- **Given** `grant_id` that cannot be revoked
- **When** `mcp_tool_revoke_grant()` is called
- **Then** returns `{success: false, isError: true}`

### Edge Cases
- `mcp_tool_request_grant` with `ttl_minutes=None` (default) vs explicit value
- `mcp_tool_request_grant` with `allow_repeat=True`
- `mcp_tool_request_grant` with `account` param (cross-account grant)
- `mcp_tool_grant_status` with missing `source` → error -32602
- `mcp_tool_revoke_grant` with empty `grant_id` → error -32602
- Internal exception in `create_grant_request` → error -32603
- `ValueError` from grant module → error result (not -32603)

### Acceptance Criteria
- [ ] 15+ new test methods covering L882-1020
- [ ] Coverage: mcp_execute.py 72% → 80%+
- [ ] All three grant tool handlers have happy-path + error tests
- [ ] Edge cases for parameter validation

---

## Story 4: Fill callbacks.py / mcp_upload.py / deployer.py Test Coverage

**Task ID:** `bouncer-test-003`

### Background

Three modules have coverage gaps:
- `callbacks.py` (949 lines, 79%) — Complex callback dispatching with many branches
- `mcp_upload.py` (965 lines) — Upload pipeline with rate limiting, trust checks, batch logic
- `deployer.py` (674 lines) — Deploy lifecycle with locks, SFN integration, history

### 4a. callbacks.py (79% → 85%+)

**Key Untested Areas:**
- `handle_grant_approve` / `handle_grant_approve_safe` / `handle_grant_deny` — Grant callback flow
- `_auto_execute_pending_requests` (L837+) — Auto-execution during trust session
- Error branches in `handle_upload_batch_callback` (L636+) — Large batch edge cases
- `_send_status_update` edge cases — Missing fields in item

**Acceptance Scenarios:**

**Scenario 1: Grant approve callback**
- **Given** a pending grant request in DynamoDB
- **When** `handle_grant_approve(query, grant_id)` is called
- **Then** grant status updated to approved, Telegram callback answered

**Scenario 2: Grant deny callback**
- **Given** a pending grant request
- **When** `handle_grant_deny(query, grant_id)` is called
- **Then** grant status updated to denied, notification sent

**Scenario 3: Auto-execute pending during trust**
- **Given** 3 pending requests matching trust scope
- **When** `_auto_execute_pending_requests(trust_scope, account_id, ...)` is called
- **Then** all 3 requests auto-executed and status updated

### 4b. mcp_upload.py (coverage gap focus)

**Key Untested Areas:**
- `_check_upload_rate_limit` edge cases
- `_check_upload_trust` with expired/invalid trust sessions
- `_submit_upload_for_approval` with large file metadata
- `execute_upload` error recovery

### 4c. deployer.py (coverage gap focus)

**Key Untested Areas:**
- `acquire_lock` / `release_lock` concurrency edge cases
- `start_deploy` with SFN integration
- `cancel_deploy` when deploy is already complete
- `mcp_tool_deploy` parameter validation

### Edge Cases (All Three)
- callbacks.py: Already-processed callback (idempotency check)
- callbacks.py: Race condition — two approvals for same request
- mcp_upload.py: Upload with 0-byte file
- mcp_upload.py: Batch with > 20 files
- deployer.py: Lock acquisition when lock already held by another deploy
- deployer.py: Deploy history query with no history

### Acceptance Criteria
- [ ] callbacks.py: 79% → 85%+ coverage, 10+ new tests
- [ ] mcp_upload.py: 5+ new tests for untested paths
- [ ] deployer.py: 5+ new tests for untested paths
- [ ] All new tests in respective split files from Story 1

---

## Story 5: Fix Flaky Tests with freezegun

**Task ID:** `bouncer-test-004`

### Background

60 occurrences of bare `time.time()` in test data setup create race conditions. When test execution crosses a second boundary, time-dependent assertions can fail intermittently. Examples:
- TTL calculations: `'ttl': int(time.time()) + 300` — if `time.time()` is called at different moments during setup vs assertion
- Expiry checks: `'expires_at': int(time.time()) + 600` — assertion may see a different second
- Unique IDs using time: `'request_id': 'test-' + str(int(time.time()))` — collision risk

### Approach: freezegun

Use `freezegun.freeze_time` to pin time during test execution:

```python
from freezegun import freeze_time

@freeze_time("2025-01-15 12:00:00")
class TestTrustSession:
    def test_trust_active(self, app_module):
        # time.time() always returns 1736942400
        app_module.table.put_item(Item={
            'ttl': 1736942400 + 300,  # deterministic
        })
```

### Acceptance Scenarios

**Scenario 1: Install freezegun**
- **Given** `freezegun` is not in any requirements file
- **When** we add `freezegun>=1.2.0` to test requirements
- **Then** `pip install` succeeds and import works

**Scenario 2: Trust session tests are deterministic**
- **Given** `TestTrustSession`, `TestTrustSessionExpiry`, `TestTrustSessionLimits` use `@freeze_time`
- **When** tests run 100 times in a loop
- **Then** all pass every time (zero flakes)

**Scenario 3: Rate limit tests are deterministic**
- **Given** rate limit tests use frozen time
- **When** `source = 'repeat-source-fixed'` (no time.time() in ID)
- **Then** tests pass deterministically

**Scenario 4: Callback TTL tests are deterministic**
- **Given** callback handler tests freeze time
- **When** TTL values use frozen timestamps
- **Then** "expired" vs "active" assertions are always correct

**Scenario 5: Backward compatibility**
- **Given** `freezegun` is applied incrementally (not all at once)
- **When** only flaky tests get `@freeze_time`
- **Then** non-flaky tests continue to work unchanged

### Flaky Test Hotspots (by line reference in test_bouncer.py)

| Line Range | Class | Issue |
|---|---|---|
| 2291-2680 | TestTelegramCallbackHandlers | 11 uses of `time.time()` for TTL |
| 2683-3046 | TestDeployerModule | Lock timestamps, deploy history |
| 6264-6342 | TestTrustSessionLimits | `expires_at` calculations |
| 6543-6638 | TestTrustSessionExpiry | Expired vs active checks |
| 5158-5177 | TestRateLimitMore | Source with `time.time()` |
| 5215-5329 | TestCallbackHandlers | `created_at` + `ttl` combos |

### Edge Cases
- `freeze_time` must freeze both `time.time()` and `datetime.now()` — verify both
- `moto` compatibility: ensure `mock_aws` + `freeze_time` don't conflict
- `module`-scoped fixtures run before class-level `@freeze_time` — fixture setup must NOT depend on frozen time unless explicitly designed
- DynamoDB TTL uses epoch seconds — frozen time must produce valid epoch values

### Acceptance Criteria
- [ ] `freezegun>=1.2.0` added to test dependencies
- [ ] All 60 bare `time.time()` occurrences replaced with frozen alternatives
- [ ] Zero flaky test failures in 10 consecutive `pytest` runs
- [ ] No interaction issues between `freezegun`, `moto`, and `pytest` fixtures
