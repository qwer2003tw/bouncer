# Bouncer - 執行計畫

> **版本:** 1.1.0
> **最後更新:** 2026-01-31 11:49 UTC
> **狀態:** 待部署

---

## 📋 專案概述

**目的：** 讓 Clawdbot 能安全執行 AWS 命令，透過獨立審批機制防止 Prompt Injection 攻擊。

**核心原則：** 
- Clawdbot 只能「申請」，不能「執行」
- 執行權在獨立的 Lambda，需要人工 Telegram 確認
- 零信任：Clawdbot 被視為「可能被劫持的實體」

---

## 🏗️ 架構

```
┌──────────────────────────────────────────────────────────────────┐
│                         AWS Cloud                                 │
│                                                                   │
│  Clawdbot ──POST──► Lambda (Function URL)                        │
│     │                     │                                       │
│     │                     ├─► DynamoDB (存請求，TTL 5min)         │
│     │                     └─► Telegram Bot (發審批)               │
│     │                              │                              │
│     │                        Steven 點擊批准/拒絕                  │
│     │                              │                              │
│     │                     Lambda 執行命令                         │
│     │                              │                              │
│     └◄── /status/{id} ◄───────────┘                              │
│          或 wait=true 長輪詢                                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 🔐 安全機制

### 命令分類（四層）

| 層級 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 永遠拒絕 | `iam create*`, `sts assume-role`, Shell 注入 |
| **SAFELIST** | 自動批准 | `ec2 describe-*`, `s3 ls`, `logs filter-*` |
| **APPROVAL** | 人工審批 | `ec2 start-instances`, `s3 cp`, `lambda update-*` |
| **DEFAULT DENY** | 未知拒絕 | 不在上述任何分類的命令 |

### 防護機制

| 威脅 | 防護 |
|------|------|
| 請求偽造 | X-Approval-Secret header |
| Webhook 偽造 | X-Telegram-Bot-Api-Secret-Token |
| 重放攻擊 | request_id + TTL (5min) |
| 用戶偽造 | Chat ID 白名單 (999999999) |
| 命令注入 | BLOCKED_PATTERNS |

---

## 📦 專案結構

```
~/projects/bouncer/
├── README.md           # 專案簡介
├── PLAN.md             # 本檔案 - 執行計畫
├── HANDOFF.md          # 交接文件 - 未完成項目
├── INTEGRATED_PLAN.md  # 三份報告整合（參考用）
├── template.yaml       # AWS SAM 部署模板
└── src/
    └── app.py          # Lambda 程式碼 (v1.1.0)
```

---

## 🚀 部署步驟

### 前置準備（人工）

- [ ] **Step 1:** 建立 Telegram Bot
  ```
  1. 開啟 Telegram，找 @BotFather
  2. 發送 /newbot
  3. 設定名稱（建議：Bouncer 或 AWS Approval）
  4. 取得 Bot Token（格式：123456789:ABC...）
  ```

- [ ] **Step 2:** 產生 Secrets
  ```bash
  # REQUEST_SECRET（Clawdbot 呼叫時驗證用）
  openssl rand -hex 16
  
  # TELEGRAM_WEBHOOK_SECRET（防偽造 webhook）
  openssl rand -hex 16
  ```

### 部署執行（自動）

- [ ] **Step 3:** SAM 部署
  ```bash
  cd ~/projects/bouncer
  sam build
  sam deploy --guided \
    --stack-name clawdbot-aws-approval \
    --parameter-overrides \
      TelegramBotToken=<BOT_TOKEN> \
      RequestSecret=<REQUEST_SECRET> \
      TelegramWebhookSecret=<WEBHOOK_SECRET>
  ```

- [ ] **Step 4:** 設定 Telegram Webhook
  ```bash
  # 取得 Function URL
  FUNCTION_URL=$(aws cloudformation describe-stacks \
    --stack-name clawdbot-aws-approval \
    --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' \
    --output text)
  
  # 設定 webhook
  curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=${FUNCTION_URL}webhook&secret_token=<WEBHOOK_SECRET>"
  ```

- [ ] **Step 5:** 驗證
  ```bash
  # 測試自動批准（read-only）
  curl -X POST "${FUNCTION_URL}" \
    -H "Content-Type: application/json" \
    -H "X-Approval-Secret: <REQUEST_SECRET>" \
    -d '{"command": "aws sts get-caller-identity", "reason": "test"}'
  
  # 測試人工審批
  curl -X POST "${FUNCTION_URL}" \
    -H "Content-Type: application/json" \
    -H "X-Approval-Secret: <REQUEST_SECRET>" \
    -d '{"command": "aws ec2 start-instances --instance-ids i-xxx", "reason": "test approval"}'
  ```

### 整合 Clawdbot

- [ ] **Step 6:** 更新 TOOLS.md
  ```markdown
  ## 🔐 AWS Bouncer
  
  **Endpoint:** <FUNCTION_URL>
  **Secret:** 存在 1Password
  
  使用方式見 TOOLS.md
  ```

---

## 💰 成本

| 組件 | 預估 |
|------|------|
| Lambda | $0 (Free Tier) |
| DynamoDB | $0 (Free Tier) |
| Function URL | $0 |
| CloudWatch | $0 (Free Tier) |
| **總計** | **$0/月** |

---

## 📈 未來擴展（Phase 2）

| 項目 | 優先級 | 說明 |
|------|--------|------|
| HMAC 簽章 | P2 | 已實現，設 `ENABLE_HMAC=true` 啟用 |
| Rate Limiting | P2 | 防審批疲勞攻擊 |
| 批量審批 | P3 | 同類命令合併 |
| SNS 告警 | P2 | CloudWatch Alarm 觸發通知 |
| 審計 Dashboard | P3 | QuickSight / Grafana |

---

*Plan created: 2026-01-31*
