# Implementation Plan: Emoji Based on Exit Code + command_status to DDB

## Technical Context
- 影響檔案：
  - `src/callbacks.py` — `_format_approval_response`, `_execute_and_store_result`, trust callback DDB update, `update_message` call after execution
  - `src/app.py` — auto_approve path (`cmd_status` computation + DDB update)
  - `src/mcp_execute.py` — auto_approve and trust paths (already compute `cmd_status` but don't store to DDB)
- 影響測試：
  - `tests/test_regression_auto_approved_failure_emoji.py` (already exists — extend)
  - `tests/test_callbacks_main.py` (manual approve path)
  - 新增：`tests/test_sprint31_001_emoji_ddb.py`
- 技術風險：
  - Trust callback DDB update expression is inline — must extend UpdateExpression carefully
  - `_execute_and_store_result` is shared by `approve` and `approve_trust` — change affects both
  - `app.py` auto-approve DDB update: check if `update_item` or `put_item` is used (expression differs)

## Constitution Check
- 安全影響：無。只是新增屬性到已存在的 DDB 記錄，不改變授權邏輯
- 成本影響：微量（每次 command 執行多一個 DDB attribute write；在同一 update_item 內，無額外 write unit）
- 架構影響：低。DDB schema additive change。無需 migration。

## Implementation Phases

### Phase 1: `_format_approval_response` emoji fix (callbacks.py)
- 修改 `title` 計算：若 `_is_execute_failed(result)` → title 改為 `❌ *已批准但執行失敗*` 或 `❌ *已批准但執行失敗* + 🔓 *信任 10 分鐘*`
- 修改 `update_message(message_id, "✅ *已執行*...")` → 依 exit code 選 ✅/❌ emoji
- 傳入 `result` 參數到 `_format_approval_response`（已有）

### Phase 2: `_execute_and_store_result` — add command_status to DDB (callbacks.py)
- 在 `update_expr` 加入 `command_status = :cs`
- `expr_values[':cs'] = cmd_status` where `cmd_status = 'failed' if _is_execute_failed(result) else 'success'`
- 同步修改 trust callback DDB inline update_item (line ~1751)

### Phase 3: Other paths — auto_approve + grant (app.py, mcp_execute.py)
- `app.py` auto_approve: 修正 emoji 判斷（line 711 用 `result.startswith('❌')` → 改用 `_is_execute_failed`）並 store `command_status` to DDB
- `mcp_execute.py` auto_approve path + trust path + grant path: add `command_status` to DDB update

### Phase 4: Tests
- 新增 `tests/test_sprint31_001_emoji_ddb.py`：
  - test_manual_approve_success_emoji
  - test_manual_approve_failure_emoji (exit code 1)
  - test_manual_approve_usage_error_emoji (exit code 2 via "usage:" prefix)
  - test_command_status_stored_to_ddb_on_success
  - test_command_status_stored_to_ddb_on_failure
