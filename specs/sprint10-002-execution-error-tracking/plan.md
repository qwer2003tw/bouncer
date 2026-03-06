# Sprint 10-002: Plan — execution error tracking 不寫 DDB

> Generated: 2026-03-03

---

## Technical Context

### 現狀分析

1. **`_is_failed_output()`**（`commands.py:548`）：正確判斷失敗（`❌` prefix / `(exit code: N)` regex / `usage:` prefix），但只回傳 `bool`。

2. **`record_execution_error()`**（`utils.py:325`）：寫入 DDB — status=executed_error, exit_code, error_output, executed_at。功能正確。

3. **3 個執行路徑**（`mcp_execute.py`）：
   - `_check_grant_session()` L532: `is_failed = result.startswith('❌')` → `record_execution_error(table, grant_req_id, exit_code=-1, error_output=result)`
   - `_check_auto_approve()` L607: `is_failed = result.startswith('\u274c')` → `record_execution_error(table, request_id, exit_code=-1, error_output=result)`
   - `_check_trust_session()` L718: `is_failed = result.startswith('❌')` → `record_execution_error(table, request_id, exit_code=-1, error_output=result)`

4. **`commands.py` 的 `__all__`**：L28 已包含 `_is_failed_output`。

### 修復策略

**方案 A（推薦）**：新增 `extract_exit_code(output) -> int` 到 `commands.py`，與 `_is_failed_output()` 邏輯一致但回傳 int。

**方案 B**：修改 `_is_failed_output()` 回傳 `Tuple[bool, int]`。但這會 break 所有現有呼叫點。

選擇方案 A — 最小改動、向後兼容。

### 影響範圍

- `src/commands.py` — 新增 `extract_exit_code()` helper
- `src/mcp_execute.py` — 3 個路徑的 `is_failed` 改用 `_is_failed_output()`，`exit_code` 改用 `extract_exit_code()`
- `tests/` — 補測試

## Implementation Phases

### Phase 1: 新增 extract_exit_code()（commands.py）

```python
def extract_exit_code(output: str) -> int:
    """從命令輸出解析 exit code。"""
    m = re.search(r'\(exit code:\s*(\d+)\)', output)
    if m:
        return int(m.group(1))
    if output.startswith('❌'):
        return -1
    if output.strip().startswith('usage:'):
        return 1
    return 0
```

加入 `__all__`。

### Phase 2: 修改 3 個執行路徑（mcp_execute.py）

每個路徑改成：
```python
from commands import _is_failed_output, extract_exit_code

is_failed = _is_failed_output(result)
# ...
if is_failed:
    ec = extract_exit_code(result)
    record_execution_error(table, request_id, exit_code=ec, error_output=result)
# ...
if is_failed:
    response_data['exit_code'] = extract_exit_code(result)
```

### Phase 3: 測試

1. Unit test `extract_exit_code()` 各種 pattern：
   - `(exit code: 255)` → 255
   - `❌ blocked` → -1
   - `usage: aws` → 1
   - 正常輸出 → 0
2. Integration test: mock `execute_command` 回傳 AWS CLI 失敗輸出 → 確認 `record_execution_error()` 被呼叫
3. Regression test: 確認成功命令不呼叫 `record_execution_error()`
