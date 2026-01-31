# Bouncer - 執行計畫

> **最後更新:** 2026-01-31 12:21 UTC
> **版本:** v1.2.0
> **狀態:** 程式碼完成，待部署

---

## 🎯 核心設計

**Clawdbot 主機零 AWS 權限，所有命令由 Bouncer Lambda 執行**

```
┌─────────────────────────────────────────────────────────────────┐
│                    Clawdbot 主機                                 │
│                   （零 AWS 權限）                                │
│                                                                  │
│  用戶: "幫我開 EC2 i-123"                                        │
│           │                                                      │
│           ▼                                                      │
│  POST /submit {"command": "aws ec2 start-instances ...",        │
│                "wait": true}                                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Bouncer Lambda                              │
│                    （有 AWS 權限）                               │
│                                                                  │
│  1. 驗證 Secret                                                  │
│  2. 命令分類：                                                   │
│     ├─ BLOCKED (iam create/delete, 注入) → 403 拒絕             │
│     ├─ SAFELIST (describe/list/get) → 直接執行，返回結果        │
│     └─ 其他 → 發 Telegram 審批                                   │
│  3. 等待審批（最長 50 秒）                                       │
│  4. 審批通過 → 執行命令 → 返回結果                               │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram                                  │
│                                                                  │
│  🔐 AWS 命令審批請求                                             │
│  📋 命令: aws ec2 start-instances --instance-ids i-123          │
│                                                                  │
│  [✅ 批准]  [❌ 拒絕]                                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔐 安全機制

### 四層命令分類

| 層級 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 直接拒絕 403 | `iam create-*`, `sts assume-role`, shell 注入 |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*`, `sts get-caller-identity` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*`, `delete-*`, `put-*` |
| **DEFAULT** | 視同 APPROVAL | 未分類的命令 |

### 防重複執行

```python
# Telegram webhook 處理
if item['status'] != 'pending_approval':
    answer_callback("❌ 此請求已處理過")
    return  # 不會重複執行
```

### 安全執行

```python
# shell=False 防止注入
args = shlex.split(command)
subprocess.run(args, shell=False, ...)
```

---

## 📋 部署步驟

### Step 1: 前置準備

```bash
# 1. 建立 Telegram Bot
# 到 @BotFather 執行 /newbot，取得 Token

# 2. 產生 Secrets
export REQUEST_SECRET=$(openssl rand -hex 16)
export WEBHOOK_SECRET=$(openssl rand -hex 16)

# 3. 記錄到 1Password（建議）
```

### Step 2: 部署 Lambda

```bash
cd ~/projects/bouncer

# 建置
sam build

# 部署
sam deploy --guided \
  --stack-name clawdbot-bouncer \
  --parameter-overrides \
    TelegramBotToken=$BOT_TOKEN \
    RequestSecret=$REQUEST_SECRET \
    TelegramWebhookSecret=$WEBHOOK_SECRET \
    ApprovedChatId=999999999
```

### Step 3: 設定 Telegram Webhook

```bash
# 取得 Function URL
FUNCTION_URL=$(aws cloudformation describe-stacks \
  --stack-name clawdbot-bouncer \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' \
  --output text)

# 設定 Webhook
curl "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${FUNCTION_URL}webhook" \
  -d "secret_token=${WEBHOOK_SECRET}"
```

### Step 4: 移除 Clawdbot 主機 AWS 權限

```bash
# 在 Clawdbot 主機上
rm -rf ~/.aws/credentials ~/.aws/config

# 或如果用 EC2 Instance Role，移除 Role
# 確認 aws 命令無法執行
aws sts get-caller-identity  # 應該失敗
```

### Step 5: 測試

```bash
# SAFELIST 命令（自動執行）
curl -X POST "$FUNCTION_URL" \
  -H "X-Approval-Secret: $REQUEST_SECRET" \
  -d '{"command": "aws sts get-caller-identity"}'

# APPROVAL 命令（需審批）
curl -X POST "$FUNCTION_URL" \
  -H "X-Approval-Secret: $REQUEST_SECRET" \
  -d '{"command": "aws ec2 start-instances --instance-ids i-xxx", "wait": true}'
```

---

## 🔧 Clawdbot 整合

### TOOLS.md 新增內容

```markdown
## 🔐 Bouncer - AWS 命令執行

**本主機無 AWS 權限，所有 AWS 命令必須透過 Bouncer**

### URL
`https://xxxxxxxxxx.lambda-url.us-east-1.on.aws/`

### 使用方式

curl -X POST "$BOUNCER_URL" \
  -H "X-Approval-Secret: $BOUNCER_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "aws ec2 describe-instances",
    "reason": "用戶要求查看 EC2 狀態",
    "wait": true
  }'

### 回應格式

| status | 說明 |
|--------|------|
| `auto_approved` | SAFELIST 命令，已執行 |
| `approved` | 審批通過，已執行 |
| `denied` | 審批拒絕 |
| `blocked` | 危險命令，直接拒絕 |
| `pending_approval` | 等待審批中（wait=false 時） |

### ⚠️ 重要

- 不要嘗試直接執行 `aws` 命令（會失敗，主機無權限）
- 所有 AWS 操作必須透過此 API
```

---

## 📊 成本估算

| 項目 | 用量 | 成本 |
|------|------|------|
| Lambda | <1M requests/月 | $0 (Free Tier) |
| DynamoDB | <25 WCU/RCU | $0 (Free Tier) |
| CloudWatch | 基本日誌 | $0 |
| **總計** | | **$0/月** |

---

## 📁 專案檔案

```
~/projects/bouncer/
├── README.md              # 專案簡介
├── PLAN.md                # 執行計畫（本檔案）
├── HANDOFF.md             # 交接文件
├── QA_REPORT.md           # QA 報告
├── TOOLS_TEMPLATE.md      # Clawdbot 整合模板
├── INTEGRATED_PLAN.md     # 設計分析
├── template.yaml          # SAM 部署模板
├── pytest.ini
├── .venv/                 # Python 虛擬環境
├── src/
│   └── app.py             # Lambda v1.2.0 (62 tests, 89% cov)
├── tests/
│   └── test_bouncer.py    # pytest 測試
└── test_local.py          # 簡易本地測試
```

---

## ✅ 待提供

| 項目 | 來源 | 狀態 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | @BotFather | ⏳ 待建立 |
| `REQUEST_SECRET` | `openssl rand -hex 16` | ⏳ 待產生 |
| `TELEGRAM_WEBHOOK_SECRET` | `openssl rand -hex 16` | ⏳ 待產生 |

---

*Bouncer v1.2.0 | 最後更新: 2026-01-31 12:21 UTC*
