# AWS 安全審批執行系統

## 架構
```
Clawdbot ──POST──► Lambda ──► Telegram 審批 ──► 執行
   │                                              │
   └──────────── 看 Telegram 得到結果 ◄────────────┘
```

## 安全機制

| 層級 | 保護 |
|------|------|
| 請求驗證 | X-Approval-Secret header |
| 用戶驗證 | 只有指定 Chat ID 能審批 |
| 命令白名單 | Read-only 自動通過 |
| 命令黑名單 | IAM/危險操作永遠拒絕 |
| 超時 | 5 分鐘未審批自動過期 |
| 執行環境 | Lambda 隔離環境 |

## 部署步驟

### 1. 建立審批專用 Telegram Bot

```bash
# 找 @BotFather，發送：
/newbot
# 名稱：AWS Approval Bot
# 取得 token，例如：7123456789:AAxxxxxx
```

### 2. 生成 Request Secret

```bash
openssl rand -hex 24
# 例如：a1b2c3d4e5f6...
```

### 3. 部署

```bash
cd ~/clawd/aws-approval-system

# 建立 samconfig.toml（一次性）
cat > samconfig.toml << 'EOF'
version = 0.1
[default.deploy.parameters]
stack_name = "clawdbot-aws-approval"
resolve_s3 = true
s3_prefix = "clawdbot-aws-approval"
region = "us-east-1"
capabilities = "CAPABILITY_IAM"
parameter_overrides = "TelegramBotToken=你的TOKEN ApprovedChatId=999999999 RequestSecret=你的SECRET"
EOF

# 部署
sam build
sam deploy
```

### 4. 設定 Telegram Webhook

部署完成後會輸出 `FunctionUrl`，設定 webhook：

```bash
FUNCTION_URL="https://xxx.lambda-url.us-east-1.on.aws/"
BOT_TOKEN="你的TOKEN"

curl "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook?url=${FUNCTION_URL}webhook"
```

### 5. 測試

```bash
FUNCTION_URL="https://xxx.lambda-url.us-east-1.on.aws/"
SECRET="你的SECRET"

# 測試 read-only（自動通過）
curl -X POST "$FUNCTION_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $SECRET" \
  -d '{"command": "aws sts get-caller-identity", "reason": "測試"}'

# 測試需要審批的命令
curl -X POST "$FUNCTION_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $SECRET" \
  -d '{"command": "aws s3 cp test.txt s3://mybucket/", "reason": "上傳檔案"}'
```

## Clawdbot 整合

在 TOOLS.md 加入：

```markdown
## AWS 執行（需審批）

**Endpoint:** https://xxx.lambda-url.us-east-1.on.aws/
**Secret:** （存在 1Password）

**使用方式：**
curl -X POST "$AWS_APPROVAL_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $AWS_APPROVAL_SECRET" \
  -d '{"command": "aws ...", "reason": "說明"}'

**自動通過的命令：** describe, list, get 類的 read-only 操作
**需要審批：** create, update, put, delete 類的 write 操作
**永遠拒絕：** IAM 變更、assume-role、shell injection 相關
```

## 權限調整

編輯 `template.yaml` 中的 IAM Policy，按需增減權限。
