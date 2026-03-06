# Sprint 9-005: Plan — trust_scope 錯誤訊息

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **`mcp_execute.py:210`**：
   ```python
   return mcp_error(req_id, -32602, 'Missing required parameter: trust_scope (use session key or stable ID)')
   ```

2. **`mcp_upload.py`**：upload / upload_batch 中 trust_scope 是 optional（不提供就跳過 trust check），所以這裡主要影響 execute。

3. **其他可能用到 trust_scope 的地方**：
   - `mcp_presigned.py` — presigned upload 也用 trust_scope
   - `grant.py` — grant session 不用 trust_scope

### 影響範圍

極小：只改 error message string。

## Implementation Phases

### Phase 1: 定義統一錯誤訊息（constants.py 或 utils.py）

1. 新增常數 `TRUST_SCOPE_MISSING_ERROR`，包含：
   - 說明用途
   - 格式範例
   - 使用提示

### Phase 2: 替換 mcp_execute.py

1. `mcp_execute.py:210`：替換現有 error message

### Phase 3: 檢查其他模組

1. 搜尋所有 `trust_scope` 相關的 error message
2. 確認一致性（upload/upload_batch 中 trust_scope 是 optional，不需要改）

### Phase 4: 測試

1. 確認現有測試中的 error message assertion 更新
