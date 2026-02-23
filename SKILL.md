---
name: bouncer
description: Execute AWS CLI commands with Telegram approval. Safe commands auto-execute, dangerous commands require human approval via Telegram.
metadata: {"openclaw": {"emoji": "🔐", "requires": {"bins": ["mcporter"]}}}
---

# Bouncer - AWS Command Approval System

Use `mcporter` to execute AWS CLI commands through the Bouncer approval system.

**API:** `https://YOUR_API_GATEWAY_URL/`
**GitHub:** https://github.com/qwer2003tw/bouncer
**MCP Source:** `/home/ec2-user/projects/bouncer/bouncer_mcp.py`

## 異步設計

所有需要審批的操作**預設異步**返回，避免 API Gateway 29 秒超時：

```bash
# 1. 發送請求（立即返回 request_id）
mcporter call bouncer bouncer_execute command="aws s3 mb s3://test" reason="建桶" source="Clawd"
# 返回: {"status": "pending_approval", "request_id": "abc123", ...}

# 2. 查詢結果
mcporter call bouncer bouncer_status request_id="abc123"
# 返回: {"status": "approved", "result": "..."} 或 {"status": "pending_approval"}
```

mcporter 會自動輪詢直到超時（預設 60 秒），所以一般使用時感覺像同步。

如需強制同步等待（不推薦），加 `sync=true`。

## Available Tools

### bouncer_execute
執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。

```bash
mcporter call bouncer bouncer_execute command="<aws command>" reason="<why>" source="<your name>"
```

**Parameters:**
- `command` (required): AWS CLI 命令（例如：`aws ec2 describe-instances`）
- `reason` (required): 執行原因，會顯示在審批請求中
- `source` (required): 來源標識，格式：`{Bot名稱} ({專案/任務})`
  - ✅ 好：`Private Bot (Bouncer - 部署修復)`
  - ✅ 好：`Public Bot (AgentCoreNexus - 建立 ECS)`
  - ❌ 差：`Private Bot`（太模糊，不知道在做什麼）
- `account` (optional): 目標 AWS 帳號 ID（12 位數字），不填使用預設帳號
- `sync` (optional): 同步模式，等待審批結果（預設 false，不推薦）

### bouncer_status
查詢審批請求狀態（用於異步模式輪詢）。

```bash
mcporter call bouncer bouncer_status request_id="<id>"
```

### bouncer_list_accounts
列出已配置的 AWS 帳號。

```bash
mcporter call bouncer bouncer_list_accounts
```

### bouncer_list_pending
列出待審批的請求。

```bash
mcporter call bouncer bouncer_list_pending
mcporter call bouncer bouncer_list_pending source="Steven's Private Bot"
mcporter call bouncer bouncer_list_pending limit=10
```

### bouncer_list_safelist
列出命令分類規則（哪些自動執行、哪些被封鎖）。

```bash
mcporter call bouncer bouncer_list_safelist
```

### bouncer_add_account
新增或更新 AWS 帳號配置（需要 Telegram 審批）。

```bash
mcporter call bouncer bouncer_add_account account_id="111111111111" name="Production" role_arn="arn:aws:iam::111111111111:role/BouncerRole" source="<your name>"
```

**Parameters:**
- `account_id` (required): 12 位 AWS 帳號 ID
- `name` (required): 帳號名稱（顯示用）
- `role_arn` (required): 該帳號的 BouncerRole ARN
- `upload_bucket` (optional): 自訂 upload 桶名（預設 `bouncer-uploads-{account_id}`）
- `source` (required): 來源標識

### bouncer_remove_account
移除 AWS 帳號配置（需要 Telegram 審批）。

```bash
mcporter call bouncer bouncer_remove_account account_id="111111111111" source="<your name>"
```

### bouncer_upload
上傳檔案到 S3 桶（需要 Telegram 審批）。檔案會上傳到 `bouncer-uploads-{account_id}` 桶，30 天後自動刪除。支援跨帳號上傳。

```bash
# 先把檔案 base64 encode
CONTENT=$(base64 -w0 template.yaml)

mcporter call bouncer bouncer_upload \
  filename="template.yaml" \
  content="$CONTENT" \
  content_type="text/yaml" \
  reason="上傳 CloudFormation template" \
  source="<your name>"
```

**Parameters:**
- `filename` (required): 檔案名稱（例如 `template.yaml`）
- `content` (required): 檔案內容（base64 encoded）
- `content_type` (optional): Content-Type（預設 `application/octet-stream`）
- `reason` (required): 上傳原因
- `source` (required): 來源標識
- `account` (optional): 目標 AWS 帳號 ID，上傳到該帳號的 `bouncer-uploads-{account_id}` 桶

**限制：** 檔案大小最大 4.5 MB（Lambda payload 限制）

**返回：**
```json
{
  "status": "approved",
  "s3_uri": "s3://bouncer-uploads-{account_id}/{source}/{timestamp}_{uuid}/{filename}",
  "s3_url": "https://bouncer-uploads-{account_id}.s3.amazonaws.com/..."
}
```

**特性：**
- 自動產生唯一路徑：`{date}/{request_id}/{filename}`
- 30 天 lifecycle 自動刪除
- 跨帳號讀取權限已設定（Dev/1st/AgentCoreNexusTest）
```

### bouncer_get_page
取得長輸出的下一頁。當命令輸出超過 3500 字元時會自動分頁。

```bash
mcporter call bouncer bouncer_get_page page_id="abc123:page:2"
```

**When to use:**
當 `bouncer_execute` 返回 `paged: true` 和 `next_page` 欄位時，用這個 tool 取得後續頁面。

---

## Trust Session Tools

Trust Session 讓你在審批時選擇「信任10分鐘」，期間同 source 的命令會自動批准（高危操作除外）。
信任時段自動批准時，Telegram 通知會顯示來源、剩餘時間和已執行命令數。

### bouncer_trust_status
查詢當前的信任時段狀態。

```bash
mcporter call bouncer bouncer_trust_status
mcporter call bouncer bouncer_trust_status source="Steven's Private Bot"
```

### bouncer_trust_revoke
撤銷信任時段。

```bash
mcporter call bouncer bouncer_trust_revoke trust_id="trust-xxx-yyy"
```

### Trust Session 規則
- 時長固定 10 分鐘
- 每個 source 最多 1 個活躍時段
- 每個時段最多 20 個命令
- **排除的高危服務**：iam, sts, organizations, kms, secretsmanager, cloudformation, cloudtrail
- **排除的高危操作**：delete-*, terminate-*, stop-*, modify-*, s3 rm, update-function-code 等
- **排除的危險旗標**：--force, --recursive, --skip-final-snapshot 等

---

## Grant Session Tools (批次權限授予)

Agent 可以預先申請一批命令的執行權限，經人工審批後在 TTL 內自動執行。

### bouncer_request_grant
```bash
mcporter call bouncer bouncer_request_grant \
  commands='["aws s3 ls s3://bucket", "aws ec2 describe-instances"]' \
  reason="部署前檢查" source="Private-Bot" ttl_minutes=30
```
- 每個命令會預檢 compliance、blocked、risk score
- 分類為 grantable / requires_individual / blocked
- Steven 收到 Telegram 訊息 + [全部批准] / [只批准安全的] / [拒絕]
- 回傳 `grant_request_id`

### bouncer_grant_status
```bash
mcporter call bouncer bouncer_grant_status grant_id="grant_xxx" source="Private-Bot"
```
- 查詢 grant 狀態、剩餘命令、剩餘時間

### bouncer_revoke_grant
```bash
mcporter call bouncer bouncer_revoke_grant grant_id="grant_xxx"
```

### 使用 Grant 執行命令
```bash
mcporter call bouncer bouncer_execute \
  command="aws s3 ls s3://bucket" grant_id="grant_xxx" \
  reason="部署前檢查" source="Private-Bot"
```
- 帶 `grant_id` 的命令會自動比對授權清單
- 匹配成功 → 自動執行（不需審批）
- 匹配失敗 → fallthrough 到正常審批流程

### Grant Session 規則
- **僅精確匹配**（normalized: 空白壓縮 + 小寫）
- TTL 最長 60 分鐘（預設 30）
- 每個 grant 最多 20 個命令
- 每個 grant 最多 50 次執行（含重複）
- TTL 從**批准時**算起
- 128-bit grant ID（`grant_` + 32 hex chars）
- Source + Account 綁定
- Compliance/Blocked 仍優先於 Grant 檢查
- 高危命令（TRUST_EXCLUDED_*）分類為 requires_individual

---

## SAM Deployer Tools

### bouncer_deploy
部署 SAM 專案（需要 Telegram 審批）。

```bash
mcporter call bouncer bouncer_deploy project="bouncer" reason="更新功能" source="<your name>"
```

**Parameters:**
- `project` (required): 專案 ID（例如：`bouncer`）
- `reason` (required): 部署原因
- `source` (required): 來源標識
- `branch` (optional): Git 分支（預設使用專案設定的分支）

**Note:** 跨帳號部署透過專案配置的 `target_account` 控制，不是呼叫時傳參。用 `bouncer_project_list` 查看專案配置。

### bouncer_deploy_status
查詢部署狀態。

```bash
mcporter call bouncer bouncer_deploy_status deploy_id="<id>"
```

### bouncer_deploy_cancel
取消進行中的部署。

```bash
mcporter call bouncer bouncer_deploy_cancel deploy_id="<id>"
```

### bouncer_deploy_history
查詢專案部署歷史。

```bash
mcporter call bouncer bouncer_deploy_history project="bouncer" limit=10
```

### bouncer_project_list
列出可部署的專案。

```bash
mcporter call bouncer bouncer_project_list
```

---

## Command Classification

| Type | Behavior | Examples |
|------|----------|----------|
| **BLOCKED** | 永遠拒絕 | `iam create-*`, `iam delete-*`, `sts assume-role` |
| **DANGEROUS** | 特殊審批（⚠️ 高危警告） | `delete-bucket`, `terminate-instances`, `delete-stack` |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | 需要 Telegram 審批 | `start-*`, `stop-*`, `create-*` |

### Telegram 審批按鈕

**一般命令：**
- `[✅ 批准]` - 只批准這一次
- `[🔓 信任10分鐘]` - 批准並啟動信任時段
- `[❌ 拒絕]`

**高危命令（DANGEROUS）：**
- `[⚠️ 確認執行]` - 確認執行（無信任選項）
- `[❌ 拒絕]`

---

## AWS 帳號

用 `bouncer_list_accounts` 查看當前設定的帳號。

Cross-account 透過 assume role 到目標帳號的 `BouncerRole` 執行。
新增帳號前需先在目標帳號部署 `target-account/template.yaml`。

---

## Examples

### 列出 S3 buckets（自動執行）
```bash
mcporter call bouncer bouncer_execute command="aws s3 ls" reason="檢查現有的 S3 buckets" source="Steven's Private Bot"
```

### 啟動 EC2 instance（需要審批）
```bash
mcporter call bouncer bouncer_execute command="aws ec2 start-instances --instance-ids i-xxx" reason="啟動開發環境" source="Steven's Private Bot"
```

### 在其他帳號執行
```bash
mcporter call bouncer bouncer_execute command="aws lambda list-functions" reason="檢查 Dev Lambda" account="222222222222" source="Steven's Private Bot"
```

### 部署 Bouncer
```bash
mcporter call bouncer bouncer_deploy project="bouncer" reason="修復 bug" source="Steven's Private Bot"
```

### 查看信任時段狀態
```bash
mcporter call bouncer bouncer_trust_status
```

### 查看待審批請求
```bash
mcporter call bouncer bouncer_list_pending
```

---

## Important Notes

1. **Always provide source** - 讓 Steven 知道是誰發的請求
2. **Always provide a clear reason** - 審批者會在 Telegram 看到
3. **Wait for response** - 需要審批的命令會 block 直到 approved/denied/timeout
4. **Multi-account** - 用 `account` 參數指定不同 AWS 帳號
5. **Trust Session** - 審批時選「信任10分鐘」可以加速後續操作

## CloudFormation Stacks
- `clawdbot-bouncer` - 主要 Bouncer
- `bouncer-deployer` - SAM Deployer 基礎建設
