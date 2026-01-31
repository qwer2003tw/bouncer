# Bouncer

> 🔐 Clawdbot AWS 命令審批執行系統

讓 AI Agent 安全執行 AWS 命令，透過 Telegram 人工審批機制防止 Prompt Injection 攻擊。

## 快速開始

```bash
# 1. 建置
sam build

# 2. 部署（需要 Telegram Bot Token 和 Secrets）
sam deploy --guided

# 3. 測試
curl -X POST "$FUNCTION_URL" \
  -H "X-Approval-Secret: $SECRET" \
  -d '{"command": "aws sts get-caller-identity"}'
```

## 文件

| 檔案 | 說明 |
|------|------|
| [PLAN.md](PLAN.md) | 執行計畫 - 部署步驟、架構說明 |
| [HANDOFF.md](HANDOFF.md) | 交接文件 - 當前狀態、待完成項目 |
| [INTEGRATED_PLAN.md](INTEGRATED_PLAN.md) | 設計分析 - 三份報告整合 |

## 核心功能

- **四層命令分類:** BLOCKED → SAFELIST → APPROVAL → DEFAULT DENY
- **Telegram 審批:** Inline buttons 一鍵批准/拒絕
- **自動過期:** 5 分鐘未審批自動失效
- **結果查詢:** `/status/{id}` endpoint 或長輪詢

## 架構

```
Clawdbot ──► Lambda (Function URL) ──► Telegram 審批
                │                           │
                └── DynamoDB ◄──────────────┘
```

## 成本

$0/月（AWS Free Tier 覆蓋）

---

*Version: 1.1.0 | Created: 2026-01-31*
