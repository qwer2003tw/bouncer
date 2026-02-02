# Bouncer

> 🔐 AWS 命令審批執行系統 v2.0
> 
> 讓 AI Agent 安全執行 AWS 命令。危險命令透過 Telegram 審批後才執行。

## 架構

```
┌─────────────────────────────────────────────────────────────────┐
│  Clawdbot / OpenClaw Agent (EC2)                                │
│                                                                  │
│    mcporter call bouncer.bouncer_execute ...                    │
│         │                                                        │
│         │ stdio (MCP Protocol)                                   │
│         ▼                                                        │
│    bouncer_mcp.py (本地 MCP Server)                             │
│         │                                                        │
│         │ HTTPS                                                  │
│         ▼                                                        │
└─────────┼───────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│  AWS Lambda (API Gateway)                                        │
│  https://YOUR_API_GATEWAY_URL    │
│                                                                  │
│  1. 驗證請求                                                     │
│  2. 命令分類 (BLOCKED / SAFELIST / APPROVAL)                    │
│  3. SAFELIST → 直接執行                                         │
│  4. APPROVAL → 發 Telegram 審批                                 │
│  5. 回傳結果                                                     │
└─────────────────────────────────────────────────────────────────┘
          │
          │ Telegram API
          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Steven (Telegram)                                               │
│                                                                  │
│  🔐 AWS 命令審批請求                                             │
│  📋 aws ec2 start-instances --instance-ids i-xxx                │
│  📝 原因: 啟動開發環境                                          │
│  👤 來源: Steven's Private Bot                                  │
│                                                                  │
│  [✅ 批准]  [❌ 拒絕]                                            │
└─────────────────────────────────────────────────────────────────┘
```

## 使用方式

透過 `mcporter` 呼叫：

```bash
# 列出 S3 buckets (SAFELIST - 自動執行)
mcporter call bouncer.bouncer_execute \
  command="aws s3 ls" \
  reason="檢查 S3" \
  source="Steven's Private Bot"

# 啟動 EC2 (APPROVAL - 需要審批)
mcporter call bouncer.bouncer_execute \
  command="aws ec2 start-instances --instance-ids i-xxx" \
  reason="啟動開發環境" \
  source="Steven's Private Bot"

# 部署 SAM 專案 (需要審批)
mcporter call bouncer.bouncer_deploy \
  project="bouncer" \
  reason="修復 bug"
```

## MCP Tools

### 核心功能
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_execute` | 執行 AWS CLI 命令 | 視命令而定 |
| `bouncer_status` | 查詢審批請求狀態 | 自動 |

### 帳號管理
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_list_accounts` | 列出 AWS 帳號 | 自動 |
| `bouncer_add_account` | 新增 AWS 帳號 | 需審批 |
| `bouncer_remove_account` | 移除 AWS 帳號 | 需審批 |

### SAM Deployer
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_deploy` | 部署 SAM 專案 | 需審批 |
| `bouncer_deploy_status` | 查詢部署狀態 | 自動 |
| `bouncer_deploy_cancel` | 取消部署 | 自動 |
| `bouncer_deploy_history` | 查看部署歷史 | 自動 |
| `bouncer_project_list` | 列出可部署專案 | 自動 |

## 命令分類

| 分類 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 永遠拒絕 | `iam create-*`, `sts assume-role`, shell injection |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*`, `delete-*`, `create-*` |

## AWS 帳號

| 名稱 | ID | 說明 |
|------|-----|------|
| 2nd (主帳號) | 111111111111 | Lambda execution role |
| Dev | 222222222222 | assume role `BouncerExecutionRole` |
| 1st | 333333333333 | assume role `BouncerExecutionRole` |

## 專案結構

```
bouncer/
├── bouncer_mcp.py        # MCP Server (本地，透過 mcporter 呼叫)
├── src/                   # Lambda 程式碼 (審批 + 執行)
├── deployer/              # SAM Deployer (CodeBuild + Step Functions)
├── mcp_server/            # [舊] 本地 MCP Server 版本 (未使用)
├── template.yaml          # SAM 部署模板
└── SKILL.md               # OpenClaw Skill 文件
```

## CloudFormation Stacks

| Stack | 說明 |
|-------|------|
| `clawdbot-bouncer` | 主要 Bouncer (Lambda + API Gateway + DynamoDB) |
| `bouncer-deployer` | SAM Deployer (CodeBuild + Step Functions) |

## 開發

```bash
# 測試
cd ~/projects/bouncer
source .venv/bin/activate
pytest tests/ -v

# 部署 (透過 Bouncer 自己)
mcporter call bouncer.bouncer_deploy project="bouncer" reason="更新功能"
```

## 相關連結

- **API**: `https://YOUR_API_GATEWAY_URL/`
- **GitHub**: https://github.com/qwer2003tw/bouncer
