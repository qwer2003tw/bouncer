# Sprint 10-002: bug: execution error tracking 不寫 DDB

> GitHub Issue: #48
> Priority: P0
> Generated: 2026-03-03

---

## Bug Name

Execution Error Tracking 完全無效 — Sprint 9 新增的 `record_execution_error()` 只在 output 以 `❌` 開頭時觸發，但 AWS CLI 失敗的實際輸出不以 `❌` 開頭，導致 DDB 0 筆記錄有 `exit_code` 欄位。

## Root Cause Analysis

### 問題代碼（3 個執行路徑都有同樣的 bug）

`mcp_execute.py` 的 3 個路徑：
- `_check_grant_session()`（L532）：`is_failed = result.startswith('❌')`
- `_check_auto_approve()`（L607）：`is_failed = result.startswith('\u274c')`
- `_check_trust_session()`（L718）：`is_failed = result.startswith('❌')`

### 為什麼 `❌` 判斷錯誤

`❌` prefix 只出現在 Bouncer 自己格式化的錯誤（blocked、compliance violation 等），**不出現在 AWS CLI 的實際執行失敗**。

AWS CLI 失敗的實際輸出範例：
```
An error occurred (AccessDenied) when calling the ListObjectsV2 operation: Access Denied

(exit code: 255)
```

已有的 `commands.py:_is_failed_output()` 函數**正確處理**了所有 case：
- `❌` prefix
- `(exit code: N)` regex（N != 0）
- `usage:` prefix

但 `mcp_execute.py` 的 3 個路徑**沒有使用** `_is_failed_output()`，而是直接用 `startswith('❌')` 判斷。

### 驗證

DDB scan 2267 筆 request，0 筆有 `exit_code` 欄位。Sprint 9-001 的 execution error analytics 功能完全無效。

## User Stories

**US-1: 執行錯誤可查詢**
As a **DevOps operator**,
I want all command execution failures to be persisted in DynamoDB,
So that I can review failed command history for debugging and analytics.

## Acceptance Scenarios

### Scenario 1: AWS CLI 失敗（exit code != 0）→ error 寫入 DDB
- **Given**: `aws s3 ls s3://nonexistent-bucket` 被自動批准
- **When**: `execute_command()` 回傳含 `(exit code: 255)` 的輸出
- **Then**: `record_execution_error()` 被呼叫，DDB item 更新：
  - `status` = `executed_error`
  - `exit_code` = 255
  - `error_output` = 錯誤輸出前 2000 字元
- **And**: MCP response 包含 `exit_code: 255`

### Scenario 2: Bouncer 格式化錯誤（❌ prefix）→ 仍然寫入
- **Given**: 命令被 compliance_checker 攔截
- **When**: 回傳以 `❌` 開頭的輸出
- **Then**: `record_execution_error()` 被呼叫（向後兼容）

### Scenario 3: 成功執行 → 不寫入
- **Given**: `aws s3 ls` 成功回傳
- **When**: 輸出不含 error marker
- **Then**: `record_execution_error()` 不被呼叫（不變）

### Scenario 4: Trust session 下 AWS CLI 失敗
- **Given**: 命令在 trust session 中自動批准
- **When**: AWS CLI 回傳 exit code 1
- **Then**: `record_execution_error()` 被呼叫

### Scenario 5: Grant session 下 AWS CLI 失敗
- **Given**: 命令在 grant session 中自動批准
- **When**: AWS CLI 回傳 exit code != 0
- **Then**: `record_execution_error()` 被呼叫

## Requirements

- **R1**: 所有 3 個執行路徑（auto_approve、trust、grant）的 `is_failed` 判斷改用 `_is_failed_output()` 而非 `startswith('❌')`
- **R2**: `_is_failed_output()` 從 `commands.py` import 到 `mcp_execute.py`
- **R3**: `exit_code` 從 output 解析（已有 regex in `_is_failed_output`），而非永遠 -1
- **R4**: 向後兼容 — `startswith('❌')` 的 case 仍然被 `_is_failed_output()` 覆蓋
- **R5**: MCP response 的 `exit_code` 使用解析出的實際值（而非 -1）

## Interface Contract

### 需要新增的 helper（或從 commands.py export）

```python
def extract_exit_code(output: str) -> int:
    """從命令輸出解析 exit code。
    - 匹配 (exit code: N) → return N
    - ❌ prefix 且無 exit code → return -1
    - 成功 → return 0
    """
```

注意：`commands.py` 已有 `_is_failed_output()` 但它只回傳 bool。需要一個 companion function 提取實際 exit code，或修改 `_is_failed_output()` 回傳 exit code。
