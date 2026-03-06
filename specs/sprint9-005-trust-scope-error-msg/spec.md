# Sprint 9-005: fix: trust_scope 錯誤訊息

> GitHub Issue: #36
> Priority: P2
> Generated: 2026-03-02

---

## Feature Name

Trust Scope Error Message Improvement — 改善 `trust_scope` 缺失時的錯誤訊息，提供 actionable 指引。

## Background

目前 `mcp_execute.py:210`：
```python
return mcp_error(req_id, -32602, 'Missing required parameter: trust_scope (use session key or stable ID)')
```

問題：
1. Agent（特別是新 agent 或 public bot）看到這個錯誤訊息不知道該怎麼做
2. 沒有說明 `trust_scope` 的具體格式要求
3. 沒有範例
4. 其他需要 `trust_scope` 的 tools（upload、upload_batch）的錯誤訊息可能不一致

## User Stories

**US-1: Agent 自我修正**
As an **AI agent encountering this error**,
I want the error message to include an example of a valid trust_scope value,
So that I can fix the request without needing human help.

## Acceptance Scenarios

### Scenario 1: execute 缺少 trust_scope
- **Given**: Agent 呼叫 `bouncer_execute` 但未提供 `trust_scope`
- **When**: validation 失敗
- **Then**: 錯誤訊息包含：
  - 說明 trust_scope 的用途
  - 有效格式範例（如 `agent:main:session-id`）
  - hint: 使用穩定的 session identifier

### Scenario 2: 其他 tools 的 trust_scope 錯誤訊息一致
- **Given**: Agent 呼叫 `bouncer_upload` 或 `bouncer_upload_batch` 未提供 trust_scope
- **When**: validation 檢查
- **Then**: 使用相同格式的錯誤訊息

### Scenario 3: trust_scope 為空字串
- **Given**: Agent 提供 `trust_scope: ""`
- **When**: `.strip()` 後為空
- **Then**: 與缺少 trust_scope 相同的錯誤訊息

## Edge Cases

1. **trust_scope 為 whitespace-only**：`"   "` → strip 後為空 → 同樣觸發錯誤
2. **trust_scope 過長**：需要長度限制？目前沒有，暫不處理

## Requirements

- **R1**: 統一所有 tools 的 trust_scope 缺失錯誤訊息
- **R2**: 錯誤訊息包含格式範例
- **R3**: 不改變 error code（維持 -32602）

## Interface Contract

### 無 API/DDB Schema 變更

僅修改 error message 文字。

### 建議的新錯誤訊息

```
Missing required parameter: trust_scope. 
Provide a stable session identifier (e.g. 'agent:main:session-abc123' or your session key). 
trust_scope is used to track trust sessions — use the same value across related requests.
```
