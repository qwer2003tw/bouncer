# Clawdbot TOOLS.md 整合範本

> **最後更新:** 2026-01-31 12:21 UTC

部署後將以下內容加到 `~/clawd/TOOLS.md`：

---

## 🔐 Bouncer - AWS 命令執行

**⚠️ 本主機無 AWS 權限，所有 AWS 命令必須透過 Bouncer**

### 設定

| 項目 | 值 |
|------|-----|
| **URL** | `https://xxxxxxxxxx.lambda-url.us-east-1.on.aws/` |
| **Secret** | 存於 1Password |

### 使用方式

```bash
curl -X POST "$BOUNCER_URL" \
  -H "X-Approval-Secret: $BOUNCER_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "aws ec2 describe-instances",
    "reason": "用戶要求查看 EC2 狀態",
    "wait": true
  }'
```

### 參數

| 參數 | 必填 | 說明 |
|------|------|------|
| `command` | ✅ | AWS CLI 命令 |
| `reason` | | 執行原因（顯示在審批訊息） |
| `wait` | | `true` = 等待審批結果（最長 50 秒） |

### 回應狀態

| status | 說明 | HTTP |
|--------|------|------|
| `auto_approved` | SAFELIST 命令，已自動執行 | 200 |
| `approved` | 審批通過，已執行 | 200 |
| `denied` | 審批拒絕 | 200 |
| `blocked` | 危險命令，直接拒絕 | 403 |
| `pending_approval` | 等待審批中 | 202 |

### 命令分類

| 類型 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 直接拒絕 | `iam create-*`, `sts assume-role`, shell 注入 |
| **SAFELIST** | 自動執行 | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | Telegram 審批 | `start-*`, `stop-*`, `delete-*` |

### ⚠️ 重要規則

1. **不要嘗試直接執行 `aws` 命令** - 會失敗，主機無權限
2. **所有 AWS 操作必須透過此 API**
3. **危險命令會被自動阻擋**，無法執行

### 查詢請求狀態

```bash
curl "$BOUNCER_URL/status/{request_id}" \
  -H "X-Approval-Secret: $BOUNCER_SECRET"
```

---

*部署後填入實際 URL 和 Secret*
