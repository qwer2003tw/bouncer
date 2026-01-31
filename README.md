# Bouncer

> 🔐 AWS 命令審批執行系統 v1.3.0
> 
> **最後更新:** 2026-01-31 13:22 UTC

讓 AI Agent 安全執行 AWS 命令。Clawdbot 主機零權限，所有命令由 Bouncer Lambda 審批後執行。

## 角色

| 角色 | 說明 |
|------|------|
| **使用者 (Steven)** | 真人，Telegram 對話 + 審批 |
| **Clawdbot/Moltbot** | AI 助手，EC2 上，零 AWS 權限 |
| **Bouncer** | Lambda，審批 + 執行 AWS 命令 |

## 安全架構

```
使用者 ──Telegram──► Clawdbot (EC2, 零權限)
                         │
                         │ POST /submit
                         ▼
                    Bouncer (Lambda)
                         │
                         │ 審批請求
                         ▼
                    使用者 審批
                         │
                         │ 執行 + 回調
                         ▼
                    Clawdbot 繼續任務
                         │
                         ▼
                    使用者 收到結果
```

**防 Prompt Injection：** 即使攻擊成功，Clawdbot 也無法直接執行 AWS 命令。

## 快速開始

```bash
# 運行測試
source .venv/bin/activate
pytest tests/ -v

# 部署（需要 Telegram Bot Token + VPC 設定）
sam build
sam deploy --guided
```

## 專案狀態

| 項目 | 狀態 |
|------|------|
| Lambda 程式碼 | ✅ v1.2.0 (shell=False) |
| 測試 | ✅ 62 tests, 89% coverage |
| VPC 整合 | ⏳ 待設定 |
| Webhook Server | ⏳ 待實作 |
| 異步回調 | ⏳ 待實作 |

## 文件

| 檔案 | 說明 |
|------|------|
| [PLAN.md](PLAN.md) | 完整計畫、角色定義、流程圖 |
| [HANDOFF.md](HANDOFF.md) | 交接文件 |
| [QA_REPORT.md](QA_REPORT.md) | 測試報告 |
| [TOOLS_TEMPLATE.md](TOOLS_TEMPLATE.md) | Clawdbot 整合模板 |

## 命令分類

| 層級 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 403 拒絕 | `iam create-*`, shell 注入 |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*` |

---

*Bouncer v1.3.0 | 62 tests | 89% coverage*
