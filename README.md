# Bouncer

> 🔐 Clawdbot AWS 命令審批執行系統 v1.2.0

讓 AI Agent 安全執行 AWS 命令，透過 Telegram 人工審批機制防止 Prompt Injection 攻擊。

## 快速開始

```bash
# 1. 運行測試
source .venv/bin/activate
pytest tests/ -v

# 2. 建置
sam build

# 3. 部署（需要 Telegram Bot Token 和 Secrets）
sam deploy --guided

# 4. 測試
curl -X POST "$FUNCTION_URL" \
  -H "X-Approval-Secret: $SECRET" \
  -d '{"command": "aws sts get-caller-identity"}'
```

## 專案狀態

| 項目 | 狀態 |
|------|------|
| 程式碼 | ✅ v1.2.0 (shell=False 安全改進) |
| 測試 | ✅ 62 tests, 89% coverage |
| 文件 | ✅ 完整 |
| 部署 | ⏳ 等待 Telegram Bot Token |

## 文件

| 檔案 | 說明 |
|------|------|
| [PLAN.md](PLAN.md) | 執行計畫 - 部署步驟、架構說明 |
| [HANDOFF.md](HANDOFF.md) | 交接文件 - 當前狀態、待完成項目 |
| [QA_REPORT.md](QA_REPORT.md) | QA 報告 - 測試覆蓋、安全掃描 |
| [TOOLS_TEMPLATE.md](TOOLS_TEMPLATE.md) | Clawdbot 整合模板 |
| [INTEGRATED_PLAN.md](INTEGRATED_PLAN.md) | 設計分析 - 三份報告整合 |

## 核心功能

- **四層命令分類:** BLOCKED → SAFELIST → APPROVAL → DEFAULT DENY
- **安全執行:** shlex.split() + shell=False（無 shell injection）
- **Telegram 審批:** Inline buttons 一鍵批准/拒絕
- **自動過期:** 5 分鐘未審批自動失效
- **結果查詢:** `/status/{id}` endpoint 或長輪詢

## 架構

```
Clawdbot ──► Lambda (Function URL) ──► Telegram 審批
                │                           │
                └── DynamoDB ◄──────────────┘
```

## 測試

```bash
# 啟用虛擬環境
source .venv/bin/activate

# 運行所有測試
pytest tests/ -v

# 帶覆蓋率
pytest tests/ --cov=src --cov-report=term-missing

# 簡易本地測試（無依賴）
python3 test_local.py
```

## 成本

$0/月（AWS Free Tier 覆蓋）

---

*Bouncer v1.2.0 | 最後更新: 2026-01-31 11:49 UTC | 62 tests | 89% coverage*
