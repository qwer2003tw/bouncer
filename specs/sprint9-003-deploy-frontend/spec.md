# Sprint 9-003: feat: bouncer_deploy_frontend + 批次審批

> GitHub Issues: #32, #34
> Priority: P1
> Generated: 2026-03-02

---

## Feature Name

Frontend Deployment Tool — 新增 `bouncer_deploy_frontend` MCP tool，自動化前端 build → upload → deploy → invalidation 流程，並支援批次審批（一次審批整個部署流程）。

## Background

目前前端部署需要 agent 手動串接多步驟：
1. `bouncer_upload_batch` 上傳到暫存 bucket
2. 等待 Steven 審批
3. 批准後，用 `bouncer_execute` 執行多個 `aws s3 cp` 搬到前端 bucket
4. 每個 `s3 cp` 可能需要單獨審批
5. 最後 `bouncer_execute` CloudFront invalidation

這個流程涉及 3-5 次 Telegram 審批，體驗很差。

## User Stories

**US-1: 一鍵前端部署**
As an **AI agent**,
I want a single `bouncer_deploy_frontend` tool that handles the entire frontend deployment pipeline,
So that I don't need to orchestrate 5+ separate MCP calls.

**US-2: 批次審批**
As **Steven**,
I want to approve the entire frontend deployment flow in one Telegram interaction,
So that I don't need to approve 5 separate requests.

**US-3: 部署可見性**
As **Steven**,
I want the Telegram approval request to show all files being deployed with their sizes,
So that I can make an informed approval decision.

## Acceptance Scenarios

### Scenario 1: 完整前端部署流程
- **Given**: Agent 有 build 好的 frontend files（index.html + assets/）
- **When**: 呼叫 `bouncer_deploy_frontend` with files manifest
- **Then**: 一個 Telegram 審批請求顯示所有檔案和目標
- **And**: Steven 批准後，所有檔案自動 deploy 到正確 bucket + CloudFront invalidation
- **And**: response 包含完整部署結果

### Scenario 2: 審批拒絕
- **Given**: `bouncer_deploy_frontend` 已發送審批請求
- **When**: Steven 點「拒絕」
- **Then**: 返回 `status: rejected`，不執行任何上傳或 s3 cp

### Scenario 3: 檔案驗證失敗
- **Given**: files manifest 中有不合法檔案（如 .exe）
- **When**: 呼叫 `bouncer_deploy_frontend`
- **Then**: 立即返回 error，不發送審批請求

### Scenario 4: 部分 deploy 失敗
- **Given**: 審批通過後開始 deploy
- **When**: 某個 s3 cp 命令失敗
- **Then**: 記錄哪些成功、哪些失敗
- **And**: 不自動回滾已成功的檔案（可手動清理）
- **And**: response 明確告知失敗詳情

### Scenario 5: Trust session 下自動部署
- **Given**: 存在 active trust session
- **When**: 呼叫 `bouncer_deploy_frontend`
- **Then**: 信任機制下自動部署（不需人工審批）
- **And**: 靜默 Telegram 通知部署結果

## Edge Cases

1. **index.html 必須存在**：沒有 index.html 的 deploy 是不完整的 → error
2. **Cache headers 自動設定**：index.html → `no-cache`, assets/ → `max-age=31536000,immutable`
3. **CloudFront invalidation 失敗**：不影響 S3 上傳結果，但 response 需標記
4. **Concurrent deploy**：需要鎖機制避免兩個 deploy 同時進行
5. **Empty assets/**：只有 index.html 的 deploy 也要支援

## Requirements

- **R1**: 新增 `bouncer_deploy_frontend` MCP tool
- **R2**: 一個 Telegram 審批請求包含所有檔案清單
- **R3**: 批准後自動執行：upload to staging → s3 cp to frontend bucket → CloudFront invalidation
- **R4**: 檔案的 Content-Type 和 Cache-Control 自動設定
- **R5**: Response 包含完整部署結果（每檔狀態）

## Interface Contract

### 新增 MCP Tool

```json
{
  "name": "bouncer_deploy_frontend",
  "description": "一鍵前端部署：上傳 → S3 → CloudFront invalidation",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project": {
        "type": "string",
        "description": "專案名稱（對應 frontend bucket 配置）"
      },
      "files": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "filename": { "type": "string" },
            "content": { "type": "string", "description": "base64 encoded" },
            "content_type": { "type": "string" }
          }
        }
      },
      "reason": { "type": "string" },
      "source": { "type": "string" },
      "trust_scope": { "type": "string" }
    },
    "required": ["project", "files", "source", "trust_scope"]
  }
}
```

### 專案配置（DDB 或 config）

```json
{
  "project": "ztp-files",
  "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
  "cloudfront_distribution_id": "E176PW0SA5JF29",
  "region": "us-east-1",
  "cache_rules": {
    "index.html": "no-cache,no-store,must-revalidate",
    "assets/*": "max-age=31536000,immutable"
  }
}
```

### Telegram 審批 UI

```
🚀 前端部署請求

📦 專案：ztp-files
📁 檔案（3 個，245KB）：
  • index.html (12KB) → no-cache
  • assets/index-a1b2c3.js (180KB) → immutable
  • assets/index-d4e5f6.css (53KB) → immutable

🤖 來源：Private Bot (ZTP Files - 部署)
💬 原因：Sprint 9 前端更新

[✅ 批准] [🔓 批准+信任] [❌ 拒絕]
```
