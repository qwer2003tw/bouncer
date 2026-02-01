# Bouncer MCP Server

AWS 命令審批執行系統 - stdio MCP Server 版本

## 概述

Bouncer MCP Server 是一個運行在 EC2 上的 MCP (Model Context Protocol) Server，用於：
- 攔截 AI Agent 的 AWS CLI 命令
- 自動分類命令（BLOCKED / SAFELIST / APPROVAL）
- 對危險命令發送 Telegram 審批請求
- 等待人工審批後執行

## 架構

```
┌─────────────────────────────────────────────────────────────────┐
│                        EC2 主機                                  │
│                                                                  │
│  ┌─────────────────┐    stdio    ┌─────────────────────────┐    │
│  │   Clawdbot      │◄───────────►│   Bouncer MCP Server    │    │
│  │  (無 AWS 權限)  │             │   (有 AWS 權限)         │    │
│  └─────────────────┘             └───────────┬─────────────┘    │
│                                              │                   │
└──────────────────────────────────────────────┼───────────────────┘
                                               │ HTTPS
                                               ▼
                                    ┌─────────────────────┐
                                    │   Telegram API      │
                                    │   (Long Polling)    │
                                    └─────────────────────┘
```

## 安裝

```bash
cd ~/projects/bouncer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-mcp.txt  # 待建立
```

## 配置

環境變數：

| 變數 | 說明 | 必要 |
|------|------|------|
| `BOUNCER_TELEGRAM_TOKEN` | Telegram Bot Token | 審批功能必要 |
| `BOUNCER_CHAT_ID` | 審批者的 Telegram Chat ID | 審批功能必要 |
| `BOUNCER_CREDENTIALS_FILE` | AWS credentials 檔案路徑 | 可選 |
| `BOUNCER_DB_PATH` | SQLite 資料庫路徑 | 可選，預設 `mcp_server/bouncer.db` |

## 使用方式

### 直接執行

```bash
BOUNCER_TELEGRAM_TOKEN=xxx \
BOUNCER_CHAT_ID=123456 \
python -m mcp_server.server
```

### MCP 配置（Clawdbot）

在 MCP 設定中加入：

```json
{
  "mcpServers": {
    "bouncer": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/home/ec2-user/projects/bouncer",
      "env": {
        "BOUNCER_TELEGRAM_TOKEN": "${BOUNCER_TELEGRAM_TOKEN}",
        "BOUNCER_CHAT_ID": "${BOUNCER_CHAT_ID}",
        "BOUNCER_CREDENTIALS_FILE": "/etc/bouncer/credentials"
      }
    }
  }
}
```

## MCP Tools

### bouncer_execute

執行 AWS CLI 命令。

**Input:**
```json
{
  "command": "aws ec2 describe-instances",
  "reason": "檢查目前的 EC2 實例",
  "timeout": 300
}
```

**Output:**
```json
{
  "status": "auto_approved",
  "command": "aws ec2 describe-instances",
  "classification": "SAFELIST",
  "output": "{\"Reservations\": [...]}",
  "exit_code": 0,
  "request_id": "abc123def456"
}
```

### bouncer_status

查詢審批請求狀態。

**Input:**
```json
{
  "request_id": "abc123def456"
}
```

### bouncer_list_rules

列出命令分類規則。

### bouncer_stats

取得審批統計資訊。

## 命令分類

| 分類 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 直接拒絕 | `iam create-*`, `sts assume-role`, shell 注入 |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*`, `delete-*` |

## 測試

```bash
cd ~/projects/bouncer
source .venv/bin/activate
pytest mcp_server/test_mcp_server.py -v
```

## 檔案結構

```
mcp_server/
├── __init__.py          # Package 定義
├── server.py            # MCP Server 主程式
├── db.py                # SQLite 資料庫層
├── classifier.py        # 命令分類與執行
├── telegram.py          # Telegram 整合
├── schema.sql           # 資料庫 schema
└── test_mcp_server.py   # 單元測試
```

## 待完成

- [ ] Steven 建立專用 Telegram Bot
- [ ] 設定 AWS credentials file
- [ ] 整合到 Clawdbot MCP 配置
- [ ] E2E 測試

## 版本

- v1.0.0 - 初始 MCP Server 版本
