# Sprint 9-001: Plan — Execution Error 記錄到 DDB

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **`execute_command()`**（`commands.py:515`）：返回 `str`。成功/失敗判斷靠 `_is_failed_output()` 檢查 `❌` prefix 或 `(exit code: N)` 後綴。`CommandChainResult` 內有 `exit_code` 欄位。

2. **`log_decision()`**（`utils.py:283`）：統一 audit log，寫入 requests 表。目前沒有 `exit_code` / `error_output` 欄位。接受 `**kwargs` 所以新增欄位是向後兼容的。

3. **執行路徑**：
   - `_check_auto_approve()`（`mcp_execute.py:588`）：auto-approve → execute → log_decision
   - `_check_trust_session()`：trust → execute → log_decision
   - `_check_grant_session()`（`mcp_execute.py:522`）：grant → execute → notify
   - `callbacks.py` approve/approve_trust：human approve → execute → update DDB

4. **`_is_failed_output()`**（`commands.py:554`）：可從 output 推斷 exit_code（regex 抽取）

### 影響範圍

- `src/utils.py` — `log_decision()` 新增 optional params
- `src/mcp_execute.py` — 3 個 execute 路徑加入 error 記錄
- `src/callbacks.py` — human approve 路徑加入 error 記錄
- `tests/` — 補測試

## Implementation Phases

### Phase 1: 工具函數（utils.py）

1. 在 `log_decision()` 新增 `exit_code: Optional[int] = None` 和 `error_output: Optional[str] = None` 參數
2. 當 `exit_code is not None` 時，寫入 DDB item：
   - `exit_code` = Decimal(exit_code)
   - `error_output` = error_output[:2000]（截斷）
   - `executed_at` = now
3. 新增 helper `_extract_exit_code(output: str) -> int`：
   - 從 `(exit code: N)` regex 抽取
   - output 以 `❌` 開頭且無明確 code → return -1
   - 其他 → return 0

### Phase 2: MCP Execute 路徑（mcp_execute.py）

1. **`_check_auto_approve()`**：
   - 在 `execute_command()` 後，呼叫 `_extract_exit_code(result)`
   - 若 exit_code != 0，傳入 `log_decision(..., exit_code=ec, error_output=result[:2000])`
   - 在 `response_data` 中加入 `exit_code`

2. **`_check_trust_session()`**：同上

3. **`_check_grant_session()`**：同上

### Phase 3: Callback 路徑（callbacks.py）

1. 在 human approve 執行後，從 result 抽取 exit_code
2. 更新 DDB item（`update_item` 現有邏輯）加入 `exit_code`、`error_output`

### Phase 4: 測試

1. 單元測試 `_extract_exit_code()` 各種 output pattern
2. 測試 `log_decision()` 帶 exit_code 時 DDB item 正確
3. Integration test: 模擬失敗命令 → 確認 DDB + MCP response 都有 exit_code
