# Sprint 14-004: auto_approved / trust_auto_approved / grant_auto_approved 缺 request_id

> GitHub Issue: #71
> Priority: P1
> TCS: 3
> Generated: 2026-03-06

---

## Problem Statement

所有「直接執行」路徑的 response 都沒有回傳 `request_id`，但 audit log（DynamoDB）其實有寫入。

### 受影響路徑

| status | 檔案 | request_id 存在於變數 | response 有回傳 |
|--------|------|---------------------|----------------|
| `auto_approved` | `mcp_execute.py` ~line 649 | ✅ `request_id` 變數已存在（line 606） | ❌ |
| `trust_auto_approved` | `mcp_execute.py` ~line 761 | ✅ `request_id` 變數已存在（line 718） | ❌ |
| `grant_auto_approved` | `mcp_execute.py` ~line 570 | ✅ `grant_req_id` 變數已存在（line 530） | ❌ |
| `auto_approved` (REST) | `app.py` ~line 719 | ⚠️ `generate_request_id()` 在 line 710 但用的是 `log_decision` 的 local，不在 response scope | ❌ |

### 正常運作的對照

| status | response |
|--------|----------|
| `pending_approval` | ✅ 有 `request_id` |
| `approved` (poll 結果) | ✅ 有 `request_id` |

## Root Cause

`response_data` dict 構建時遺漏了 `request_id` key。變數都已存在（用於 `log_decision`、`store_paged_output`、`record_execution_error`），只是沒有放進 response。

## User Stories

**US-1: caller 可追蹤自動執行的命令**
As an **MCP client (Agent)**,
I want auto_approved responses to include `request_id`,
So that I can use `/status` or `/history` to look up the execution record later.

## Scope

### 變更 1: MCP auto_approved（mcp_execute.py ~line 649）

```python
response_data = {
    'status': 'auto_approved',
    'request_id': request_id,   # ← 補上（變數在 line 606）
    'command': ctx.command,
    ...
}
```

### 變更 2: MCP trust_auto_approved（mcp_execute.py ~line 761）

```python
response_data = {
    'status': 'trust_auto_approved',
    'request_id': request_id,   # ← 補上（變數在 line 718）
    'command': ctx.command,
    ...
}
```

### 變更 3: MCP grant_auto_approved（mcp_execute.py ~line 570）

```python
response_data = {
    'status': 'grant_auto_approved',
    'request_id': grant_req_id,   # ← 補上（變數在 line 530）
    'command': ctx.command,
    ...
}
```

### 變更 4: REST auto_approved（app.py ~line 719）

```python
request_id = generate_request_id(command)  # ← 已在 line 710 用於 log_decision，需提前或重用
...
return response(200, {
    'status': 'auto_approved',
    'request_id': request_id,   # ← 補上
    'command': command,
    'result': result
})
```

**注意：** REST 路徑中 `generate_request_id(command)` 在 `log_decision()` 呼叫時是 inline 的（line 710: `request_id=generate_request_id(command)`），需要把它提取成變數以便 response 使用。

## Out of Scope

- 修改 audit log 寫入邏輯
- 修改 `pending_approval` 路徑（已正常）

## Test Plan

### Unit Tests

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_auto_approved_response_has_request_id` | MCP auto_approved response 含 `request_id` |
| T2 | `test_trust_auto_approved_response_has_request_id` | trust path response 含 `request_id` |
| T3 | `test_grant_auto_approved_response_has_request_id` | grant path response 含 `request_id` |
| T4 | `test_rest_auto_approved_response_has_request_id` | REST auto_approved response 含 `request_id` |

### Regression

- 所有既有 auto_approve 測試不受影響（新增欄位是 additive）

## Acceptance Criteria

- [ ] `auto_approved` response 包含 `request_id`（MCP + REST）
- [ ] `trust_auto_approved` response 包含 `request_id`
- [ ] `grant_auto_approved` response 包含 `request_id`（key 名: `request_id`）
- [ ] `request_id` 值與 DDB audit record 中的一致
- [ ] 所有既有測試通過
