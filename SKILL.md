---
name: bouncer
description: Execute AWS CLI commands with Telegram approval. Safe commands auto-execute, dangerous commands require human approval via Telegram. Supports trust sessions, batch uploads, and grant sessions.
metadata: {"openclaw": {"emoji": "🔐", "requires": {"bins": ["mcporter"]}}}
---

# Bouncer - AWS Command Approval System

## ⚡ 推薦：用 bouncer-exec skill 執行 AWS 命令

```bash
# 輸出 clean（無 JSON wrapper），自動 poll 等審批
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh aws s3 ls
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh aws sts get-caller-identity
```

**✅ v3.10.0 特殊字元支援（#49 #51）：**
含空格、pipe (`|`)、括號等特殊字元的參數現在自動用雙引號包裹，`aws_cli_split` 解析正確。
```bash
# pipe 字元現在正確處理
bash bouncer_exec.sh aws s3 ls --query "Contents[?Size > \`1000\`]"
```

**只有以下 MCP-only tools 才用 `mcporter` 直接呼叫：**
`bouncer_deploy`, `bouncer_deploy_frontend`, `bouncer_upload`, `bouncer_upload_batch`, `bouncer_request_grant`, `bouncer_trust_status` 等

---

Use `mcporter` to execute AWS CLI commands through the Bouncer approval system.

**API:** `https://n8s3f1mus6.execute-api.us-east-1.amazonaws.com/prod/`
**GitHub:** https://github.com/qwer2003tw/bouncer
**MCP Source:** `/home/ec2-user/projects/bouncer/bouncer_mcp.py`

## 異步設計（重要！必讀！）

所有需要審批的操作**預設異步**返回，避免 API Gateway 29 秒超時：

```bash
# 1. 發送請求（立即返回 request_id）
mcporter call bouncer bouncer_execute \
  command="aws s3 mb s3://test" \
  reason="建桶" \
  source="Private Bot (task)" \
  trust_scope="private-bot-main"
# 返回: {"status": "pending_approval", "request_id": "abc123", ...}

# 2. 輪詢結果（必須！不會自動通知！）
mcporter call bouncer bouncer_status request_id="abc123"
# 返回: {"status": "approved", "result": "..."} 或 {"status": "pending_approval"}
```

### ⚠️ 審批輪詢規則（強制）

收到 `pending_approval` 後，**你必須主動輪詢 `bouncer_status`**，Bouncer 不會主動通知你結果：

```
1. 等 10 秒後第一次查 bouncer_status
2. 如果還是 pending，每 10-15 秒查一次
3. 最多輪詢 5 分鐘
4. 超過 5 分鐘仍 pending → 回報「等待審批中，request_id: xxx」
```

## ⚠️ 必填參數

### trust_scope（bouncer_execute 必填）

`trust_scope` 是穩定的呼叫者識別符，用於信任匹配。**bouncer_execute 必須帶此參數**。

- 使用 session key 或其他穩定 ID（不要用 source，source 是顯示用）
- 同一個 bot 不同任務應有不同 trust_scope
- 上傳 tools（bouncer_upload / bouncer_upload_batch）trust_scope 是 optional

**缺少 trust_scope 時的錯誤訊息（v3.9.0 改善）：**
```
Missing required parameter: trust_scope

trust_scope is a stable caller identifier used for trust session matching.
Examples:
  - "private-bot-main"        (for general usage)
  - "private-bot-deploy"      (for deployment tasks)
  - "private-bot-kubectl"     (for kubectl operations)
```

### source（所有操作必填）

`source` 是顯示用的來源描述，出現在 Telegram 通知中。

格式：`{Bot名稱} ({專案/任務})`
- ✅ `source="Private Bot (Bouncer 部署)"`
- ❌ `source="Private Bot"`（太模糊）

---

## Core Tools

### bouncer_execute
執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。

```bash
mcporter call bouncer bouncer_execute \
  command="aws ec2 describe-instances" \
  reason="檢查 EC2 狀態" \
  source="Private Bot (infra check)" \
  trust_scope="private-bot-main"
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `command` | ✅ | AWS CLI 命令 |
| `reason` | ✅ | 執行原因（顯示在審批通知） |
| `source` | ✅ | 來源標識 |
| `trust_scope` | ✅ | 穩定呼叫者 ID（session key） |
| `account` | ❌ | 目標 AWS 帳號 ID（預設 190825685292） |
| `sync` | ❌ | 同步模式（不推薦） |

**Returns:**
- `auto_approved` — 安全命令，已自動執行
- `pending_approval` — 需要 Telegram 審批
- `blocked` — 被封鎖（含 `block_reason` 和 `suggestion`）
- `trust_auto_approved` — 信任期間自動執行

**✅ v3.10.0 Telegram 審批按鈕（英文化 + Bot API 9.4 style）：**
- `[✅ Approve]` (green) / `[❌ Reject]` (red) — 一般審批
- `[🔓 Trust 10min]` (blue) — 建立信任時段
- `[⏹ End Trust]` / `[🚫 Revoke Grant]` — 撤銷
- `[⏰ expires_at]` — 顯示「5 分鐘後過期（UTC 14:35）」（UTC 絕對時間）

**⚠️ Lambda 環境變數保護（B-LAMBDA-01）：**
- `lambda update-function-configuration --environment Variables={}` → **BLOCKED**（空值覆寫保護）
- `lambda update-function-configuration --environment Variables={...}` → **DANGEROUS**（帶值需審批，附警告）

### bouncer_status
查詢審批請求狀態。

```bash
mcporter call bouncer bouncer_status request_id="abc123"
```

### bouncer_list_pending
列出待審批的請求。

```bash
mcporter call bouncer bouncer_list_pending source="Private Bot"
```

---

## Upload Tools

### bouncer_upload
上傳單一檔案到 S3。

```bash
CONTENT=$(base64 -w0 config.json)
mcporter call bouncer bouncer_upload \
  filename="config.json" \
  content="$CONTENT" \
  content_type="application/json" \
  reason="上傳設定檔" \
  source="Private Bot (config)" \
  trust_scope="private-bot-main"
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `filename` | ✅ | 檔案名稱 |
| `content` | ✅ | 檔案內容（base64 encoded） |
| `reason` | ✅ | 上傳原因 |
| `source` | ✅ | 來源標識 |
| `content_type` | ❌ | MIME type（預設 `application/octet-stream`） |
| `trust_scope` | ❌ | 信任範圍 ID（帶了才能走信任上傳） |
| `account` | ❌ | 目標帳號 |

**信任上傳（Trust Upload）：**
- 信任期間 + 帶 trust_scope → 自動上傳（不需審批）
- 每個信任時段最多 5 次上傳
- 每檔 5MB、每 session 20MB 上限
- 副檔名黑名單：`.sh .exe .py .jar .zip .tar.gz .7z .bat .ps1 .rb .war .bin .bash`
- Custom s3_uri 不會走信任（只允許預設路徑）

### bouncer_upload_batch
批量上傳多個檔案，**一次審批**。

```bash
mcporter call bouncer bouncer_upload_batch \
  files='[
    {"filename":"index.html","content":"'$(base64 -w0 index.html)'"},
    {"filename":"style.css","content":"'$(base64 -w0 style.css)'"},
    {"filename":"app.js","content":"'$(base64 -w0 app.js)'"}
  ]' \
  reason="前端部署" \
  source="Private Bot (ZTP Files deploy)" \
  trust_scope="private-bot-main"
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `files` | ✅ | JSON array: `[{filename, content, content_type?}]` |
| `reason` | ✅ | 上傳原因 |
| `source` | ✅ | 來源標識 |
| `trust_scope` | ❌ | 信任範圍 ID |
| `account` | ❌ | 目標帳號 |

**Limits:**
- 最多 50 個檔案
- 每檔 5MB、總計 20MB
- 副檔名黑名單（同 bouncer_upload）
- 檔名自動消毒（path traversal、null bytes 等）

**⚠️ 大檔案早期驗證（v3.9.0）：**
payload 超過 3.5MB base64（約 2.5MB raw）時，**在 decode 前**立即返回明確錯誤：
```json
{
  "status": "validation_error",
  "message": "Total payload too large: ... bytes (limit 3500000). Use bouncer_request_presigned_batch for large files.",
  "suggestion": "bouncer_request_presigned_batch"
}
```
→ 改用 `bouncer_request_presigned_batch` 避免 Lambda 靜默失敗。

**⚠️ base64 截斷偵測（v3.9.0）：**
透過 CLI args 傳入的 base64 content 若被 OS 截斷（`len % 4 != 0`），立即返回錯誤：
```json
{
  "status": "validation_error",
  "message": "Invalid base64 content: likely truncated by OS argument length limit. Use HTTP API or bouncer_request_presigned."
}
```

**審批按鈕：**
- `[📁 批准上傳]` — 只批准這批
- `[🔓 批准 + 信任10分鐘]` — 批准 + 開信任（含 5 次上傳 quota）
- `[❌ 拒絕]`

**信任 batch：** 如果有 active trust session + 足夠 quota → 全部自動執行

---

### bouncer_request_presigned
**大檔案直傳**：生成 S3 presigned PUT URL，client 直接 PUT，不過 Lambda（解除 500KB 限制）。

```bash
# Step 1: 取得 presigned URL
result=$(mcporter call bouncer bouncer_request_presigned \
  --args '{
    "filename": "assets/pdf.worker.min.mjs",
    "content_type": "application/javascript",
    "reason": "ZTP Files 前端部署",
    "source": "Private Bot (ZTP Files deploy)"
  }')

# Step 2: 直接 PUT（不過 Lambda）
presigned_url=$(echo $result | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('presigned_url',''))")
curl -X PUT \
  -H "Content-Type: application/javascript" \
  --data-binary @pdf.worker.min.mjs \
  "$presigned_url"
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `filename` | ✅ | 目標檔名（含路徑，如 `assets/foo.js`）|
| `content_type` | ✅ | MIME type |
| `reason` | ✅ | 上傳原因 |
| `source` | ✅ | 來源標識 |
| `account` | ❌ | 目標帳號（預設主帳號）|
| `expires_in` | ❌ | URL 有效期秒數（預設 900，min 60，max 3600）|

**Response:**
```json
{
  "status": "ready",
  "presigned_url": "https://...",
  "s3_key": "2026-02-25/{request_id}/assets/foo.js",
  "s3_uri": "s3://bouncer-uploads-190825685292/...",
  "request_id": "abc123",
  "expires_at": "2026-02-25T06:00:00Z",
  "method": "PUT",
  "headers": {"Content-Type": "application/javascript"}
}
```

**特性：**
- **不需審批**（只上傳到 staging bucket）
- Staging bucket 固定用主帳號（`bouncer-uploads-{DEFAULT_ACCOUNT_ID}`）
- 後續搬到正式 bucket 仍需 `bouncer_execute s3 cp`（那步才審批）
- 寫 DynamoDB audit record（`action=presigned_upload`, `status=url_issued`）
- filename sanitization 保留子目錄結構（`assets/foo.js` 完整保留）

---

### bouncer_request_presigned_batch
**批量大檔案直傳**：一次呼叫取得 N 個 presigned PUT URL，client 各自直接 PUT，不過 Lambda。解決前端部署 10+ 檔案有大有小的問題。

```bash
# Step 1: 一次取得所有 presigned URL
result=$(mcporter call bouncer bouncer_request_presigned_batch \
  --args '{
    "files": [
      {"filename": "index.html", "content_type": "text/html"},
      {"filename": "assets/index-xxx.js", "content_type": "application/javascript"},
      {"filename": "assets/pdf.worker.min.mjs", "content_type": "application/javascript"}
    ],
    "reason": "ZTP Files 前端部署",
    "source": "Private Bot (ZTP Files deploy)"
  }')

# Step 2: 各自 PUT（可並行）
echo $result | python3 -c "
import sys, json, subprocess
data = json.load(sys.stdin)
for f in data['files']:
    subprocess.run(['curl', '-s', '-X', 'PUT',
      '-H', f'Content-Type: {f[\"headers\"][\"Content-Type\"]}',
      '--data-binary', f'@{f[\"filename\"]}',
      f['presigned_url']])
    print(f'Uploaded: {f[\"filename\"]} -> {f[\"s3_uri\"]}')
"
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `files` | ✅ | `[{filename, content_type}]`，最多 50 個 |
| `reason` | ✅ | 上傳原因 |
| `source` | ✅ | 來源標識 |
| `account` | ❌ | 目標帳號（預設主帳號）|
| `expires_in` | ❌ | URL 有效期秒數（預設 900，min 60，max 3600）|

**Response:**
```json
{
  "status": "ready",
  "batch_id": "batch-abc123",
  "file_count": 3,
  "files": [
    {
      "filename": "index.html",
      "presigned_url": "https://...",
      "s3_key": "2026-02-25/batch-abc123/index.html",
      "s3_uri": "s3://bouncer-uploads-190825685292/...",
      "method": "PUT",
      "headers": {"Content-Type": "text/html"}
    }
  ],
  "expires_at": "2026-02-25T07:00:00Z",
  "bucket": "bouncer-uploads-190825685292"
}
```

**特性：**
- **不需審批**（只上傳到 staging bucket）
- 所有檔案共用同一 `batch_id` prefix，方便後續 `s3 cp` 批量搬到正式 bucket
- Duplicate filename 自動加 suffix（`_1`, `_2`, ...）
- DynamoDB 單筆 batch audit record

---

### bouncer_confirm_upload
**驗證 presigned batch 上傳結果**：在 PUT 後確認所有檔案已成功上傳到 staging bucket，避免後續 `s3 cp` 時遇到 404。

```bash
result=$(mcporter call bouncer bouncer_confirm_upload \
  --args '{
    "batch_id": "batch-db31d35b7c1e",
    "files": [
      {"s3_key": "2026-02-25/batch-db31d35b7c1e/index.html"},
      {"s3_key": "2026-02-25/batch-db31d35b7c1e/assets/main.js"}
    ]
  }')

# 回傳 verified=true 才繼續後續 s3 cp
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `batch_id` | ✅ | batch ID（格式：`batch-{12 hex chars}`）|
| `files` | ✅ | `[{s3_key}]`，最多 50 個 |

**Response（成功）：**
```json
{
  "batch_id": "batch-db31d35b7c1e",
  "verified": true,
  "results": [
    {"s3_key": "2026-02-25/batch-db31d35b7c1e/index.html", "exists": true},
    {"s3_key": "2026-02-25/batch-db31d35b7c1e/assets/main.js", "exists": true}
  ],
  "missing": []
}
```

**Response（有缺失）：**
```json
{
  "batch_id": "batch-db31d35b7c1e",
  "verified": false,
  "results": [...],
  "missing": ["2026-02-25/batch-db31d35b7c1e/assets/main.js"]
}
```

**特性：**
- **不需審批**（純 S3 read，無 Telegram 通知）
- 使用 `list_objects_v2` 批量驗證（比 N 次 HeadObject 省 API call）
- DynamoDB audit record（TTL 7 天），可事後查驗
- `verified=false` 時列出所有缺失檔案

**建議的前端部署流程：**
```
presigned_batch → PUT 上傳 → confirm_upload 驗證 → (verified=true) → grant s3 cp
```

---

## Trust Session

審批時選「🔓 信任10分鐘」，期間同 trust_scope 的操作自動執行。

### 信任時段結束摘要（v3.9.0 新增）
Trust session **revoke 或自動到期**時，Bouncer 會自動發送 Telegram 摘要：
```
🔓 信任時段結束（手動撤銷）
⏱ 時長：8 分 32 秒
📋 執行了 5 個命令：
  1. ✅ aws s3 ls s3://bucket
  2. ✅ aws ec2 describe-instances
  3. ❌ aws s3 cp /nonexistent ...
  ...
✅ 4 成功 / ❌ 1 失敗
```

### ⚡ 批次操作正確流程（重要！）

需要執行多個命令時，**不要一個一個等審批**，應該：

```
✅ 正確流程：
1. 把所有命令一次全部發出（全部帶同一 trust_scope）
   → 全部進入 pending 狀態
2. Steven 看到第一個請求，按「🔓 信任10分鐘」
3. Bouncer 建立 trust session，並自動執行所有同 trust_scope 的 pending 命令
4. 後續新命令帶同一 trust_scope → 繼續自動執行

❌ 錯誤流程：
1. 發第一個命令 → 等審批 → 審批後才發第二個
   → 信任時段已建立，但第二個命令是新的 pending，不在 trust 建立當下
   → 第二個命令可能需要再次審批（trust 不一定能 match）
```

**關鍵原則：先批量發出所有 pending，再等 Steven 一次批准信任。**

### 特性
- 時長：10 分鐘
- 命令上限：20 次/session
- 上傳上限：5 次/session（5MB/檔, 20MB/session total）
- 匹配方式：`trust_scope + account_id`（不是 source）

### 排除（即使信任中仍需審批）
- **高危服務**：iam, sts, organizations, kms, secretsmanager, cloudformation, cloudtrail
- **高危操作**：delete-*, terminate-*, stop-*, modify-*, s3 rm, update-function-code 等
- **危險旗標**：--force, --recursive, --skip-final-snapshot 等
- **上傳排除**：blocked 副檔名、custom s3_uri

### Tools
```bash
mcporter call bouncer bouncer_trust_status
mcporter call bouncer bouncer_trust_status source="Private Bot"
mcporter call bouncer bouncer_trust_revoke trust_id="trust-xxx-yyy"
```

---

## Grant Session（批次授權）

預先申請一組命令的執行權限，審批後可在 TTL 內重複或一次性執行。

### bouncer_request_grant
```bash
mcporter call bouncer bouncer_request_grant \
  commands='["aws ec2 describe-instances", "aws s3 ls"]' \
  reason="基礎設施檢查" \
  source="Private Bot (infra)" \
  trust_scope="private-bot-main" \
  ttl_minutes=30 \
  allow_repeat=true
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `commands` | ✅ | JSON array of AWS CLI commands |
| `reason` | ✅ | 授權原因 |
| `source` | ✅ | 來源標識 |
| `trust_scope` | ✅ | 呼叫者 ID |
| `ttl_minutes` | ❌ | 授權時長（1-60 分鐘，預設 30） |
| `allow_repeat` | ❌ | 可重複執行（預設 true） |
| `account` | ❌ | 目標帳號 |

### bouncer_grant_execute
在已批准的 grant 內執行命令（**精確匹配**）。

```bash
mcporter call bouncer bouncer_grant_execute \
  grant_id="grant-abc123" \
  command="aws ec2 describe-instances" \
  trust_scope="private-bot-main"
```

### bouncer_grant_status
```bash
mcporter call bouncer bouncer_grant_status grant_id="grant-abc123"
```

### Grant vs Trust
| 維度 | Grant Session | Trust Session |
|------|---------------|---------------|
| 模式 | 白名單（精確命令） | 黑名單（排除高危） |
| 觸發 | Agent 主動申請 | 審批者選擇信任 |
| 匹配 | 命令精確匹配 | trust_scope + account |
| 適用 | 可預測的命令清單 | 互動式探索 |
| 上傳 | 不支援 | 支援（quota 限制） |

---

## Frontend Deployer

### bouncer_deploy_frontend
一鍵前端部署：staging → 一次審批 → S3 copy + CloudFront invalidation，取代原本 3-5 次審批流程。

> **實作說明：** 審批通過後，S3 cp 和 CloudFront invalidation 走 `execute_command`（CodeBuild 執行），不需要給 Bouncer Lambda 額外 IAM 權限。

```bash
mcporter call bouncer bouncer_deploy_frontend \
  project="ztp-files" \
  files='[
    {"filename":"index.html","content":"'$(base64 -w0 index.html)'","content_type":"text/html"},
    {"filename":"assets/app.js","content":"'$(base64 -w0 assets/app.js)'","content_type":"application/javascript"}
  ]' \
  reason="前端部署" \
  source="Private Bot (ZTP Files deploy)" \
  trust_scope="private-bot-deploy"
```

**Parameters:**
| 參數 | 必填 | 說明 |
|------|------|------|
| `project` | ✅ | 專案名稱（目前支援：`ztp-files`）|
| `files` | ✅ | JSON array: `[{filename, content(base64), content_type?}]` |
| `reason` | ✅ | 部署原因 |
| `source` | ✅ | 來源標識 |
| `trust_scope` | ✅ | 穩定呼叫者 ID |

**Cache-Control 自動設定：**
- `index.html` → `no-cache, no-store, must-revalidate`
- `assets/*` → `max-age=31536000, immutable`
- 其他 → `no-cache`

**Limits:**
- 必須包含 `index.html`
- 每檔 10MB、總計 50MB
- 副檔名黑名單：`.sh .exe .py .jar .zip .tar .gz .7z .bat .ps1 .rb .war .bin .bash`
- 不支援 path traversal（`../`）

**審批按鈕：**
- `[✅ 批准部署]` — 自動 S3 copy + CloudFront invalidation
- `[❌ 拒絕]`

**已設定專案：**
| project | frontend_bucket | distribution_id |
|---------|----------------|-----------------|
| `ztp-files` | `ztp-files-dev-frontendbucket-nvvimv31xp3v` | `E176PW0SA5JF29` |

---

## SAM Deployer

### bouncer_deploy
```bash
mcporter call bouncer bouncer_deploy \
  project="bouncer" \
  reason="更新功能" \
  source="Private Bot (Bouncer deploy)"
```

**Response 包含：**
- `commit_sha` — 完整 commit hash
- `commit_short` — 7 字元短 hash（`🔖 abc1234 — commit message`）
- `commit_message` — commit 標題

**衝突（已有部署在跑）時回傳：**
```json
{
  "status": "conflict",
  "running_deploy_id": "deploy-xxx",
  "started_at": "2026-02-27T03:00:00Z",
  "estimated_remaining": "2 minutes",
  "hint": "Use bouncer_deploy_cancel to cancel the running deploy"
}
```

### bouncer_deploy_status / bouncer_deploy_cancel / bouncer_deploy_history / bouncer_project_list
```bash
mcporter call bouncer bouncer_deploy_status deploy_id="deploy-xxx"
mcporter call bouncer bouncer_deploy_cancel deploy_id="deploy-xxx"
mcporter call bouncer bouncer_deploy_history project="bouncer" limit=5
mcporter call bouncer bouncer_project_list
```

**⚠️ Deploy 狀態 Poll 規則（重要）：**
- ✅ **用 `bouncer_deploy_status`** 查部署進度 — 直接查 DDB，不發 Telegram 通知
- ✅ **一律 spawn sub-agent 追蹤 deploy**，主 session 繼續回應其他問題
- ✅ **只看 `status` 欄位**（`pending`/`RUNNING`/`SUCCESS`/`FAILED`）
- ❌ **不看 `phase` 欄位** — 整個 deploy 過程一直顯示 `INITIALIZING`，不準確（bug #53）
- ❌ **禁止用 `bouncer_execute + aws stepfunctions describe-execution`** — 每次執行都發一則自動通知，造成通知洗版
- ❌ **不知道前一個請求狀態，不能自己重發** — 5 分鐘 pending 先問 Steven，等確認才重發

**✅ v3.10.0 deploy_status 行為改善（#47）：**
- `deploy_id` 不存在時回傳 `{status: "pending"}` 而非 error（避免 race condition）
- RUNNING 時回傳 `elapsed_seconds`；SUCCESS/FAILED 時回傳 `duration_seconds`

---

## Account Management

### bouncer_list_accounts / bouncer_add_account / bouncer_remove_account
```bash
mcporter call bouncer bouncer_list_accounts
mcporter call bouncer bouncer_add_account account_id="111111111111" name="Production" role_arn="arn:aws:iam::111111111111:role/BouncerRole" source="Bot"
mcporter call bouncer bouncer_remove_account account_id="111111111111" source="Bot"
```

### AWS 帳號
| 帳號 | ID | 說明 |
|------|-----|------|
| 2nd (主帳號) | 190825685292 | 直接使用 Lambda execution role |
| Dev | 992382394211 | 透過 assume role `BouncerExecutionRole` |
| 1st | 841882238387 | 透過 assume role `BouncerExecutionRole` |

---

## Execution Error Tracking（v3.9.0）

命令執行失敗（exit code != 0）時，Bouncer 自動記錄到 DynamoDB，並在 MCP response 加上 `exit_code` 欄位。

**DDB 新增欄位（失敗時）：**
| 欄位 | 說明 |
|------|------|
| `status` | `executed_error` |
| `exit_code` | AWS CLI 退出碼（e.g. 255） |
| `error_output` | 錯誤輸出前 2000 字元 |
| `executed_at` | 執行完成時間（Unix timestamp） |

**MCP response 範例（失敗時）：**
```json
{
  "status": "auto_approved",
  "result": "❌ usage: aws [options] ...",
  "exit_code": 255
}
```

---

## Other Tools

### bouncer_get_page
當命令輸出超過 3500 字元自動分頁，用此 tool 取後續頁面。

```bash
mcporter call bouncer bouncer_get_page page_id="abc123:page:2"
```

### bouncer_list_safelist
列出命令分類規則。

---

## MCP Tools Quick Reference

| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_execute` | 執行 AWS CLI 命令 | 視命令而定 |
| `bouncer_status` | 查詢審批請求狀態 | 自動 |
| `bouncer_list_pending` | 列出待審批請求 | 自動 |
| `bouncer_list_accounts` | 列出 AWS 帳號 | 自動 |
| `bouncer_add_account` | 新增 AWS 帳號 | 需審批 |
| `bouncer_remove_account` | 移除 AWS 帳號 | 需審批 |
| `bouncer_upload` | 上傳單一檔案到 S3 | 需審批（信任可自動）|
| `bouncer_upload_batch` | 批量上傳多個檔案 | 需審批（信任可自動）|
| `bouncer_request_presigned` | 取得單檔 presigned PUT URL | 自動 |
| `bouncer_request_presigned_batch` | 取得批量 presigned PUT URL | 自動 |
| `bouncer_confirm_upload` | 驗證 presigned batch 上傳結果，確認 S3 files 存在 | 自動 |
| `bouncer_deploy_frontend` | 前端一鍵部署（staging→S3→CloudFront）| 需審批 |
| `bouncer_deploy` | 部署 SAM 專案 | 需審批 |
| `bouncer_deploy_status` | 查詢部署狀態 | 自動 |
| `bouncer_deploy_cancel` | 取消部署 | 自動 |
| `bouncer_deploy_history` | 查看部署歷史 | 自動 |
| `bouncer_project_list` | 列出可部署專案 | 自動 |
| `bouncer_request_grant` | 申請批次命令授權 | 需審批 |
| `bouncer_grant_execute` | 在授權內執行命令 | 自動 |
| `bouncer_grant_status` | 查詢授權狀態 | 自動 |
| `bouncer_trust_status` | 查詢信任時段 | 自動 |
| `bouncer_trust_revoke` | 撤銷信任時段 | 自動 |
| `bouncer_get_page` | 取分頁輸出 | 自動 |
| `bouncer_help` | 查詢命令說明 | 自動 |
| `bouncer_list_safelist` | 列出命令分類規則 | 自動 |

---

## Telegram Commands

在 Telegram 中可直接對 Bouncer bot 發送的指令：

| 指令 | 說明 |
|------|------|
| `/start` | 顯示歡迎訊息與基本說明 |
| `/help` | 顯示完整指令列表 |
| `/stats [hours]` | 查看 N 小時統計（預設 24h）。顯示：總請求數、各狀態分布、top sources/commands、approval rate、avg execution time |
| `/pending` | 列出待審批請求 |

### `/stats` 範例

```
/stats       → 顯示過去 24 小時統計
/stats 1     → 顯示過去 1 小時統計
/stats 168   → 顯示過去 7 天統計
```

**回傳欄位：**
- `total` — 總請求數
- `by_status` — 各狀態分布（approved / denied / pending / auto_approved）
- `approval_rate` — 人工審批通過率（%）
- `avg_execution_time_seconds` — 平均執行時間（已審批命令）
- `top_sources` — Top 5 來源
- `top_commands` — Top 5 命令類型

---

## ⚠️ Known Limitations

### Shell substitution 不展開
Bouncer 用 `aws_cli_split` 解析命令，**不走 bash**，所以 shell substitution `$(...)` 和變數 `$VAR` 不會被展開。

```bash
# ❌ 不行 — $(date +%s) 會被當成 literal 字串
aws cloudwatch get-metric-statistics --start-time $(date -u +%Y-%m-%dT%H:%M:%SZ)

# ✅ Agent 先算好值再 inline 進命令字串
import datetime
start = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
command = f"aws cloudwatch get-metric-statistics --start-time {start} ..."
```

直接在 terminal 跑 AWS CLI 時，bash 會先展開 `$(...)`；透過 Bouncer 沒有這一層展開。

---

## Command Classification

| Type | Behavior | Examples |
|------|----------|----------|
| **BLOCKED** | 永遠拒絕（含原因 + 建議） | `iam create-*`, `sts assume-role` |
| **DANGEROUS** | 特殊審批（⚠️ 高危警告） | `delete-bucket`, `terminate-instances` |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | 需要 Telegram 審批 | `start-*`, `stop-*`, `create-*` |

---

## 批次部署完整流程（bouncer-trust-batch-flow）

使用 `presigned_batch → confirm_upload → trust → grant` 達成多檔案部署，最小化審批次數。

### 前置說明

| 步驟 | 工具 | 說明 |
|------|------|------|
| 1 | `bouncer_presigned_batch` | 取得多個 S3 presigned URL（無需審批） |
| 2 | 直接 PUT（curl/SDK）| 用 presigned URL 上傳檔案到暫存 bucket |
| 3 | `bouncer_confirm_upload` | 確認上傳完成，建立 DynamoDB 請求記錄 |
| 4 | `bouncer_request_grant` | 申請批次 grant（列出所有部署命令，**一次審批**）|
| 5 | `bouncer_grant_execute` | 在 grant 內逐一執行命令（無需再次審批）|

### 完整 Bash 範例

```bash
# ─── Step 1: 取得 presigned URLs ───────────────────────────────────────────
BATCH=$(mcporter call bouncer bouncer_presigned_batch \
  files='[
    {"filename":"app.zip","content_type":"application/zip"},
    {"filename":"index.html","content_type":"text/html"}
  ]' \
  reason="部署 app v2.0" \
  source="Private Bot (batch-deploy)")

BATCH_ID=$(echo "$BATCH" | jq -r '.batch_id')
echo "batch_id: $BATCH_ID"

# ─── Step 2: 用 presigned URL 上傳（curl）──────────────────────────────────
APP_URL=$(echo "$BATCH" | jq -r '.presigned_urls[] | select(.filename=="app.zip") | .url')
curl -s -X PUT \
  -H "Content-Type: application/zip" \
  --data-binary @app.zip \
  "$APP_URL"

HTML_URL=$(echo "$BATCH" | jq -r '.presigned_urls[] | select(.filename=="index.html") | .url')
curl -s -X PUT \
  -H "Content-Type: text/html" \
  --data-binary @index.html \
  "$HTML_URL"

# ─── Step 3: 確認上傳完成 ──────────────────────────────────────────────────
mcporter call bouncer bouncer_confirm_upload \
  batch_id="$BATCH_ID" \
  source="Private Bot (batch-deploy)"

# ─── Step 4: 申請 grant session（一次審批所有命令）────────────────────────
GRANT=$(mcporter call bouncer bouncer_request_grant \
  commands='[
    "aws s3 cp s3://bouncer-uploads-190825685292/pending/app.zip s3://my-deploy-bucket/app.zip",
    "aws lambda update-function-code --function-name MyApp --s3-bucket my-deploy-bucket --s3-key app.zip",
    "aws cloudfront create-invalidation --distribution-id EXXXXX --paths /index.html"
  ]' \
  reason="部署 app v2.0" \
  source="Private Bot (batch-deploy)" \
  account_id="190825685292" \
  ttl_minutes=30)

GRANT_ID=$(echo "$GRANT" | jq -r '.grant_id')
echo "grant_id: $GRANT_ID"
# → Telegram 會收到審批請求，等待 Steven 批准

# ─── Step 5: grant 批准後，逐一執行（無需再審批）─────────────────────────
mcporter call bouncer bouncer_grant_execute \
  grant_id="$GRANT_ID" \
  command="aws s3 cp s3://bouncer-uploads-190825685292/pending/app.zip s3://my-deploy-bucket/app.zip"

mcporter call bouncer bouncer_grant_execute \
  grant_id="$GRANT_ID" \
  command="aws lambda update-function-code --function-name MyApp --s3-bucket my-deploy-bucket --s3-key app.zip"

mcporter call bouncer bouncer_grant_execute \
  grant_id="$GRANT_ID" \
  command="aws cloudfront create-invalidation --distribution-id EXXXXX --paths /index.html"
```

### 查詢 help

```bash
mcporter call bouncer bouncer_help command="batch-deploy"
```

---

## CloudFormation Stacks
- `clawdbot-bouncer` - 主要 Bouncer
- `bouncer-deployer` - SAM Deployer 基礎建設
