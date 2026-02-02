# Bouncer MCP Server (舊版本 - 未使用)

> ⚠️ **此版本已棄用**
> 
> 實際使用的是 `bouncer_mcp.py`，透過 Lambda API 運作。
> 此目錄保留作為參考，不再維護。

---

## 原始設計

這是本地執行的 MCP Server 版本：
- 直接在 EC2 上執行 AWS 命令
- 使用 SQLite 存審批狀態
- Telegram Long Polling 等待審批

## 為何棄用

改用 Lambda 版本的原因：
1. 更好的隔離（Agent EC2 不需要 AWS 權限）
2. 更好的安全性（Lambda 有獨立 IAM Role）
3. 更好的可靠性（無狀態、自動擴展）

## 檔案

```
mcp_server/
├── server.py            # MCP Server
├── db.py                # SQLite 資料庫
├── classifier.py        # 命令分類
├── telegram.py          # Telegram 整合
├── schema.sql           # 資料庫 schema
└── test_mcp_server.py   # 測試
```

---

*如需了解目前架構，請看根目錄的 README.md*
