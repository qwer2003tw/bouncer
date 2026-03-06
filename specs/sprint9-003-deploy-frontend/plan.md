# Sprint 9-003: Plan — bouncer_deploy_frontend + 批次審批

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **前端部署流程**（見 TOOLS.md）：目前是 agent 手動串接 upload_batch + 多個 execute(s3 cp) + execute(CloudFront invalidation)。每步可能需要審批。

2. **現有 tool 架構**：
   - `mcp_tools.py:63` 註冊所有 MCP tools
   - `tool_schema.py` 定義 input schema
   - `mcp_upload.py` 處理上傳
   - `mcp_presigned.py` 處理 presigned URL
   - `callbacks.py` 處理 Telegram callback

3. **Batch approve 前例**：`upload_batch` 已有批次審批（一次 approve 所有檔案）。`deploy_frontend` 可以複用這個模式。

4. **`bouncer_request_presigned_batch`** 也是批次模式，但用的是 presigned URL + client 直傳 S3。

### 設計決策

**方案 A: 全部在 Lambda 內完成**（選此）
- 接收 files（小檔 base64 或引用 staging key）→ staging → 審批 → deploy
- 優點：單一請求，atomic
- 缺點：受 Lambda payload limit

**方案 B: 分離 upload + deploy 兩步**
- 先用 presigned batch 上傳 → 再呼叫 deploy_frontend 引用已上傳的 staging files
- 優點：繞過 payload limit
- 缺點：兩步驟，稍複雜

**推薦方案 A + fallback B**：預設走 A（小型前端），超過 payload limit 時 error 引導用 B。

## Implementation Phases

### Phase 1: 專案配置（新增 DDB table 或 config）

1. 在 `deployer_projects_table` 中新增 frontend project 配置
2. 或者：在現有 `bouncer-projects` table 新增 `type: frontend` 的 entry
3. 配置欄位：`frontend_bucket`, `cloudfront_distribution_id`, `region`, `cache_rules`

### Phase 2: 新增 mcp_deploy_frontend.py

1. 新模組：`src/mcp_deploy_frontend.py`
2. 核心函數：`mcp_tool_deploy_frontend(req_id, arguments) -> dict`
3. 流程：
   a. 驗證 project 存在 + files 格式正確
   b. 驗證 index.html 存在
   c. Payload 大小檢查（超過 → error + 建議 presigned）
   d. 上傳到 S3 staging（複用 upload_batch 邏輯）
   e. 寫入 DDB pending 記錄（action=deploy_frontend）
   f. 發 Telegram 審批（顯示完整 file list + cache rules）

### Phase 3: Callback 處理（callbacks.py）

1. 新增 `deploy_frontend` action handler
2. 批准後：
   a. 從 staging bucket 讀取檔案
   b. s3 cp 到 frontend bucket（帶 Content-Type + Cache-Control）
   c. CloudFront invalidation
   d. 更新 DDB 記錄（success/failed + 每檔狀態）
   e. 更新 Telegram 訊息

### Phase 4: Tool 註冊

1. `tool_schema.py`: 新增 `bouncer_deploy_frontend` schema
2. `mcp_tools.py`: 註冊 handler

### Phase 5: Telegram UI

1. `notifications.py`: 新增 `send_deploy_frontend_notification()`
2. 顯示：專案名、檔案清單 + 大小 + cache rule、來源、原因

### Phase 6: 測試

1. 單元測試：input validation, file list parsing
2. Integration test：完整 deploy 流程（mock S3 + CloudFront）
3. Error case：缺 index.html、payload 超限、部分 deploy 失敗
