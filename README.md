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

### 上傳
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_upload` | 上傳單一檔案到 S3 | 需審批（信任期間可自動） |
| `bouncer_upload_batch` | 批量上傳多個檔案 | 需審批（信任期間可自動） |

### 信任時段 (Trust Session)
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_approve_trust` | 開啟信任時段 | 由審批者觸發 |
| `bouncer_revoke_trust` | 撤銷信任時段 | 自動 |

### 批次授權 (Grant Session)
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_request_grant` | 申請批次命令授權 | 需審批 |
| `bouncer_grant_execute` | 在授權範圍內執行命令 | 自動（授權內） |
| `bouncer_grant_status` | 查詢授權狀態 | 自動 |

## 命令分類

| 分類 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 永遠拒絕 | `iam create-*`, `sts assume-role`, shell injection |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*`, `delete-*`, `create-*` |

## 部署

### 前置需求

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Telegram Bot Token](https://core.telegram.org/bots#how-do-i-create-a-bot)（透過 @BotFather 建立）
- Python 3.9+
- AWS 帳號，且有權限部署 CloudFormation

### Step 1: 部署 Bouncer Lambda

```bash
# Clone repo
git clone https://github.com/qwer2003tw/bouncer
cd bouncer

# 複製環境變數範本
cp .env.example .env
# 編輯 .env 填入你的值

# SAM 部署
sam build
sam deploy --guided \
  --parameter-overrides \
    TelegramBotToken=你的-bot-token \
    ApprovedChatId=你的-telegram-chat-id \
    RequestSecret=你自己設定的-secret \
    TelegramWebhookSecret=你自己設定的-webhook-secret
```

部署完成後會輸出 API Gateway URL，記下來。

### Step 2: 設定 Telegram Webhook

```bash
curl -X POST "https://api.telegram.org/bot你的-bot-token/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://你的-api-gateway-url/webhook", "secret_token": "你的-webhook-secret"}'
```

### Step 3: 新增 Cross-Account AWS 帳號（可選）

Bouncer 預設使用 Lambda 所在帳號的權限執行命令。如果你要讓 Bouncer 操作**其他 AWS 帳號**，需要在目標帳號建立 `BouncerExecutionRole`：

#### 3a. 在目標帳號建立 IAM Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::YOUR_BOUNCER_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "bouncer-cross-account"
        }
      }
    }
  ]
}
```

- `YOUR_BOUNCER_ACCOUNT_ID` = 部署 Bouncer Lambda 的 AWS 帳號 ID
- Role 名稱建議：`BouncerExecutionRole`
- 附上你需要的 AWS 權限（建議 PowerUserAccess 或更嚴格的自訂 policy）

#### 3b. 透過 Bouncer 註冊帳號

```bash
mcporter call bouncer.bouncer_add_account \
  account_id="目標帳號ID" \
  name="帳號別名" \
  role_arn="arn:aws:iam::目標帳號ID:role/BouncerExecutionRole" \
  source="你的Bot名稱"
```

此操作需要 Telegram 審批。審批通過後，就可以用 `account` 參數指定帳號執行命令：

```bash
mcporter call bouncer.bouncer_execute \
  command="aws s3 ls" \
  account="目標帳號ID" \
  reason="檢查目標帳號 S3" \
  source="你的Bot名稱"
```

### Step 4: 設定本地 MCP Client

在你的 AI Agent 機器上設定 `bouncer_mcp.py`：

```bash
# 設定環境變數
export BOUNCER_API_URL=https://你的-api-gateway-url
export BOUNCER_SECRET=你的-request-secret

# 或透過 mcporter 設定
mcporter config bouncer \
  --transport stdio \
  --command "python3 /path/to/bouncer_mcp.py" \
  --env BOUNCER_API_URL=https://你的-api-gateway-url \
  --env BOUNCER_SECRET=你的-request-secret
```

## 環境變數

| 變數 | 說明 | 必須 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | ✅ |
| `APPROVED_CHAT_ID` | 審批者 Telegram Chat ID（支援多個，逗號分隔） | ✅ |
| `REQUEST_SECRET` | API 認證 Secret | ✅ |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook 驗證 Secret | ✅ |
| `DEFAULT_ACCOUNT_ID` | 預設 AWS 帳號 ID | 建議 |
| `UPLOAD_BUCKET` | 上傳用 S3 Bucket 名稱 | 視需求 |
| `TRUSTED_ACCOUNT_IDS` | 合規檢查信任帳號（逗號分隔） | 視需求 |
| `BOUNCER_API_URL` | API Gateway URL（MCP Client 用） | MCP Client |
| `BOUNCER_SECRET` | API Secret（MCP Client 用） | MCP Client |

## Multi-Account 架構

```
┌──────────────────┐     sts:AssumeRole     ┌──────────────────┐
│  Bouncer 帳號     │ ───────────────────── → │  目標帳號 A       │
│  (Lambda 所在)    │                         │  BouncerExecRole │
│                   │     sts:AssumeRole     ├──────────────────┤
│  直接用 Lambda    │ ───────────────────── → │  目標帳號 B       │
│  execution role   │                         │  BouncerExecRole │
└──────────────────┘                         └──────────────────┘
```

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
# 安裝依賴
pip install -r src/requirements.txt
pip install pytest

# 測試
pytest tests/ -v

# 部署
sam build && sam deploy
```

## 安全設計

- **命令分類**：每個 AWS CLI 命令在執行前都會被分類為 BLOCKED / SAFELIST / APPROVAL
- **合規檢查**：自動檢測 IAM 濫用、資源公開存取、跨帳號信任等違規操作
- **風險評分**：多維度評估命令風險（動詞、參數、帳號敏感度）
- **信任 Session**：審批者可啟用短期自動批准（高危操作除外）
- **IAM 限制**：Lambda execution role 有 Deny 規則阻擋危險 IAM 操作
- **審計追蹤**：所有命令執行記錄存在 DynamoDB

## License

MIT
