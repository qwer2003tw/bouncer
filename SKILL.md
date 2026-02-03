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

## Available Tools

### bouncer_execute
執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。

```bash
mcporter call bouncer bouncer_execute command="<aws command>" reason="<why>" source="<your name>"
```

**Parameters:**
- `command` (required): AWS CLI 命令（例如：`aws ec2 describe-instances`）
- `reason` (required): 執行原因，會顯示在審批請求中
- `source` (required): 來源標識（例如：`Steven's Private Bot`）
- `account` (optional): 目標 AWS 帳號 ID（12 位數字），不填使用預設帳號
- `timeout` (optional): 審批等待超時秒數（預設 300）

### bouncer_status
查詢審批請求狀態。

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

### bouncer_remove_account
移除 AWS 帳號配置（需要 Telegram 審批）。

```bash
mcporter call bouncer bouncer_remove_account account_id="111111111111" source="<your name>"
```

### bouncer_upload
上傳檔案到固定 S3 桶（需要 Telegram 審批）。檔案會上傳到集中管理的 `bouncer-uploads-111111111111` 桶，30 天後自動刪除。

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

**限制：** 檔案大小最大 4.5 MB（Lambda payload 限制）

**返回：**
```json
{
  "status": "approved",
  "s3_uri": "s3://bouncer-uploads-111111111111/Clawd/20260203_121500_abc123/template.yaml",
  "s3_url": "https://bouncer-uploads-111111111111.s3.amazonaws.com/Clawd/20260203_121500_abc123/template.yaml"
}
```

**特性：**
- 自動產生唯一路徑：`{source}/{timestamp}_{uuid}/{filename}`
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

| 帳號 | ID | 說明 |
|------|-----|------|
| 2nd (主帳號) | 111111111111 | 直接使用 Lambda execution role |
| Dev | 222222222222 | 透過 assume role `BouncerExecutionRole` |
| 1st | 333333333333 | 透過 assume role `BouncerExecutionRole` |

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
