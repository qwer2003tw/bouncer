# Bouncer - AWS 安全審批執行系統

## 📋 專案概述

**目的：** 讓 Clawdbot 能安全執行 AWS 命令，透過獨立審批機制防止 Prompt Injection 攻擊。

**核心原則：** Clawdbot 只能「申請」，不能「執行」。執行權在獨立的 Lambda，需要人工 Telegram 確認。

## 🏗️ 架構

```
Clawdbot ──POST──► Lambda (Function URL)
   │                   │
   │                   ├─► DynamoDB (存請求)
   │                   └─► Telegram (發審批)
   │                            │
   │                      Steven 點擊批准
   │                            │
   │                   Lambda 執行命令
   │                            │
   └◄─────────────── 結果發回 Telegram
```

## 🔐 安全機制

| 層級 | 保護 |
|------|------|
| 請求驗證 | X-Approval-Secret header |
| 用戶驗證 | 只有 Chat ID 999999999 能審批 |
| 命令白名單 | read-only 自動通過 |
| 命令黑名單 | IAM/危險操作永遠拒絕 |
| 超時 | 5 分鐘未審批自動過期 |
| 執行隔離 | Lambda 環境，與 Clawdbot 完全分離 |

## 📦 組件

| 組件 | 說明 |
|------|------|
| `template.yaml` | SAM 部署模板 |
| `src/app.py` | Lambda 程式碼 |
| Telegram Bot | 獨立 bot「Bouncer」 |
| DynamoDB | 存待審批請求 |

## 🚀 部署計畫

### 配置
- **區域：** us-east-1（成本最低）
- **Stack 名稱：** clawdbot-aws-approval
- **Bot 名稱：** Bouncer

### 步驟

- [ ] 1. Steven 建立 Telegram Bot (@BotFather)
- [ ] 2. Steven 提供部署用 AWS credentials
- [ ] 3. Clawd 執行 `sam build && sam deploy`
- [ ] 4. Clawd 設定 Telegram webhook
- [ ] 5. 測試整個流程
- [ ] 6. 更新 Clawdbot TOOLS.md 整合
- [ ] 7. 刪除部署用 credentials

### 所需資訊

| 項目 | 狀態 |
|------|------|
| AWS Access Key ID | ⏳ 待提供 |
| AWS Secret Access Key | ⏳ 待提供 |
| AWS Region | ✅ us-east-1 |
| Telegram Bot Token | ⏳ 待提供 |
| Approved Chat ID | ✅ 999999999 |

## 💰 成本預估

| 項目 | 費用 |
|------|------|
| Lambda | Free Tier 覆蓋 |
| DynamoDB | < $0.01/月 |
| Function URL | 免費 |
| **總計** | **≈ $0/月** |

## 📅 時間線

- **2026-01-31：** 計畫確定，程式碼完成
- **待定：** 部署執行

---

*Created: 2026-01-31*
*Status: Planning*
