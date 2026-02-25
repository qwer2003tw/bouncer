# Spec: bouncer_request_presigned

**Issue:** #4
**Task:** bouncer-presigned-upload
**Priority:** critical

---

## 背景

`bouncer_upload` / `bouncer_upload_batch` 走 API Gateway → Lambda，base64 膨脹後實際穩定上限約 500KB。
ZTP Files 前端有 `pdf.worker.min.mjs`（1MB raw → 1.4MB b64），每次部署都要繞限制，非常麻煩。

---

## 目標

新增 `bouncer_request_presigned` MCP 工具，讓 client 直接 PUT 到 S3 presigned URL，不經過 Lambda，解除大小限制。

---

## 新增工具：`bouncer_request_presigned`

### 輸入參數

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `filename` | string | ✅ | 目標檔名（含路徑，如 `assets/pdf.worker.min.mjs`） |
| `content_type` | string | ✅ | MIME type（如 `application/javascript`） |
| `reason` | string | ✅ | 上傳原因 |
| `source` | string | ✅ | 來源識別（如 `Private Bot (deploy)`） |
| `account` | string | ❌ | 目標帳號 ID（預設 DEFAULT_ACCOUNT_ID） |
| `expires_in` | int | ❌ | presigned URL 有效期秒數（預設 900，最大 3600） |

### 輸出

```json
{
  "status": "ready",
  "presigned_url": "https://bouncer-uploads-190825685292.s3.amazonaws.com/...",
  "s3_key": "2026-02-25/{uuid}/{filename}",
  "s3_uri": "s3://bouncer-uploads-190825685292/2026-02-25/{uuid}/{filename}",
  "request_id": "abc123",
  "expires_at": "2026-02-25T05:00:00Z",
  "method": "PUT",
  "headers": {
    "Content-Type": "application/javascript"
  }
}
```

### 使用方式（client 端）

```bash
# 1. 取得 presigned URL
result=$(mcporter call bouncer bouncer_request_presigned ...)

# 2. 直接 PUT（不過 Lambda）
curl -X PUT \
  -H "Content-Type: application/javascript" \
  --data-binary @pdf.worker.min.mjs \
  "{presigned_url}"
```

---

## 設計決策

### 審批設計

- Staging bucket（`bouncer-uploads-{DEFAULT_ACCOUNT_ID}`）→ **不需要審批**
  - Presigned URL 只允許上傳到 staging，不直接到正式 bucket
  - 安全邊界：staging 的檔案還需要後續 `bouncer_execute s3 cp` 才能到正式環境（那步才需審批）
- 正式 bucket → **需要審批**（Phase 2 再做，本 sprint 不含）

### Staging bucket 路徑

```
{date}/{request_id}/{filename}
```
與現有 `bouncer_upload` 路徑一致，方便後續 `s3 cp` 操作。

### Presigned URL 生成方式

- Lambda 用自己的 IAM role（已有 staging bucket PutObject 權限）生成 presigned URL
- 用 `boto3.client('s3').generate_presigned_url('put_object', ...)`
- 不需要 assume_role（staging bucket 在主帳號，Lambda role 有權限）

### 審計記錄

每次呼叫寫入 DynamoDB（`bouncer-prod-requests`）：

```json
{
  "request_id": "abc123",
  "action": "presigned_upload",
  "status": "url_issued",
  "filename": "assets/pdf.worker.min.mjs",
  "s3_key": "2026-02-25/abc123/assets/pdf.worker.min.mjs",
  "bucket": "bouncer-uploads-190825685292",
  "content_type": "application/javascript",
  "source": "Private Bot (deploy)",
  "expires_at": 1234567890,
  "created_at": 1234567890
}
```

---

## 不在本 Sprint 範圍

- `bouncer_confirm_upload`（驗證檔案已上傳）— Phase 2
- 正式 bucket 的 presigned URL（需審批）— Phase 2
- Trust session 整合 — Phase 2

---

## 影響範圍

| 檔案 | 變更 |
|------|------|
| `src/mcp_presigned.py` | 新增（presigned URL 邏輯） |
| `src/mcp_tools.py` | 新增 tool 定義 |
| `src/app.py` | route presigned tool |
| `tests/test_presigned.py` | 新增（unit tests） |
| `SKILL.md`（bouncer skill） | 新增 tool 文件 |

---

## 測試要求

1. 正常路徑：`bouncer_request_presigned` 回傳正確格式
2. 參數驗證：缺少必填、expires_in 超過 3600
3. filename sanitization（防 path traversal）
4. DynamoDB 寫入確認
5. 錯誤處理：S3 generate 失敗

Coverage 維持 ≥ 75%。
