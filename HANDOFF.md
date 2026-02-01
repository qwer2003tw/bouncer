# Bouncer - 交接文件

> **最後更新:** 2026-02-01 06:45 UTC
> **當前狀態:** ✅ MCP Server 完成、測試通過、待部署

---

## 🎯 專案目的

**防止 Prompt Injection 繞過 AWS 命令執行**

設計：Clawdbot 主機零 AWS 權限，所有命令由 Bouncer 審批後執行。

---

## 📍 當前進度

### ✅ 已完成

| 項目 | 說明 |
|------|------|
| 需求分析 | 三份子代理報告整合 |
| 架構設計 | 四層命令分類、MCP stdio Server |
| **MCP Server v1.0.0** | `mcp_server/` - 完整實作 |
| pytest 測試 | 40 tests, 100% pass |
| 文件 | PLAN.md, README.md, mcp_server/README.md |

### ⏳ 待完成

| 項目 | 阻塞原因 | 負責人 |
|------|----------|--------|
| Telegram Bot | 需 Steven 操作 @BotFather | Steven |
| AWS Credentials | 需 Steven 建立專用 credentials file | Steven |
| MCP 整合 | 等待上述資訊 | Agent |
| 移除主機 AWS 權限 | 整合後執行 | Agent |

---

## 🔐 安全架構

```
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  Clawdbot        │      │  Bouncer MCP     │      │    Telegram      │
│  (零 AWS 權限)   │─────►│  (有 AWS 權限)   │─────►│   (Steven 審批)  │
└──────────────────┘      └──────────────────┘      └──────────────────┘
        │                         │                         │
        │ stdio (MCP)             │ 命令分類                │ 批准/拒絕
        │ bouncer_execute()       │ BLOCKED/SAFELIST/APPROVAL│
        │                         │                         │
        ▼                         ▼                         ▼
    同步等待結果              執行並返回結果           一鍵審批
```

**與 Lambda 版本的差異：**
- ❌ Lambda + Function URL + DynamoDB + Webhook
- ✅ EC2 MCP Server + SQLite + Long Polling
- **優點：** 同步流程、100% 確定性、無冷啟動延遲

---

## 📋 接手指南

### 1. 運行測試

```bash
cd ~/projects/bouncer
source .venv/bin/activate
pytest mcp_server/test_mcp_server.py -v
```

### 2. 本地測試（無 Telegram）

```bash
# 會警告 Telegram 未配置，但 SAFELIST 命令可執行
python -m mcp_server.server
```

輸入 JSON-RPC：
```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"bouncer_execute","arguments":{"command":"aws sts get-caller-identity"}}}
```

### 3. 部署（需要 Secrets）

1. Steven 建立 Telegram Bot，取得 token
2. Steven 建立 `/etc/bouncer/credentials` 檔案
3. 配置 Clawdbot MCP（見 mcp_server/README.md）
4. 測試端到端流程
5. 移除 Clawdbot 的 AWS credentials

---

## 📊 測試覆蓋

| 指標 | 數值 |
|------|------|
| 測試數量 | 40 |
| 通過率 | 100% |
| 測試類別 | 8 |

主要測試類別：
- Database（7）
- Classifier（10）
- Validation（4）
- Execution（3）
- ApprovalWaiter（3）
- MCPTools（2）
- MCPServer（10）
- Integration（1）

---

## 📁 檔案清單

| 檔案 | 說明 |
|------|------|
| `PLAN.md` | 完整部署計畫 |
| `README.md` | 專案簡介 |
| `mcp_server/README.md` | MCP Server 文件 |
| `mcp_server/server.py` | MCP Server 主程式 |
| `mcp_server/db.py` | SQLite 資料庫層 |
| `mcp_server/classifier.py` | 命令分類邏輯 |
| `mcp_server/telegram.py` | Telegram 整合 |
| `mcp_server/test_mcp_server.py` | 單元測試 |

### 已棄用（Lambda 版本）

| 檔案 | 說明 |
|------|------|
| `src/app.py` | Lambda 版本（保留參考） |
| `template.yaml` | SAM 部署模板（不再使用） |
| `tests/test_bouncer.py` | Lambda 版本測試 |

---

## ⚠️ 重要提醒

1. **部署後必須移除主機 AWS 權限** - 這是安全架構的關鍵
2. **Telegram Bot 必須是 Bouncer 專用** - 避免 long polling 衝突
3. **Credentials file 權限要限制** - chmod 600

---

*Handoff v2.0.0 | MCP Server 版本 | 最後更新: 2026-02-01 06:45 UTC*
