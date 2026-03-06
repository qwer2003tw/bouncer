# Sprint 9-002: Plan — upload_batch 大檔案靜默失敗根因修復

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **`mcp_tool_upload_batch()`**（`mcp_upload.py:546`）：接收 `files[]` 陣列，每個 file 有 `content`（base64）。單檔限制 `TRUST_UPLOAD_MAX_BYTES_PER_FILE = 5MB`，batch 總限制 `TRUST_UPLOAD_MAX_BYTES_TOTAL = 20MB`。

2. **問題根因**：這些限制是針對 decoded bytes。但 Lambda payload limit 是針對整個 HTTP body（包含 base64 string + JSON overhead）。base64 膨脹率 ~1.37x。所以 3.5MB decoded = ~4.8MB base64 + JSON = 接近 6MB limit。

3. **`mcp_tool_upload()`**（單檔）：已有 `max_size = 4.5 * 1024 * 1024` 檢查（`mcp_upload.py:160`），但這是 decoded size，base64 後可能超過 Lambda limit。

4. **現有 presigned 流程**：`bouncer_request_presigned_batch`（`mcp_presigned.py:575`）是設計給大檔案用的，完全繞過 Lambda payload limit。

### 修復策略

**不改架構**，只加 early validation：在 base64 decode 之前先檢查 payload 大小。超過 safe limit 就拒絕並建議 presigned。

## Implementation Phases

### Phase 1: 常數定義（constants.py）

1. 新增 `UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT = 3_500_000`（3.5MB）
2. 新增 `UPLOAD_SINGLE_PAYLOAD_SAFE_LIMIT = 3_500_000`

### Phase 2: mcp_upload.py — upload_batch payload check

1. 在 files 驗證迴圈**之前**，計算所有 `content` field 的 base64 string 總長度
2. 若總長度 > `UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT`，返回 error + suggestion
3. 同時檢查單個檔案的 base64 長度

### Phase 3: mcp_upload.py — upload 單檔 payload check

1. 在 `mcp_tool_upload()` 的 base64 decode 之前，檢查 `content_b64` 字串長度
2. 超過 `UPLOAD_SINGLE_PAYLOAD_SAFE_LIMIT` → error + 建議用 presigned

### Phase 4: Trust 路徑部分失敗處理（mcp_upload.py）

1. 在 trust batch upload 的 for loop 中，catch 單檔 put_object 失敗
2. 記錄成功/失敗檔案清單
3. Response 中區分 `uploaded` 和 `failed` 陣列

### Phase 5: tool_schema.py 更新

1. 在 `bouncer_upload_batch` 的 description 中加入大小限制說明
2. 建議大檔案使用 presigned 流程

### Phase 6: 測試

1. 測試 payload 超過 safe limit 時的錯誤回應
2. 測試 payload 在 limit 內的正常行為
3. 測試 trust 路徑部分失敗的 response 結構
