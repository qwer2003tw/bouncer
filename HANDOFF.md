# Bouncer - 交接文件

> **最後更新:** 2026-01-31 12:21 UTC
> **當前狀態:** ✅ 程式碼完成、測試完成、待部署

---

## 🎯 專案目的

**防止 Prompt Injection 繞過 AWS 命令執行**

設計：Clawdbot 主機零 AWS 權限，所有命令由 Bouncer Lambda 審批後執行。

---

## 📍 當前進度

### ✅ 已完成

| 項目 | 說明 |
|------|------|
| 需求分析 | 三份子代理報告整合 |
| 架構設計 | 四層命令分類、Function URL、DynamoDB |
| 程式碼 v1.2.0 | `src/app.py` - shell=False 安全執行 |
| SAM 模板 | `template.yaml` - 含 CloudWatch Alarms |
| pytest 測試 | 62 tests, 89% coverage |
| 文件 | PLAN.md, README.md, QA_REPORT.md |

### ⏳ 待完成

| 項目 | 阻塞原因 | 負責人 |
|------|----------|--------|
| Telegram Bot | 需 Steven 操作 @BotFather | Steven |
| Secrets | 需 Steven 決定存放位置 | Steven |
| SAM Deploy | 等待上述資訊 | Clawd |
| 移除主機 AWS 權限 | Deploy 後執行 | Clawd |

---

## 🔐 安全架構

```
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  Clawdbot 主機   │      │  Bouncer Lambda  │      │    Telegram      │
│  (零 AWS 權限)   │─────►│  (有 AWS 權限)   │─────►│   (Steven 審批)  │
└──────────────────┘      └──────────────────┘      └──────────────────┘
        │                         │                         │
        │ POST /submit            │ 命令分類                │ 批准/拒絕
        │ {"command": "..."}      │ BLOCKED/SAFELIST/APPROVAL│
        │                         │                         │
        ▼                         ▼                         ▼
    無法直接執行              執行並返回結果           一鍵審批
```

---

## 📋 接手指南

### 1. 運行測試

```bash
cd ~/projects/bouncer
source .venv/bin/activate
pytest tests/ -v --cov=src
```

### 2. 部署（需要 Secrets）

```bash
sam build
sam deploy --guided \
  --stack-name clawdbot-bouncer \
  --parameter-overrides \
    TelegramBotToken=<TOKEN> \
    RequestSecret=<SECRET> \
    TelegramWebhookSecret=<WEBHOOK_SECRET>
```

### 3. 部署後

1. 設定 Telegram Webhook（見 PLAN.md）
2. 移除 Clawdbot 主機 AWS credentials
3. 更新 `~/clawd/TOOLS.md`（用 TOOLS_TEMPLATE.md）
4. 端到端測試

---

## 📊 測試覆蓋

| 指標 | 數值 |
|------|------|
| 測試數量 | 62 |
| 覆蓋率 | 89% |
| 測試類別 | 16 |

主要測試類別：
- CommandClassification（19）
- E2EFlow（3）
- Security（2）
- LongPolling（2）
- ExecuteCommandErrors（4）

---

## 📁 檔案清單

| 檔案 | 說明 |
|------|------|
| `PLAN.md` | 完整部署計畫 |
| `README.md` | 專案簡介 |
| `QA_REPORT.md` | QA 報告 |
| `TOOLS_TEMPLATE.md` | Clawdbot 整合模板 |
| `template.yaml` | SAM 部署模板 |
| `src/app.py` | Lambda 程式碼 v1.2.0 |
| `tests/test_bouncer.py` | pytest 測試 |

---

## ⚠️ 重要提醒

1. **部署後必須移除主機 AWS 權限** - 這是安全架構的關鍵
2. **Secrets 不要寫入 git** - 用 parameter overrides 傳入
3. **測試通過才部署** - `pytest tests/ -v`

---

*Handoff v1.2.0 | 最後更新: 2026-01-31 12:21 UTC*
