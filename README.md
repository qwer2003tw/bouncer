# Bouncer

> 🔐 AWS 命令審批執行系統 v3.86.0
> 
> 讓 AI Agent 安全執行 AWS 命令。危險命令透過 Telegram 審批後才執行。

## 架構

```
┌─────────────────────────────────────────────────────────────────┐
│  Clawdbot / OpenClaw Agent (EC2)                                │
│                                                                  │
│    mcporter call bouncer.bouncer_execute_native ...             │
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
# 部署 SAM 專案 (需要審批)
mcporter call bouncer.bouncer_deploy \
  project="bouncer" \
  reason="修復 bug"
```

### bouncer_execute_native（推薦）
使用 boto3 native 格式，直接調用 AWS SDK。完全不依賴 awscli。

**優點：**
- ✅ 無 awscli global flag 衝突問題
- ✅ 參數類型安全（int、bool、list 直接傳遞）
- ✅ 性能更好（無 subprocess overhead）
- ✅ 支援所有 boto3 API

**Input 結構：**
```json
{
  "aws": {
    "service": "eks",
    "operation": "create_cluster",
    "region": "us-east-1",
    "account": "992382394211",
    "params": {
      "name": "my-cluster",
      "version": "1.32",
      "roleArn": "arn:aws:iam::992382394211:role/eksClusterRole"
    }
  },
  "bouncer": {
    "reason": "建立 EKS cluster",
    "source": "Private Bot (EKS)",
    "trust_scope": "agent-bouncer-exec"
  }
}
```

**使用範例：**
```bash
# 建立 EKS cluster
mcporter call bouncer bouncer_execute_native --args '{
  "aws": {
    "service": "eks",
    "operation": "create_cluster",
    "region": "us-east-1",
    "account": "992382394211",
    "params": {
      "name": "my-cluster",
      "version": "1.32",
      "roleArn": "arn:aws:iam::992382394211:role/eksClusterRole",
      "resourcesVpcConfig": {
        "subnetIds": ["subnet-xxx", "subnet-yyy"]
      }
    }
  },
  "bouncer": {
    "reason": "建立 EKS cluster",
    "source": "Private Bot",
    "trust_scope": "agent-bouncer-exec"
  }
}'

# 列出 S3 buckets（SAFELIST - 自動執行）
mcporter call bouncer bouncer_execute_native --args '{
  "aws": {
    "service": "s3",
    "operation": "list_buckets",
    "region": "us-east-1"
  },
  "bouncer": {
    "reason": "檢查 S3 buckets",
    "source": "Private Bot"
  }
}'

# 啟動 EC2 instance（APPROVAL - 需要審批）
mcporter call bouncer bouncer_execute_native --args '{
  "aws": {
    "service": "ec2",
    "operation": "start_instances",
    "region": "us-east-1",
    "params": {
      "InstanceIds": ["i-0123456789abcdef0"]
    }
  },
  "bouncer": {
    "reason": "啟動開發環境",
    "source": "Private Bot"
  }
}'
```

**服務與操作對應：**
- `service`: boto3 service name（如 `eks`, `s3`, `ec2`, `iam`）
- `operation`: boto3 operation name（如 `create_cluster`, `list_buckets`, `start_instances`）
- 參考 [boto3 documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html) 查詢可用操作

## MCP Tools

> ⚠️ **注意：** `bouncer_execute` 已於 v3.70 移除（v3.65 標記為 deprecated），請使用 `bouncer_execute_native`。

### 核心功能
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_execute_native` | 執行 AWS API call（**boto3 native 格式**）| 視命令而定 |
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
| `bouncer_upload` | 上傳單一檔案到 S3（base64，< 500KB） | 需審批（信任期間可自動） |
| `bouncer_upload_batch` | 批量上傳多個小檔案（base64，< 500KB/檔）⚠️ deprecated | 需審批（信任期間可自動） |
| `bouncer_request_presigned` | 生成單檔 S3 presigned PUT URL，client 直傳，無大小限制 | **不需審批**（staging bucket）|
| `bouncer_request_presigned_batch` | 批量生成 N 個 presigned PUT URL，前端部署推薦用法 | **不需審批**（staging bucket）|
| `bouncer_deploy_frontend` | 前端一鍵部署：staging → 一次審批 → S3 copy + CloudFront invalidation | 需審批 |

> **前端部署推薦流程：** `bouncer_deploy_frontend` 一鍵完成（推薦）；或 `bouncer_request_presigned_batch` 取得 URL → PUT → `bouncer_execute s3 cp`（手動）

### CloudWatch Logs 查詢

| Tool | 說明 |
|------|------|
| `bouncer_query_logs` | 查詢 CloudWatch Logs（允許名單制 + fallback 審批） |
| `bouncer_logs_allowlist` | 管理允許查詢的 log group 名單（add/remove/list/add_batch） |

- 允許名單內 → 直接查詢回傳結果
- 不在允許名單 → Telegram 審批通知（一次性允許 / 加入允許名單 / 拒絕）
- 支援跨帳號（assume role）
- 相對時間：`-1h`、`-30m`、`-7d`、`now`
- Telegram 命令：`/logs [account]` 列出允許名單

### 信任時段 (Trust Session)
| Tool | 說明 | 審批 |
|------|------|------|
| `bouncer_approve_trust` | 開啟信任時段 | 由審批者觸發 |
| `bouncer_revoke_trust` | 撤銷信任時段 | 自動 |

> **v3.9.0 新增：** Trust session revoke/到期時，自動發 Telegram 摘要（執行命令清單、成功/失敗計數）。

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
