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
mcporter call bouncer.bouncer_execute command="<aws command>" reason="<why>" source="<your name>"
```

**Parameters:**
- `command` (required): AWS CLI 命令（例如：`aws ec2 describe-instances`）
- `reason` (required): 執行原因，會顯示在審批請求中
- `source` (optional): 來源標識（例如：`Steven's Private Bot`）
- `account` (optional): 目標 AWS 帳號 ID（12 位數字），不填使用預設帳號
- `timeout` (optional): 審批等待超時秒數（預設 300）

### bouncer_status
查詢審批請求狀態。

```bash
mcporter call bouncer.bouncer_status request_id="<id>"
```

### bouncer_list_accounts
列出已配置的 AWS 帳號。

```bash
mcporter call bouncer.bouncer_list_accounts
```

### bouncer_add_account
新增或更新 AWS 帳號配置（需要 Telegram 審批）。

```bash
mcporter call bouncer.bouncer_add_account account_id="111111111111" name="Production" role_arn="arn:aws:iam::111111111111:role/BouncerRole" source="<your name>"
```

### bouncer_remove_account
移除 AWS 帳號配置（需要 Telegram 審批）。

```bash
mcporter call bouncer.bouncer_remove_account account_id="111111111111" source="<your name>"
```

---

## SAM Deployer Tools

### bouncer_deploy
部署 SAM 專案（需要 Telegram 審批）。

```bash
mcporter call bouncer.bouncer_deploy project="bouncer" reason="更新功能" branch="main"
```

**Parameters:**
- `project` (required): 專案 ID（例如：`bouncer`）
- `reason` (required): 部署原因
- `branch` (optional): Git 分支（預設使用專案設定的分支）

### bouncer_deploy_status
查詢部署狀態。

```bash
mcporter call bouncer.bouncer_deploy_status deploy_id="<id>"
```

### bouncer_deploy_cancel
取消進行中的部署。

```bash
mcporter call bouncer.bouncer_deploy_cancel deploy_id="<id>"
```

### bouncer_deploy_history
查詢專案部署歷史。

```bash
mcporter call bouncer.bouncer_deploy_history project="bouncer" limit=10
```

### bouncer_project_list
列出可部署的專案。

```bash
mcporter call bouncer.bouncer_project_list
```

---

## Command Classification

| Type | Behavior | Examples |
|------|----------|----------|
| **BLOCKED** | 永遠拒絕 | `iam create-*`, shell injection |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | 需要 Telegram 審批 | `start-*`, `stop-*`, `delete-*`, `create-*` |

## AWS 帳號

| 帳號 | ID | 說明 |
|------|-----|------|
| 2nd (主帳號) | 111111111111 | 直接使用 Lambda execution role |
| Dev | 222222222222 | 透過 assume role `BouncerExecutionRole` |
| 1st | 333333333333 | 透過 assume role `BouncerExecutionRole` |

## Examples

### 列出 S3 buckets（自動執行）
```bash
mcporter call bouncer.bouncer_execute command="aws s3 ls" reason="檢查現有的 S3 buckets" source="Steven's Private Bot"
```

### 啟動 EC2 instance（需要審批）
```bash
mcporter call bouncer.bouncer_execute command="aws ec2 start-instances --instance-ids i-xxx" reason="啟動開發環境" source="Steven's Private Bot"
```

### 在其他帳號執行
```bash
mcporter call bouncer.bouncer_execute command="aws lambda list-functions" reason="檢查 Dev Lambda" account="222222222222" source="Steven's Private Bot"
```

### 部署 Bouncer
```bash
mcporter call bouncer.bouncer_deploy project="bouncer" reason="修復 bug" source="Steven's Private Bot"
```

## Important Notes

1. **Always provide source** - 讓 Steven 知道是誰發的請求
2. **Always provide a clear reason** - 審批者會在 Telegram 看到
3. **Wait for response** - 需要審批的命令會 block 直到 approved/denied/timeout
4. **Multi-account** - 用 `account` 參數指定不同 AWS 帳號

## CloudFormation Stacks
- `clawdbot-bouncer` - 主要 Bouncer
- `bouncer-deployer` - SAM Deployer 基礎建設
