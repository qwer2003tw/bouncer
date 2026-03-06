# Sprint 9-002: bug: upload_batch 大檔案靜默失敗根因修復

> GitHub Issue: #33
> Priority: P1
> Generated: 2026-03-02

---

## Feature Name

Upload Batch Silent Failure Fix — 修復 `bouncer_upload_batch` 在大檔案場景下靜默失敗的根因。

## Background

目前 `upload_batch` 的流程：
1. Client 將所有檔案 base64 編碼放入 MCP request JSON
2. Lambda 接收整個 payload → 解碼 → 上傳到 S3 staging
3. Human 批准 → 從 staging 搬到目標 bucket

問題：當 batch 包含大檔案時，整個 MCP request payload 可能接近 Lambda payload limit（6MB for sync invocation via API Gateway）。即使單個檔案通過了 `TRUST_UPLOAD_MAX_BYTES_PER_FILE`（5MB）檢查，多個檔案的 base64 encoded 內容加上 JSON overhead 可能超過 API Gateway/Lambda 限制，導致：
- API Gateway 返回 502/413
- Lambda 收到截斷的 payload → JSON parse 失敗 → 500
- 部分檔案上傳成功但後續的因 Lambda timeout 或 OOM 被中斷

TOOLS.md 記載：「base64 payload 超過 ~500KB 時可能在 Lambda 內部靜默失敗（status 仍顯示 approved）」

## User Stories

**US-1: 大檔案可靠上傳**
As an **AI agent**,
I want `bouncer_upload_batch` to reliably handle batches up to 20MB total,
So that frontend deployment and other multi-file uploads don't silently fail.

**US-2: 失敗時明確錯誤**
As an **AI agent**,
I want clear error messages when an upload batch exceeds limits,
So that I can split the batch or use presigned URLs instead.

## Acceptance Scenarios

### Scenario 1: Batch 總 payload 超過 Lambda limit — 明確拒絕
- **Given**: batch 含 3 個 2MB 檔案（base64 後 ~8MB，超過 6MB Lambda limit）
- **When**: 呼叫 `bouncer_upload_batch`
- **Then**: 返回 error，建議使用 `bouncer_request_presigned_batch`
- **And**: error message 包含 payload 大小和建議的替代方案

### Scenario 2: Batch 在 Lambda limit 內 — 正常上傳
- **Given**: batch 含 5 個 100KB 檔案（base64 後 ~680KB）
- **When**: 呼叫 `bouncer_upload_batch`
- **Then**: 正常上傳，行為不變

### Scenario 3: 單檔超過 payload-safe limit — 建議 presigned
- **Given**: 單檔 base64 後 > 3.5MB
- **When**: 呼叫 `bouncer_upload_batch` 或 `bouncer_upload`
- **Then**: 返回 error，建議改用 `bouncer_request_presigned` 或 `bouncer_request_presigned_batch`

### Scenario 4: 上傳後驗證 — 所有檔案均到位
- **Given**: batch 正常上傳完成
- **When**: 檢查 S3 staging
- **Then**: 每個檔案都有 `_verify_upload()` 結果
- **And**: 若驗證失敗，response 中 `verification_failed` 陣列列出失敗檔案

### Scenario 5: Trust 路徑下部分檔案上傳失敗
- **Given**: trust session 中 batch upload，第 3 個檔案 S3 put_object 失敗
- **When**: error 發生
- **Then**: 已上傳的檔案不回滾（已寫入 S3）
- **And**: response 明確告知哪些成功、哪些失敗
- **And**: trust quota 只扣已成功的數量

## Edge Cases

1. **API Gateway body size limit (10MB)**：base64 編碼讓有效 payload ~= 原始大小 × 1.37。4.5MB 原始 → ~6.2MB base64。接近 Lambda sync limit。
2. **Lambda memory pressure**：大量 base64 decode 會消耗記憶體。10 × 500KB = 5MB 原始 → decode 期間記憶體 ~15MB。
3. **Concurrent uploads**：多個 batch 同時進入 → S3 staging key collision 風險（已用 UUID 避免）。
4. **Rollback on staging failure**：目前已有 rollback 邏輯（staged_keys 逐個刪除）。

## Requirements

- **R1**: 新增 `UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT`（建議 3.5MB base64，約 2.5MB 原始），超過則拒絕並建議 presigned
- **R2**: 單檔 base64 大小超過此 limit 時，錯誤訊息明確建議 `bouncer_request_presigned_batch`
- **R3**: Trust 路徑部分失敗時，response 需區分成功/失敗檔案
- **R4**: 所有上傳完成後 response 必須包含 `verification_results`
- **R5**: 不改變 presigned 流程（那是大檔案的正確路徑）

## Interface Contract

### 新增常數（constants.py）

```python
# upload_batch 的 payload safe limit（base64 encoded bytes）
# Lambda sync invocation limit = 6MB, 預留 JSON overhead + safety margin
UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT = 3_500_000  # 3.5MB base64
```

### Error Response（新 error case）

```json
{
  "status": "error",
  "error": "Batch payload too large: 5.2MB base64 (safe limit 3.5MB). Use bouncer_request_presigned_batch for large files.",
  "suggestion": "bouncer_request_presigned_batch",
  "payload_size": 5242880,
  "safe_limit": 3500000
}
```
