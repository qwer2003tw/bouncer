# Bouncer - 執行計畫

> **最後更新:** 2026-01-31 13:22 UTC
> **版本:** v1.3.0
> **狀態:** 程式碼完成，待部署 + 異步回調待實作

---

## 👥 角色定義

| 角色 | 說明 | 位置 |
|------|------|------|
| **使用者 (Steven)** | 真人，透過 Telegram 與 Clawdbot 對話，也負責審批 AWS 命令 | Telegram |
| **Clawdbot/Moltbot** | AI 助手，執行使用者交辦的任務，需要 AWS 權限時呼叫 Bouncer | EC2 主機 |
| **Bouncer** | 審批閘道 + AWS 命令執行器，收到命令後發審批請求，通過後執行 | AWS Lambda |

---

## 🎯 核心設計

**Clawdbot 主機零 AWS 權限，所有 AWS 命令由 Bouncer Lambda 執行**

防止 Prompt Injection 繞過審批機制。

---

## 🔄 完整流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     使用者 (Steven)                              │
│                                                                  │
│  「幫我部署一個新服務」                                          │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Telegram
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Clawdbot/Moltbot (EC2)                         │
│                      （零 AWS 權限）                             │
│                                                                  │
│  1. 理解任務：需要執行 aws ec2 run-instances                     │
│  2. 儲存任務狀態到 memory/tasks/{task_id}.json                   │
│  3. POST /submit 到 Bouncer                                      │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP (VPC 內網)
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Bouncer Lambda                              │
│                     （有 AWS 權限）                              │
│                                                                  │
│  1. 驗證 X-Approval-Secret                                       │
│  2. 命令分類：                                                   │
│     ├─ BLOCKED → 403 拒絕                                        │
│     ├─ SAFELIST → 直接執行，返回結果                             │
│     └─ 其他 → 發 Telegram 審批                                   │
│  3. 返回 pending_approval + request_id                           │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Telegram API
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     使用者 (Steven)                              │
│                                                                  │
│  🔐 AWS 命令審批請求                                             │
│  📋 命令: aws ec2 run-instances ...                              │
│                                                                  │
│  [✅ 批准]  [❌ 拒絕]     ← Steven 點擊審批                      │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Telegram Webhook
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Bouncer Lambda                              │
│                                                                  │
│  1. 收到審批結果                                                 │
│  2. 執行 AWS 命令                                                │
│  3. HTTP 回調到 EC2 Webhook Server                               │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP (VPC 內網)
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                  EC2 Webhook Server (:18800)                     │
│                     （確定性代碼）                               │
│                                                                  │
│  1. 收到 Bouncer 回調                                            │
│  2. 讀取 memory/tasks/{task_id}.json                             │
│  3. 執行 clawdbot agent --text "{resume_prompt}"                 │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Clawdbot/Moltbot (EC2)                         │
│                                                                  │
│  1. 收到明確指令，恢復任務上下文                                 │
│  2. 繼續執行任務                                                 │
│  3. 如需更多 AWS 權限，重複上述流程                              │
│  4. 任務完成，通知使用者                                         │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Telegram
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     使用者 (Steven)                              │
│                                                                  │
│  「服務部署完成！Instance ID: i-xxx」                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔐 安全機制

### 四層命令分類

| 層級 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 直接拒絕 403 | `iam create-*`, `sts assume-role`, shell 注入 |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*`, `delete-*`, `put-*` |
| **DEFAULT** | 視同 APPROVAL | 未分類的命令 |

### 防 Prompt Injection

| 攻擊 | 結果 |
|------|------|
| 直接執行 `aws xxx` | ❌ 主機無權限，失敗 |
| 繞過審批 | ❌ 必須經過 Bouncer |
| 重複執行已審批命令 | ❌ status 檢查阻擋 |

---

## 📦 組件清單

### 1. Bouncer Lambda（已完成）
- 位置：`~/projects/bouncer/src/app.py`
- 狀態：62 tests, 89% coverage
- 待改：加入 VPC 設定 + 回調機制

### 2. EC2 Webhook Server（待實作）
- 位置：`~/projects/bouncer/webhook_server/`
- 功能：接收 Bouncer 回調，觸發 Clawdbot 繼續任務

### 3. 任務狀態管理（待實作）
- 位置：`~/clawd/memory/tasks/`
- 格式：JSON 檔案，包含 task_id, resume_prompt, context

---

## 📋 部署步驟

### Phase 1: Lambda 部署（現有）

1. 建立 Telegram Bot
2. 產生 Secrets
3. `sam deploy`
4. 設定 Telegram Webhook
5. 移除 EC2 AWS 權限

### Phase 2: VPC 整合（新增）

1. 將 Lambda 加入 VPC
2. 設定 NAT Gateway（Lambda 訪問外網）
3. 設定 Security Group（Lambda → EC2:18800）

### Phase 3: Webhook Server（新增）

1. 實作 `webhook_server.py`
2. 設定 systemd service
3. 測試端到端流程

---

## ✅ 待提供

| 項目 | 來源 | 狀態 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | @BotFather | ⏳ 待建立 |
| `REQUEST_SECRET` | `openssl rand -hex 16` | ⏳ 待產生 |
| VPC ID | AWS Console | ⏳ 待提供 |
| 私有子網 ID | AWS Console | ⏳ 待提供 |
| EC2 私有 IP | `ip addr` | 可自動取得 |

---

*Bouncer v1.3.0 | 最後更新: 2026-01-31 13:22 UTC*
