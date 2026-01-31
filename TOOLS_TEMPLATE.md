# Clawdbot TOOLS.md 整合範本

> **最後更新:** 2026-01-31 11:49 UTC

將以下內容加到 `~/clawd/TOOLS.md`，部署後填入實際 URL：

---

## 🔐 AWS Bouncer (命令審批系統)

**用途：** 安全執行 AWS CLI 命令，透過 Telegram 人工審批

**Endpoint:** `<FUNCTION_URL>` _(部署後填入)_
**Secret:** 存在 1Password `API Credentials` vault

### 使用方式

```bash
# 1. 自動批准（read-only 命令）
curl -X POST "$BOUNCER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $BOUNCER_SECRET" \
  -d '{"command": "aws ec2 describe-instances", "reason": "檢查 EC2"}'

# 回應：
# {"status": "auto_approved", "result": "..."}

# 2. 需要審批（write 命令）
curl -X POST "$BOUNCER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $BOUNCER_SECRET" \
  -d '{"command": "aws ec2 start-instances --instance-ids i-xxx", "reason": "啟動 EC2"}'

# 回應：
# {"status": "pending_approval", "request_id": "abc123def456"}

# 3. 查詢結果
curl "$BOUNCER_URL/status/abc123def456" \
  -H "X-Approval-Secret: $BOUNCER_SECRET"

# 4. 長輪詢（等待審批，最多 50 秒）
curl -X POST "$BOUNCER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $BOUNCER_SECRET" \
  -d '{"command": "aws ec2 start-instances --instance-ids i-xxx", "wait": true}'
```

### 命令分類

| 類型 | 行為 | 範例 |
|------|------|------|
| **BLOCKED** | 403 拒絕 | `iam create-*`, `sts assume-role`, Shell 注入 |
| **SAFELIST** | 自動執行 | `ec2 describe-*`, `s3 ls`, `sts get-caller-identity` |
| **APPROVAL** | Telegram 審批 | `ec2 start/stop-*`, `s3 cp`, `lambda update-*` |

### 回應狀態

| status | 說明 |
|--------|------|
| `auto_approved` | 自動批准並已執行 |
| `pending_approval` | 等待 Telegram 確認 |
| `blocked` | 命令被拒絕（安全原因） |
| `approved` | 已批准並執行完成 |
| `denied` | 已被拒絕 |

### 環境變數

```bash
export BOUNCER_URL="https://xxx.lambda-url.us-east-1.on.aws/"
export BOUNCER_SECRET="your_secret_here"
```

---

_Bouncer v1.1.0 | 部署日期: ____-__-___
