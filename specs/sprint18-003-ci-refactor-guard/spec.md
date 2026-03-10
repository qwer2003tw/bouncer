# Sprint 18-003: CI guard 強化 — module refactor mock 一致性檢查

> Priority: P1
> TCS: 3
> Generated: 2026-03-08

---

## Problem Statement

CI 目前有一個 `Check entities migration mock consistency` step，用 grep 檢查 `tests/` 中是否有過期的 `_send_message` mock patch（應已遷移為 `send_message_with_entities`）。

現有 CI guard 的問題：

1. **排除規則太寬鬆**：排除了 `test_notifications_main`、`test_app`、`conftest`，這些檔案中的 stale mock 不會被抓到
2. **只檢查 `_send_message`**：不檢查 `_escape_markdown` 的殘留 mock
3. **不檢查 `_send_message_silent`**：另一個 legacy helper
4. **grep 模式可能漏網**：只看 `patch.*'_send_message'`，沒有覆蓋 `patch.object` 或其他 mock pattern

### 現有 CI step（ci.yaml）

```yaml
- name: Check entities migration mock consistency
  run: |
    STALE=$(grep -rn "patch.*'_send_message'" tests/ 2>/dev/null |
            grep -v "_mock_entities_send\|autouse\|conftest\|#\|send_telegram_message_silent" |
            grep -v "test_notifications_main\|test_app" || true)
    if [ -n "$STALE" ]; then
      echo "❌ Stale _send_message mock patches found"
      echo "$STALE"
      exit 1
    fi
```

## Scope

### 變更 1: 擴展 stale mock 檢查

**檔案：** `.github/workflows/ci.yaml`

擴展 grep pattern 以檢查所有三個 legacy helper 的殘留 mock：

```yaml
- name: Check entities migration mock consistency
  run: |
    # Check for stale _send_message mocks
    STALE_SEND=$(grep -rn "patch.*['\"].*_send_message['\"]" tests/ 2>/dev/null |
                 grep -v "_mock_entities_send\|autouse\|#" || true)

    # Check for stale _escape_markdown mocks (should be removed after Phase 4)
    STALE_ESCAPE=$(grep -rn "patch.*['\"].*_escape_markdown['\"]" tests/ 2>/dev/null |
                   grep -v "#" || true)

    # Check for stale _send_message_silent mocks
    STALE_SILENT=$(grep -rn "patch.*['\"].*_send_message_silent['\"]" tests/ 2>/dev/null |
                   grep -v "#" || true)

    ERRORS=""
    if [ -n "$STALE_SEND" ]; then
      ERRORS="${ERRORS}\n❌ Stale _send_message mock patches:\n${STALE_SEND}"
    fi
    if [ -n "$STALE_ESCAPE" ]; then
      ERRORS="${ERRORS}\n❌ Stale _escape_markdown mock patches:\n${STALE_ESCAPE}"
    fi
    if [ -n "$STALE_SILENT" ]; then
      ERRORS="${ERRORS}\n❌ Stale _send_message_silent mock patches:\n${STALE_SILENT}"
    fi

    if [ -n "$ERRORS" ]; then
      echo -e "$ERRORS"
      exit 1
    fi
    echo "✅ Entities mock patterns consistent"
```

### 變更 2: 新增 import guard

**檔案：** `.github/workflows/ci.yaml`

在同一個 step 或新 step 中，檢查 `src/notifications.py` 不再 import 或定義 legacy helpers：

```bash
# After Phase 4 completes, notifications.py should not contain legacy helpers
if grep -q "def _escape_markdown\|def _send_message\b" src/notifications.py; then
  echo "⚠️ Warning: Legacy helpers still in notifications.py (expected after sprint18-001)"
fi
```

⚠️ 此檢查在 sprint18-001 完成前是 warning（不 fail CI），完成後改為 error。

### 變更 3: 移除過度排除

移除 `grep -v "test_notifications_main\|test_app"` — 這些檔案也應該遷移到 entities mock。如果目前有 stale mock，需要記錄為 known issue 或在 001 中一併修。

## Out of Scope

- 測試程式碼本身的重構（只改 CI 檢查）
- 新增 Python-based CI script（保持 shell grep 簡單）

## Test Plan

| # | 測試 | 預期 |
|---|------|------|
| T1 | 在 test file 加入 `patch('..._send_message')` → push | CI fail |
| T2 | 在 test file 加入 `patch('..._escape_markdown')` → push | CI fail |
| T3 | 正常 codebase（無 stale mock） | CI pass |
| T4 | `#` 註解中含 `_send_message` | CI pass（被 grep -v # 排除） |

### 驗證方式

本地模擬：
```bash
# 執行 CI step 的 shell script
bash -c '...(copy CI step content)...'
```

## Acceptance Criteria

- [ ] CI 檢查覆蓋 `_send_message`、`_escape_markdown`、`_send_message_silent` 三個 legacy helper
- [ ] 移除過度排除（`test_notifications_main`、`test_app`）
- [ ] 加入 legacy helper 定義存在的 warning/error check
- [ ] 現有 CI 通過（無 stale mock）
