# Bouncer - 交接文件

> **最後更新:** 2026-01-31 11:47 UTC
> **當前狀態:** ✅ 程式碼完成、測試完成、待部署

---

## 📍 當前進度

### ✅ 已完成

| 項目 | 說明 | 日期 |
|------|------|------|
| 需求分析 | 三份子代理報告（Security/Architect/Pragmatic） | 2026-01-31 |
| 架構設計 | 四層命令分類、Function URL、DynamoDB | 2026-01-31 |
| 程式碼 v1.2.0 | `src/app.py` - shell=False 安全改進 | 2026-01-31 |
| SAM 模板 | `template.yaml` - 含 CloudWatch Alarms | 2026-01-31 |
| 計畫文件 | `PLAN.md`, `INTEGRATED_PLAN.md` | 2026-01-31 |
| 本地測試 | `test_local.py` - 40 tests 全部通過 | 2026-01-31 |
| **pytest 單元測試** | `tests/test_bouncer.py` - 62 tests | 2026-01-31 |
| **E2E 測試** | moto mock AWS - 完整審批流程 | 2026-01-31 |
| **覆蓋率** | 89% code coverage | 2026-01-31 |
| **QA 報告** | 安全掃描、依賴檢查、品質分析 | 2026-01-31 |

### ⏳ 待完成

| 項目 | 阻塞原因 | 負責人 |
|------|----------|--------|
| 建立 Telegram Bot | 需要 Steven 操作 @BotFather | Steven |
| 產生 Secrets | 需要 Steven 決定存放位置 | Steven |
| SAM Deploy | 等待上述資訊 | Clawd |
| 設定 Webhook | Deploy 後執行 | Clawd |
| 更新 TOOLS.md | Deploy 後執行 | Clawd |

---

## 📊 測試覆蓋

```
Tests:    62 passed
Coverage: 89%
```

### 測試分類

| 類別 | 測試數 | 說明 |
|------|--------|------|
| CommandClassification | 19 | BLOCKED/SAFELIST 命令分類 |
| HMACVerification | 4 | 簽章驗證邏輯 |
| Utilities | 3 | 輔助函數 |
| APIHandlers | 7 | API endpoint 處理 |
| StatusQuery | 3 | 狀態查詢 endpoint |
| E2EFlow | 3 | 完整審批流程 |
| Security | 2 | 安全性測試 |
| EdgeCases | 3 | 邊界情況 |
| LongPolling | 2 | 長輪詢功能 |
| TTLExpiry | 2 | 過期處理 |
| DuplicateApproval | 1 | 重複審批防護 |
| ExecuteCommandErrors | 4 | 命令執行錯誤 |
| LambdaRouting | 4 | Lambda 路由 |
| HMACEnabledFlow | 2 | HMAC 完整流程 |
| TelegramAPIErrors | 2 | API 異常處理 |
| MultipleChatIDs | 1 | 多用戶白名單 |

---

## 🔐 安全改進

### v1.1.0 → v1.2.0

```python
# 舊版（有風險）
subprocess.run(command, shell=True, ...)

# 新版（安全）
args = shlex.split(command)
subprocess.run(args, shell=False, ...)
```

---

## 📋 等待 Steven 提供的資訊

```
1. TELEGRAM_BOT_TOKEN
   - 來源：@BotFather → /newbot
   - 格式：123456789:ABC-DEF...

2. REQUEST_SECRET
   - 用途：Clawdbot 呼叫 API 時驗證
   - 產生：openssl rand -hex 16

3. TELEGRAM_WEBHOOK_SECRET
   - 用途：防止 Telegram webhook 被偽造
   - 產生：openssl rand -hex 16
```

---

## 🔧 接手後的下一步

1. **運行測試確認環境：**
   ```bash
   cd ~/projects/bouncer
   source .venv/bin/activate
   pytest tests/ -v
   ```

2. **部署（需要 Token + Secrets）：**
   ```bash
   sam build
   sam deploy --guided \
     --stack-name clawdbot-aws-approval \
     --parameter-overrides \
       TelegramBotToken=<TOKEN> \
       RequestSecret=<SECRET> \
       TelegramWebhookSecret=<WEBHOOK_SECRET>
   ```

3. **部署完成後：**
   - 設定 Telegram Webhook（見 PLAN.md Step 4）
   - 端到端測試（見 PLAN.md Step 5）
   - 更新 `~/clawd/TOOLS.md`（用 TOOLS_TEMPLATE.md）

---

## 📁 專案結構

```
~/projects/bouncer/
├── README.md              # 專案簡介
├── PLAN.md                # 執行計畫
├── HANDOFF.md             # 交接文件（本檔案）
├── QA_REPORT.md           # QA 報告
├── TOOLS_TEMPLATE.md      # Clawdbot 整合模板
├── INTEGRATED_PLAN.md     # 設計分析
├── template.yaml          # SAM 部署模板
├── pytest.ini             # pytest 配置
├── .gitignore
├── .venv/                 # Python 虛擬環境
├── src/
│   └── app.py             # Lambda v1.2.0
├── tests/
│   ├── __init__.py
│   └── test_bouncer.py    # 62 個 pytest 測試
└── test_local.py          # 簡易本地測試（無依賴）
```

---

## ⚠️ 注意事項

1. **不要把 Secrets 寫入 git**
2. **部署前運行 `pytest tests/ -v` 確認測試通過**
3. **部署後更新 TOOLS.md 填入實際 URL**

---

*Handoff document v2 | 2026-01-31 | 62 tests | 89% coverage*
