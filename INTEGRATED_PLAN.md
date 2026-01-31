# Bouncer - 整合實作計畫

> **整合自三份子代理報告：** Security Analyst + Solutions Architect + Pragmatic Engineer
> **最後更新：** 2026-01-31 11:49 UTC
> **狀態：** ✅ Implementation Complete

---

## 🎯 執行摘要

| 面向 | 決策 |
|------|------|
| **實作路徑** | 兩階段：MVP → Production |
| **MVP 部署時間** | 10-15 分鐘 |
| **MVP 成本** | $0（Free Tier） |
| **Production 成本** | < $1/月 |

---

## 📐 架構決策（三份報告共識）

### ✅ 採用
| 組件 | 選擇 | 理由 |
|------|------|------|
| **API 入口** | Lambda Function URL | 免費、低延遲、無 API Gateway 開銷 |
| **資料庫** | DynamoDB On-Demand | Free Tier 25GB，TTL 自動清理 |
| **IaC** | AWS SAM | Serverless 專用，本地測試方便 |
| **審批通道** | Telegram Bot (Inline Buttons) | 你已有，回調即時 |
| **執行隔離** | Lambda 環境 | 與 Clawdbot 完全分離 |

### ❌ 不採用
| 組件 | 理由 |
|------|------|
| API Gateway | 額外成本，Function URL 已足夠 |
| Step Functions | 過度工程，簡單狀態機不需要 |
| SQS | 批量審批目前不需要 |

---

## 🛡️ 安全設計（Security Analyst 建議整合）

### 命令分類系統（四層）

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: BLOCKED        永遠拒絕，立即返回 403             │
│  ├─ iam create/delete/attach/detach/put/update              │
│  ├─ sts assume-role                                         │
│  ├─ organizations *                                          │
│  └─ Shell 注入符號: ; | && ` $( rm sudo                     │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: SAFELIST       自動批准，直接執行                 │
│  ├─ ec2 describe-*                                          │
│  ├─ s3 ls, s3api list-*                                     │
│  ├─ rds/lambda/logs/cloudwatch describe/list/get            │
│  ├─ iam list-*, iam get-* (read-only)                       │
│  └─ sts get-caller-identity                                 │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: APPROVAL       需要人工審批                       │
│  ├─ ec2 start/stop-instances                                │
│  ├─ s3 cp (upload), s3 rm                                   │
│  ├─ lambda update-*                                         │
│  └─ 其他非 blocked 非 safelist 的命令                       │
├─────────────────────────────────────────────────────────────┤
│  Layer 4: DEFAULT DENY   未知命令，拒絕                     │
└─────────────────────────────────────────────────────────────┘
```

### 防護機制

| 威脅 | 防護 | 實現 |
|------|------|------|
| **請求偽造** | X-Approval-Secret header | 每個請求驗證 |
| **Webhook 偽造** | X-Telegram-Bot-Api-Secret-Token | Telegram 內建機制 |
| **重放攻擊** | request_id + TTL (5min) | DynamoDB TTL 自動清理 |
| **審批疲勞** | 限流 + 清晰命令顯示 | 一目了然的 Telegram 消息 |
| **用戶偽造** | Chat ID 白名單 | 只有 999999999 能審批 |
| **命令注入** | Blocked prefixes | Shell 特殊字符全擋 |

---

## 🚀 兩階段實作計畫

### Phase 1: MVP（今天可完成）

**目標：** 能用就好，10 分鐘內跑起來

```
Clawdbot ──► Lambda (Function URL) ──► Telegram 審批
                    │                        │
                    └── DynamoDB ◄───────────┘
                         (存請求)       (callback)
```

**部署步驟：**

```bash
# 1. 建立 Telegram Bot（你來做）
#    @BotFather → /newbot → 取得 Token

# 2. 建立 secrets（你來做）
#    REQUEST_SECRET: 隨機字串，Clawdbot 呼叫時帶上
#    TELEGRAM_WEBHOOK_SECRET: 隨機字串，防偽造 webhook

# 3. 部署（我來做）
cd ~/projects/bouncer
sam build
sam deploy --guided \
  --parameter-overrides \
    TelegramBotToken=<BOT_TOKEN> \
    RequestSecret=<YOUR_SECRET> \
    TelegramWebhookSecret=<WEBHOOK_SECRET>

# 4. 設定 Telegram Webhook（我來做）
FUNCTION_URL=$(aws cloudformation describe-stacks \
  --stack-name clawdbot-aws-approval \
  --query 'Stacks[0].Outputs[?OutputKey==`WebhookUrl`].OutputValue' \
  --output text)

curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=${FUNCTION_URL}&secret_token=<WEBHOOK_SECRET>"

# 5. 測試
curl -X POST "${FUNCTION_URL}" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: <YOUR_SECRET>" \
  -d '{"command": "aws sts get-caller-identity", "reason": "test"}'
```

### Phase 2: Production Hardening（之後再做）

| 加強項目 | 做法 | 優先級 |
|----------|------|--------|
| HMAC 請求簽章 | 防篡改 + 防重放 | P2 |
| CloudWatch Alarms | 異常請求量告警 | P2 |
| 批量審批 | 同類命令合併 | P3 |
| 結果回調 | Clawdbot 主動查詢結果 | P2 |
| 審計 Dashboard | QuickSight / Grafana | P3 |

---

## 📁 現有程式碼評估

### `src/app.py` - 評分：✅ 可用（8/10）

**優點：**
- 四層命令分類已實現
- Telegram callback 處理完整
- TTL 過期機制
- 基本錯誤處理

**待改進（Phase 2）：**
- [ ] 加 HMAC 簽章驗證
- [ ] 加 nonce 防重放
- [ ] 結果超過 4000 字時截斷處理
- [ ] 加 structured logging

### `template.yaml` - 評分：✅ 可用（9/10）

**優點：**
- SAM 標準模板
- Function URL 正確配置
- IAM 最小權限設計
- DynamoDB TTL 已啟用

**待改進：**
- [ ] 加 CloudWatch Alarm
- [ ] 考慮 VPC 內執行（如果要存取 private 資源）

---

## 🔧 Clawdbot 整合

部署完成後，更新 `TOOLS.md`：

```markdown
## 🔐 AWS Bouncer (Approval System)

**Endpoint:** https://xxx.lambda-url.us-east-1.on.aws/
**認證:** X-Approval-Secret header

**使用方式：**
\`\`\`bash
curl -X POST "$BOUNCER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $BOUNCER_SECRET" \
  -d '{"command": "aws ec2 describe-instances", "reason": "檢查 EC2 狀態"}'
\`\`\`

**回應類型：**
- `auto_approved` - 已自動執行（read-only 命令）
- `pending_approval` - 等待 Telegram 確認
- `blocked` - 命令被拒絕（黑名單）

**查詢結果：**
\`\`\`bash
curl "$BOUNCER_URL/status/<request_id>" \
  -H "X-Approval-Secret: $BOUNCER_SECRET"
\`\`\`
```

---

## ✅ 下一步 Action Items

### 你需要做的：
1. **建立 Telegram Bot**
   - @BotFather → /newbot
   - 名稱建議：`Bouncer` 或 `AWS Approval`
   - 給我 Bot Token

2. **產生兩個 Secret**
   ```bash
   # REQUEST_SECRET（Clawdbot 呼叫用）
   openssl rand -hex 16
   
   # TELEGRAM_WEBHOOK_SECRET（防偽造 webhook）
   openssl rand -hex 16
   ```

3. **確認 AWS 部署帳號**
   - 我需要臨時 Access Key 來部署
   - 或者你自己跑 `sam deploy`

### 我會做的：
1. 等你提供 Token + Secrets
2. 執行 `sam build && sam deploy`
3. 設定 Telegram Webhook
4. 端到端測試
5. 更新 TOOLS.md 整合說明

---

## 📊 成本分析（最終版）

| 組件 | 月用量假設 | 成本 |
|------|-----------|------|
| Lambda | 1000 invocations × 500ms | $0.00 (Free Tier) |
| DynamoDB | < 1GB, 1000 reads/writes | $0.00 (Free Tier) |
| Function URL | 無額外成本 | $0.00 |
| CloudWatch Logs | 5GB | $0.00 (Free Tier) |
| **總計** | | **$0.00** |

---

*整合完成：2026-01-31*
*下一步：等待 Bot Token + Secrets*
