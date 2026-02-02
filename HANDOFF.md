# Bouncer - 交接文件

> **最後更新:** 2026-02-02
> **狀態:** ✅ Production 運行中

---

## 🎯 專案目的

讓 AI Agent 安全執行 AWS 命令，防止 Prompt Injection 直接操作 AWS 資源。

---

## 📍 架構概述

```
Agent (EC2) ──mcporter──► bouncer_mcp.py ──HTTPS──► Lambda API ──► Telegram 審批
                                                        │
                                                        ▼
                                                   AWS 執行
```

**關鍵組件：**

| 組件 | 位置 | 說明 |
|------|------|------|
| `bouncer_mcp.py` | EC2 本地 | MCP Server，透過 mcporter 呼叫 |
| Lambda API | AWS | 審批 + 執行 AWS 命令 |
| DynamoDB | AWS | 存審批請求、帳號配置 |
| SAM Deployer | AWS | CodeBuild + Step Functions |

---

## 🔧 MCP Server 設定

mcporter 配置 (`~/.config/mcporter/config.json`)：

```json
{
  "servers": {
    "bouncer": {
      "type": "stdio",
      "command": "python3",
      "args": ["/home/ec2-user/projects/bouncer/bouncer_mcp.py"],
      "env": {
        "BOUNCER_API_URL": "https://YOUR_API_GATEWAY_URL",
        "BOUNCER_SECRET": "<from 1Password>"
      }
    }
  }
}
```

---

## 📋 常用操作

### 執行 AWS 命令
```bash
mcporter call bouncer.bouncer_execute \
  command="aws s3 ls" \
  reason="檢查 S3" \
  source="Steven's Private Bot"
```

### 部署 Bouncer
```bash
mcporter call bouncer.bouncer_deploy \
  project="bouncer" \
  reason="更新功能"
```

### 查詢部署狀態
```bash
mcporter call bouncer.bouncer_deploy_status deploy_id="<id>"
```

---

## 🔐 Secrets

| Secret | 位置 | 用途 |
|--------|------|------|
| `BOUNCER_SECRET` | 1Password | API 認證 |
| `TelegramBotToken` | Secrets Manager | Telegram Bot |
| `sam-deployer/github-pat` | Secrets Manager | GitHub clone |

---

## 📁 檔案結構

```
bouncer/
├── bouncer_mcp.py        # MCP Server 入口
├── src/app.py            # Lambda handler
├── template.yaml         # SAM 部署模板
├── deployer/             # SAM Deployer
│   ├── template.yaml     # Deployer stack
│   └── notifier/         # Telegram 通知 Lambda
├── tests/                # 測試
└── mcp_server/           # [舊] 本地版本，未使用
```

---

## ⚠️ 注意事項

1. **source 參數** - 所有請求都要帶，讓 Steven 知道來源
2. **Multi-account** - 用 `account` 參數指定帳號 ID
3. **審批超時** - 預設 300 秒，可用 `timeout` 調整

---

## 🔗 相關資源

- **API**: `https://YOUR_API_GATEWAY_URL/`
- **GitHub**: https://github.com/qwer2003tw/bouncer
- **CloudFormation**: `clawdbot-bouncer`, `bouncer-deployer`
