# Bouncer - 交接文件

> **最後更新:** 2026-01-31 10:52 UTC
> **當前狀態:** 程式碼完成，待部署

---

## 📍 當前進度

### ✅ 已完成

| 項目 | 說明 | 日期 |
|------|------|------|
| 需求分析 | 三份子代理報告（Security/Architect/Pragmatic） | 2026-01-31 |
| 架構設計 | 四層命令分類、Function URL、DynamoDB | 2026-01-31 |
| 程式碼 v1.1.0 | `src/app.py` - 整合三份報告建議 | 2026-01-31 |
| SAM 模板 | `template.yaml` - 含 CloudWatch Alarms | 2026-01-31 |
| 計畫文件 | `PLAN.md`, `INTEGRATED_PLAN.md` | 2026-01-31 |

### ⏳ 待完成

| 項目 | 阻塞原因 | 負責人 |
|------|----------|--------|
| 建立 Telegram Bot | 需要 Steven 操作 @BotFather | Steven |
| 產生 Secrets | 需要 Steven 決定存放位置 | Steven |
| SAM Deploy | 等待上述資訊 | Clawd |
| 設定 Webhook | Deploy 後執行 | Clawd |
| 更新 TOOLS.md | Deploy 後執行 | Clawd |

---

## 🗣️ 最近討論摘要

### 2026-01-31 對話重點

1. **子代理分析完成**
   - Security Analyst: STRIDE 威脅模型、HMAC 簽章建議
   - Solutions Architect: Lambda + DynamoDB + SAM 架構
   - Pragmatic Engineer: MVP 快速部署路徑

2. **整合決策**
   - 採用 Function URL（省 API Gateway）
   - 四層命令分類（BLOCKED/SAFELIST/APPROVAL/DEFAULT DENY）
   - 加入 `/status/{id}` endpoint
   - 加入長輪詢選項 `wait=true`
   - HMAC 驗證為 opt-in（Phase 2 啟用）

3. **Steven 的要求**
   - 先更新程式碼，之後再部署
   - 需要有計畫、專案、交接文件

---

## 📋 等待 Steven 提供的資訊

```
1. TELEGRAM_BOT_TOKEN
   - 來源：@BotFather → /newbot
   - 格式：123456789:ABC-DEF...

2. REQUEST_SECRET
   - 用途：Clawdbot 呼叫 API 時驗證
   - 產生：openssl rand -hex 16
   - 存放：建議放 1Password

3. TELEGRAM_WEBHOOK_SECRET
   - 用途：防止 Telegram webhook 被偽造
   - 產生：openssl rand -hex 16
   - 存放：建議放 1Password
```

---

## 🔧 接手後的下一步

1. **如果 Steven 已提供 Token + Secrets：**
   ```bash
   cd ~/projects/bouncer
   sam build
   sam deploy --guided \
     --stack-name clawdbot-aws-approval \
     --parameter-overrides \
       TelegramBotToken=<TOKEN> \
       RequestSecret=<SECRET> \
       TelegramWebhookSecret=<WEBHOOK_SECRET>
   ```

2. **如果還沒提供：**
   - 提醒 Steven 完成 Telegram Bot 建立
   - 參考 `PLAN.md` 的「前置準備」章節

3. **部署完成後：**
   - 設定 Telegram Webhook（見 PLAN.md Step 4）
   - 端到端測試（見 PLAN.md Step 5）
   - 更新 `~/clawd/TOOLS.md` 整合說明

---

## 📁 相關檔案

| 檔案 | 用途 |
|------|------|
| `PLAN.md` | 完整執行計畫 |
| `INTEGRATED_PLAN.md` | 三份報告整合分析 |
| `README.md` | 專案簡介 |
| `template.yaml` | SAM 部署模板 |
| `src/app.py` | Lambda 程式碼 |
| `~/clawd/memory/2026-01-31.md` | 今日工作記錄 |

---

## ⚠️ 注意事項

1. **不要把 Secrets 寫入 git**
   - 用 `sam deploy --parameter-overrides` 傳入
   - 或用 AWS Secrets Manager

2. **部署前確認 AWS Region**
   - 預設 us-east-1（成本最低）
   - Steven 可能偏好 ap-east-1（香港）

3. **Telegram Bot 權限**
   - 不需要 Group Privacy 設定
   - 只需要能發訊息和接收 callback

---

*Handoff document - 讓下一個接手的人能快速上手*
