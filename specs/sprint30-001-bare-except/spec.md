# Sprint 30-001: Bare Except Batch 3

## Problem

### Security Risk

Bouncer 的 `src/` 目錄中有 **173 個** `except Exception` / `except:` catch-all（不含已標 `# noqa` 的 11 個），分布在 **27 個檔案**中。

Bare except 會掩蓋關鍵安全故障：

1. **STS AssumeRole 失敗被吞** — `callbacks.py:1286` 的 `except Exception` 吞掉 AssumeRole 失敗，返回泛用錯誤訊息而非 `ClientError` 細節。攻擊者可能利用被隱藏的 credential 錯誤來掩蓋未授權存取嘗試。

2. **DynamoDB 操作失敗被隱藏** — `trust.py:207,334` 對 DDB get/update 用 `except Exception`，trust session 查詢失敗時回傳 `None`/`0` 而非區分「DDB 不可用」vs「session 不存在」。在 DDB 故障時可能導致 trust session 驗證 fail-open。

3. **Telegram 通知失敗被靜默** — `mcp_execute.py:839,843` 中 Telegram 通知失敗後的 DDB 清理也是 `except Exception`，如果清理也失敗會留下 orphan pending record（永遠不會被審批也不會過期）。

4. **JSON parse 失敗攻擊面** — `app.py:472,642,816` 的 JSON parse 用 `except Exception` 而非 `except (json.JSONDecodeError, TypeError, ValueError)`，可能吞掉意外的記憶體錯誤或 encoding 錯誤。

5. **Grant 預檢 fail-open** — `grant.py:381` 的 risk scoring 錯誤被 `except Exception` 捕獲後設 `risk_score=0`（fail-open），但外層 `grant.py:386` 是 fail-closed。兩層不一致的 fail 策略在程式碼審查中容易被誤解。

### Scale

| 檔案類別 | 檔案數 | except 數 |
|----------|--------|-----------|
| 安全核心（mcp_execute, trust, app） | 3 | 37 |
| 業務邏輯（callbacks, grant, notifications） | 3 | 40 |
| 工具模組（deployer, mcp_deploy_frontend, mcp_upload） | 3 | 42 |
| 輔助模組（其餘 18 個檔案） | 18 | 54 |
| **合計** | **27** | **173** |

### Prior Work

- Issue #84 原估 33 個，Sprint 27-28 修了部分
- 11 個已有 `# noqa` 標注（經審核為合理的 catch-all）
- 本 Sprint 處理剩餘 173 個

---

## User Stories

### US-1: Batch A — 安全核心模組類型化例外

> As a **security auditor**, I want `mcp_execute.py`, `trust.py`, and `app.py` to catch specific exception types instead of bare `except Exception`, so that security-critical failures (STS, DDB, Telegram) are properly logged with context and never silently swallowed.

### US-2: Batch B — 業務邏輯模組類型化例外

> As a **system operator**, I want `callbacks.py`, `grant.py`, and `notifications.py` to use typed exceptions, so that approval workflow failures surface meaningful error details instead of generic error messages.

### US-3: Batch C — 工具模組類型化例外

> As a **developer**, I want `deployer.py`, `mcp_deploy_frontend.py`, and `mcp_upload.py` to catch specific exceptions, so that deploy/upload failures can be diagnosed without checking broad exception logs.

### US-4: Batch D — 輔助模組類型化例外

> As a **maintainer**, I want all remaining modules (18 files, 54 occurrences) to use typed exceptions, so that the entire codebase follows a consistent exception handling pattern.

---

## Acceptance Scenarios

### Scenario 1: AWS SDK Error Specificity

```
Given mcp_execute.py calls DynamoDB put_item at line 160
When DynamoDB returns ProvisionedThroughputExceededException
Then the except block catches ClientError (not bare Exception)
  And logger.error includes the DDB error code and table name
  And the shadow log failure does not propagate to the caller
```

### Scenario 2: Trust Session DDB Fail-Close

```
Given trust.py check_trust_session at line 207 queries DDB
When DynamoDB is unreachable (endpoint timeout)
Then the except block catches ClientError
  And the function returns None (existing fail-close behavior preserved)
  And the log includes "ClientError" with the DDB error code
```

### Scenario 3: Telegram Notification + Orphan Cleanup

```
Given mcp_execute.py at line 839 catches Telegram notification failure
When Telegram returns HTTP 502
Then the except block catches (OSError, TimeoutError, ConnectionError, urllib.error.URLError)
  And the inner cleanup (line 843) catches ClientError for DDB delete
  And if both fail, both error details are logged separately
```

### Scenario 4: JSON Parse at API Entry Points

```
Given app.py handle_mcp_request at line 472 parses request body
When body contains invalid JSON
Then the except block catches (json.JSONDecodeError, ValueError)
  And returns mcp_error with code -32700
  And does NOT catch MemoryError, SystemExit, or KeyboardInterrupt
```

### Scenario 5: Grant Risk Scoring Fail Strategy

```
Given grant.py _precheck_command at line 381 calls risk scorer
When risk scorer raises an unexpected error
Then the inner except catches the specific exception type
  And the fail strategy (fail-open for inner scoring, fail-closed for outer precheck) is documented with inline comments
```

### Scenario 6: Top-Level Lambda Handler Catch-All

```
Given app.py has top-level request handlers (handle_mcp_request, handle_telegram_webhook)
When an unexpected exception reaches the top level
Then except Exception is retained (with # noqa: BLE001)
  And logger.exception() is called (full traceback)
  And an appropriate error response is returned
  And the exception is NOT re-raised (Lambda must return HTTP response)
```

### Scenario 7: No Behavioral Change

```
Given all 173 except blocks are modified
When the full test suite runs (987 tests)
Then all existing tests pass without modification to test logic
  And no public API signatures change
  And return values for error paths remain identical
```

---

## Edge Cases

1. **Chained exceptions** — Some `except Exception` blocks re-raise (`raise`). These must preserve the original exception chain via `raise ... from e` or plain `raise`.

2. **Bare `except:` (no Exception)** — A few sites use `except:` which also catches `SystemExit`, `KeyboardInterrupt`. These must become `except Exception` at minimum, then further narrowed.

3. **Nested try/except** — `mcp_execute.py:839-843`, `mcp_upload.py:457-461` have outer Telegram catch + inner DDB cleanup catch. Both need separate typed exceptions.

4. **ClientError already caught upstream** — `deployer.py:254,267,301` already catch `ClientError` before a fallback `except Exception`. The fallback should be narrowed to non-ClientError cases or removed if unreachable.

5. **`except Exception:` with `logger.exception()`** — `deployer.py:257,270,304,341,366,424` use `except Exception:` followed by `logger.exception()`. These already log properly but still catch too broadly — should narrow to the expected exception types.

6. **Import-time exceptions** — `mcp_execute.py:360` already catches `ImportError` separately; the following `except Exception` at 362 is for runtime errors only.

7. **`print()` instead of `logger`** — `app.py:792` uses `print()` in an except block instead of `logger`. Must fix to use `logger.error()`.

---

## Requirements

### Functional

1. Replace each `except Exception` / `except:` with the most specific exception type(s) based on the operation being wrapped:
   - AWS SDK calls → `except ClientError`
   - DynamoDB calls → `except ClientError`
   - Telegram API (urllib-based) → `except (OSError, TimeoutError, ConnectionError, urllib.error.URLError)`
   - JSON parsing → `except (json.JSONDecodeError, ValueError)`
   - Base64 decoding → `except (binascii.Error, ValueError)`
   - Regex operations → `except re.error`
   - botocore session → `except (botocore.exceptions.BotoCoreError, botocore.exceptions.NoRegionError)`
   - General I/O → `except OSError`
   - Network operations → `except (OSError, TimeoutError, ConnectionError)`

2. Top-level Lambda handlers retain `except Exception` with:
   - `logger.exception()` for full traceback
   - `# noqa: BLE001 — top-level Lambda handler, must return HTTP response`
   - Return appropriate error response (never crash Lambda)

3. Legitimate catch-all sites (identified during analysis) get `# noqa: BLE001` with comment explaining why.

4. Every modified except block must have at minimum `logger.error()` or `logger.warning()` (no silent pass).

5. `app.py:792` — replace `print()` with `logger.error()`.

### Non-functional

1. **No public API changes** — All function signatures, return types, and error response formats remain identical.
2. **No behavioral changes** — Error paths return the same values (None, False, [], {}, error dicts) as before.
3. **No new dependencies** — All exception types used must already be imported or available in stdlib.
4. **Backward-compatible imports** — New import lines for exception types (e.g., `from botocore.exceptions import ClientError`) are added only where not already present.
5. **Test suite passes** — All 987 existing tests pass. Some test mocks use `side_effect=Exception(...)` — these may need updating to the new specific type, but the test assertions must remain equivalent.
6. **No template.yaml changes** — Pure code change, no infrastructure impact.
