# Spec: bouncer_request_presigned_batch

**Issue:** #6
**Task:** bouncer-presigned-batch
**Priority:** critical

---

## 背景

前端部署需要上傳 10+ 個檔案（有大有小），目前沒有工具能同時解決「批量 + 大檔案」：
- `bouncer_upload_batch` → 走 Lambda，>500KB 靜默失敗
- `bouncer_request_presigned` → 只能單檔

---

## 目標

新增 `bouncer_request_presigned_batch`：
- 一次呼叫，傳入 N 個檔名 + content_type
- 回傳 N 個 presigned PUT URL
- Client 各自直接 PUT，不過 Lambda
- Staging bucket，不需審批

---

## 新增工具：`bouncer_request_presigned_batch`

### 輸入參數

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `files` | array | ✅ | `[{filename, content_type}]`，最多 50 個 |
| `reason` | string | ✅ | 上傳原因 |
| `source` | string | ✅ | 來源識別 |
| `account` | string | ❌ | 目標帳號（預設 DEFAULT_ACCOUNT_ID）|
| `expires_in` | int | ❌ | URL 有效期秒數（預設 900，min 60，max 3600）|

### 輸出

```json
{
  "status": "ready",
  "batch_id": "batch-abc123",
  "file_count": 10,
  "files": [
    {
      "filename": "index.html",
      "presigned_url": "https://s3.amazonaws.com/...",
      "s3_key": "2026-02-25/{batch_id}/index.html",
      "s3_uri": "s3://bouncer-uploads-190825685292/...",
      "method": "PUT",
      "headers": {"Content-Type": "text/html"}
    }
  ],
  "expires_at": "2026-02-25T07:00:00Z",
  "bucket": "bouncer-uploads-190825685292"
}
```

### 使用方式（client 端）

```bash
# 1. 取得所有 presigned URL
result=$(mcporter call bouncer bouncer_request_presigned_batch \
  --args '{
    "files": [
      {"filename": "index.html", "content_type": "text/html"},
      {"filename": "assets/pdf.worker.min.mjs", "content_type": "application/javascript"}
    ],
    "reason": "ZTP Files 前端部署",
    "source": "Private Bot (deploy)"
  }')

# 2. 各自 PUT（可並行）
echo $result | python3 -c "
import sys, json, subprocess
data = json.load(sys.stdin)
for f in data['files']:
    subprocess.run(['curl', '-X', 'PUT',
      '-H', f'Content-Type: {f[\"headers\"][\"Content-Type\"]}',
      '--data-binary', f'@{f[\"filename\"]}',
      f['presigned_url']])
"
```

---

## 設計決策

### 單一 batch_id，各自獨立 s3_key

每個檔案的 s3_key 格式：`{date}/{batch_id}/{filename}`

所有檔案共用同一個 batch_id，方便後續 `s3 cp` 知道去哪裡找。

### 不需審批

同 `bouncer_request_presigned`，staging bucket 不需審批。

### DynamoDB audit

寫一筆 batch audit record（action=`presigned_upload_batch`, status=`urls_issued`），包含所有 filename 列表。

### filename sanitization

與 `mcp_presigned.py` 的 `_sanitize_filename` 邏輯一致，保留子目錄結構。

### Rate limiting

對 source 做 rate limit check（同 `mcp_presigned.py`）。

---

## 影響範圍

| 檔案 | 變更 |
|------|------|
| `src/mcp_presigned.py` | 新增 `mcp_tool_request_presigned_batch()` |
| `src/mcp_tools.py` | 新增 tool re-export |
| `src/app.py` | route presigned_batch tool |
| `src/tool_schema.py` | 新增 schema 定義 |
| `tests/test_presigned_batch.py` | 新增 unit tests |
| `SKILL.md` | 新增 tool 文件 |

---

## 不在本 Sprint 範圍

- `bouncer_upload_batch` deprecated 標記（保留向下相容，下個 sprint）
- 正式 bucket presigned batch（需審批）

---

## 測試要求

1. 正常路徑：多個檔案，回傳正確格式
2. 空 files array → error
3. 超過 50 個檔案 → error
4. 缺少必填參數（filename/content_type/reason/source）
5. expires_in 驗證（min/max）
6. filename sanitization（path traversal）
7. DynamoDB audit record 寫入
8. S3 generate 失敗
9. Rate limit exceeded
10. 所有 s3_key 共用同一個 batch_id prefix

Coverage ≥ 75%（整體）
