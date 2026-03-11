# Tasks

## TCS Calculation Reference

| Dimension | Criteria | Score |
|-----------|----------|-------|
| D1 Files | <2 files = 1, 2-4 = 3, >4 = 5 |
| D2 Cross-module | None = 0, import changes = 2, interface changes = 4 |
| D3 Testing | None = 0, update tests = 2, redesign tests = 4 |
| D4 Infrastructure | None = 0, template.yaml = 4 |
| D5 External | None = 0, known AWS = 2, new service = 4 |

---

## Task List

### [T001] [P] [Batch A] mcp_execute.py + trust.py bare except → typed exceptions | TCS=7 (Simple)

**Scope:** 18 except sites across 2 files (mcp_execute.py: 10, trust.py: 8)

**TCS Breakdown:**
- D1 Files: 2 files = 3
- D2 Cross-module: import `ClientError`, `urllib.error` = 2
- D3 Testing: update mock side_effects = 2
- D4 Infrastructure: 0
- D5 External: 0
- **Total: 7 (Simple)**

**Key changes:**
- mcp_execute.py: 7 sites → `ClientError` or Telegram urllib errors; 3 MCP entry points → `# noqa: BLE001`
- trust.py: 7 sites → `ClientError`; 1 metrics site → `# noqa: BLE001`
- Add `from botocore.exceptions import ClientError` and `import urllib.error` where missing
- Update test mocks in `test_mcp_execute*.py`, `test_trust.py`

---

### [T002] [P] [Batch A] app.py top-level handlers bare except → typed exceptions | TCS=5 (Simple)

**Scope:** 19 except sites in 1 file

**TCS Breakdown:**
- D1 Files: 1 file = 1
- D2 Cross-module: import `ClientError`, `urllib.error` = 2
- D3 Testing: update mock side_effects = 2
- D4 Infrastructure: 0
- D5 External: 0
- **Total: 5 (Simple)**

**Key changes:**
- 8 DDB sites → `except ClientError`
- 5 Telegram sites → `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)`
- 3 JSON parse sites → `except (json.JSONDecodeError, ValueError)`
- 1 timestamp parse → `except (ValueError, TypeError)`
- 1 mixed Telegram+DDB → `except (…, ClientError)`
- 1 `print()` → `logger.error()` fix (line 791)
- Update test mocks in `test_app.py`, `test_regression_cleanup_message_id.py`

---

### [T003] [P] [Batch B] callbacks.py + grant.py + notifications.py bare except → typed exceptions | TCS=9 (Simple)

**Scope:** 40 except sites across 3 files (callbacks: 13, grant: 13, notifications: 14)

**TCS Breakdown:**
- D1 Files: 3 files = 3
- D2 Cross-module: import additions = 2
- D3 Testing: update mock side_effects in grant/callbacks tests = 2
- D4 Infrastructure: 0
- D5 External: known AWS (ClientError) = 2
- **Total: 9 (Simple)**

**Key changes:**
- callbacks.py: 13 sites → mix of `ClientError` (DDB/STS/S3) and Telegram urllib errors
- grant.py: 13 sites → `ClientError` (DDB), `re.error`, `ValueError`
- notifications.py: 14 sites → mostly Telegram urllib errors, a few `ClientError` for DDB
- Add `import re`, `import urllib.error`, `from botocore.exceptions import ClientError` where missing
- Update test mocks in `test_callbacks_main.py`, `test_grant.py`, `test_notifications_main.py`

---

### [T004] [P] [Batch C] deployer.py + mcp_deploy_frontend.py + mcp_upload.py bare except → typed exceptions | TCS=9 (Simple)

**Scope:** 42 except sites across 3 files (deployer: 20, mcp_deploy_frontend: 10, mcp_upload: 12)

**TCS Breakdown:**
- D1 Files: 3 files = 3
- D2 Cross-module: import additions = 2
- D3 Testing: update mock side_effects = 2
- D4 Infrastructure: 0
- D5 External: known AWS (ClientError, SFN, CFN) = 2
- **Total: 9 (Simple)**

**Key changes:**
- deployer.py: 20 sites — **special attention** to 6 duplicate `except Exception:` that follow existing `except ClientError:` blocks (lines 257, 270, 304, 341, 366, 424). Strategy: merge into single `except ClientError:` or determine if the `except Exception:` catches non-ClientError from other code paths.
- mcp_deploy_frontend.py: 10 sites → `ClientError`, `binascii.Error`, Telegram urllib errors
- mcp_upload.py: 12 sites → `ClientError`, `binascii.Error`, Telegram urllib errors, nested cleanup
- Add `import binascii`, `import urllib.error` where missing
- Update test mocks in `test_deployer_main.py`, `test_ddb_400kb_fix.py`, `test_mcp_upload_sprint9_002.py`

---

### [T005] [P] [Batch D-1] telegram.py + telegram_commands.py + accounts.py + paging.py + commands.py bare except → typed exceptions | TCS=7 (Simple)

**Scope:** 19 except sites across 5 files

**TCS Breakdown:**
- D1 Files: 5 files = 5
- D2 Cross-module: import additions = 2
- D3 Testing: minimal mock updates = 0
- D4 Infrastructure: 0
- D5 External: 0
- **Total: 7 (Simple)**

**Key changes:**
- telegram.py (5): all → Telegram urllib errors
- telegram_commands.py (4): 3 DDB → `ClientError`, 1 datetime → `(ValueError, TypeError)`
- accounts.py (4): 1 Telegram, 3 DDB → `ClientError`
- paging.py (2): DDB + Telegram mixed
- commands.py (3): 1 shlex → `ValueError`, 1 STS → `ClientError`, 1 → `# noqa: BLE001`

---

### [T006] [P] [Batch D-2] risk_scorer.py + smart_approval.py + sequence_analyzer.py + template_scanner.py + compliance_checker.py + rate_limit.py bare except → typed exceptions | TCS=7 (Simple)

**Scope:** 15 except sites across 6 files

**TCS Breakdown:**
- D1 Files: 6 files = 5
- D2 Cross-module: import additions = 2
- D3 Testing: minimal = 0
- D4 Infrastructure: 0
- D5 External: 0
- **Total: 7 (Simple)**

**Key changes:**
- risk_scorer.py (5): JSON/regex/parse errors; 1 → `# noqa: BLE001` (fail-closed evaluator)
- smart_approval.py (2): 1 inner analysis error, 1 → `# noqa: BLE001` (fail-closed approval)
- sequence_analyzer.py (4): DDB operations → `ClientError`
- template_scanner.py (2): JSON parse, 1 → `# noqa: BLE001`
- compliance_checker.py (1): regex → `re.error`
- rate_limit.py (1): DDB → `ClientError` (fail-close preserved via `raise RateLimitExceeded`)

---

### [T007] [P] [Batch D-3] scheduler_service.py + mcp_presigned.py + mcp_admin.py + mcp_history.py + mcp_confirm.py + help_command.py + utils.py bare except → typed exceptions | TCS=7 (Simple)

**Scope:** 20 except sites across 7 files

**TCS Breakdown:**
- D1 Files: 7 files = 5
- D2 Cross-module: import additions = 2
- D3 Testing: minimal = 0
- D4 Infrastructure: 0
- D5 External: 0
- **Total: 7 (Simple)**

**Key changes:**
- scheduler_service.py (4): EventBridge Scheduler → `ClientError`
- mcp_presigned.py (4): S3 + Telegram errors
- mcp_admin.py (3): MCP entry points → `# noqa: BLE001`
- mcp_history.py (3): DDB + 1 MCP entry point → `# noqa: BLE001`
- mcp_confirm.py (2): S3 + DDB → `ClientError`
- help_command.py (3): botocore introspection → `# noqa: BLE001`
- utils.py (2): DDB audit log → `ClientError`

---

## 🔢 TCS Summary

| Task | TCS | Complexity |
|------|-----|------------|
| T001 | 7 | Simple |
| T002 | 5 | Simple |
| T003 | 9 | Simple |
| T004 | 9 | Simple |
| T005 | 7 | Simple |
| T006 | 7 | Simple |
| T007 | 7 | Simple |

**TCS 摘要：Simple ×7 / Medium ×0 / Complex ×0**

**Total except sites: 173**
- Narrowed to typed exceptions: ~159
- Retained as `# noqa: BLE001`: ~14

---

## Execution Order

1. **T001 + T002** (Batch A) → 安全核心，最高優先
2. **T003** (Batch B) → 業務邏輯
3. **T004** (Batch C) → 工具模組
4. **T005 + T006 + T007** (Batch D) → 輔助模組，可並行

Each task is independently deployable but should be merged in order for clean git history.

## Notes

- All tasks are Simple (TCS < 13) — no further splitting required
- No template.yaml changes — pure code modifications
- No new dependencies — all exception types from stdlib or already-imported botocore
- deployer.py (T004) requires extra care due to duplicate `except ClientError` + `except Exception` patterns — verify reachability before removing
- Test mock updates are mechanical: change `side_effect=Exception(...)` to `side_effect=ClientError(...)` or `side_effect=OSError(...)` as appropriate
