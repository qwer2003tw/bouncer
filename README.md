# Bouncer

> 🔐 Clawdbot AWS 命令審批執行系統 v1.2.0
> 
> **最後更新:** 2026-01-31 12:21 UTC

讓 AI Agent 安全執行 AWS 命令。Clawdbot 主機零權限，所有命令由 Bouncer Lambda 審批後執行。

## 安全架構

```
Clawdbot (零 AWS 權限) ──► Bouncer Lambda ──► Telegram 審批
                              │                    │
                              └─── 執行命令 ◄──────┘
```

**防 Prompt Injection：** 即使攻擊成功，Clawdbot 也無法直接執行 AWS 命令。

## 快速開始

```bash
# 運行測試
source .venv/bin/activate
pytest tests/ -v

# 部署（需要 Telegram Bot Token）
sam build
sam deploy --guided
```

## 專案狀態

| 項目 | 狀態 |
|------|------|
| 程式碼 | ✅ v1.2.0 (shell=False) |
| 測試 | ✅ 62 tests, 89% coverage |
| 文件 | ✅ 完整 |
| 部署 | ⏳ 等待 Telegram Bot Token |

## 文件

| 檔案 | 說明 |
|------|------|
| [PLAN.md](PLAN.md) | 部署步驟、架構說明 |
| [HANDOFF.md](HANDOFF.md) | 交接文件、接手指南 |
| [QA_REPORT.md](QA_REPORT.md) | 測試報告、覆蓋率 |
| [TOOLS_TEMPLATE.md](TOOLS_TEMPLATE.md) | Clawdbot 整合模板 |

## 命令分類

| 層級 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 403 拒絕 | `iam create-*`, shell 注入 |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*` |

## 成本

$0/月（AWS Free Tier）

---

*Bouncer v1.2.0 | 62 tests | 89% coverage*
