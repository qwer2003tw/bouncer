# Trust Session 批次流程文件

## 概述

Trust Session 讓已審批的來源（source + account）在時段內自動執行命令，無需逐一審批。

---

## 批次流程

### 1. 觸發信任時段

信任時段透過 Telegram callback 啟動（`approve_trust` 按鈕）：

```
Steven 按下 [🔓 信任10分鐘] 按鈕
→ Trust Session 建立（trust_id 生成）
→ 同一 trust_scope + account 的 pending 請求自動執行
```

### 2. Trust Session 參數

| 欄位 | 說明 |
|------|------|
| `trust_scope` | 識別符（必須與 bouncer_execute 的 trust_scope 相同）|
| `account_id` | 目標 AWS 帳號 |
| `expires_at` | Unix timestamp（建立後 10 分鐘） |
| `max_commands` | 最大命令數（預設 50） |
| `max_uploads` | 最大上傳數（預設 20） |

### 3. Pending 請求的自動執行

信任啟動後，系統查詢 `status=pending` + 相同 `trust_scope` + `account_id` 的請求，
**依 `created_at` 排序，最多 20 個**，逐一自動執行。

#### Approach B 改進：顯示 display_summary

當 `pending_count > 0` 時，Telegram 通知現在顯示每個 pending 請求的 `display_summary`（最多 5 個），
而不只是數量。這讓 Steven 在批准信任時，能看到即將自動執行的命令清單：

```
🔓 信任時段已啟動：`trust-abc123`
📊 命令: 0/50 | 上傳: 0/20
⚡ 自動執行 3 個排隊請求：
  • aws ec2 describe-instances --region us-east-1
  • aws s3 ls s3://my-bucket
  • aws cloudformation describe-stacks --stack-name prod
```

### 4. 安全性保障

每個 pending 請求在自動執行前會重新執行 **compliance check（SEC-013）**：
- 不合規的命令不執行，狀態更新為 `compliance_rejected`
- 確保長時間在 pending queue 的請求不會繞過安全規則

---

## 使用範例

### 典型批次部署流程

```python
# Step 1: 提交多個需審批的命令
await bouncer_execute(
    command="aws cloudformation deploy --stack-name prod --template-file template.yaml",
    reason="Deploy production stack",
    source="Private Bot (deploy)",
    trust_scope="deploy-session-2026-02-26",
)

await bouncer_execute(
    command="aws s3 cp build/ s3://prod-bucket/ --recursive",
    reason="Upload build artifacts",
    source="Private Bot (deploy)",
    trust_scope="deploy-session-2026-02-26",
)

# Step 2: 第一個請求到達時，Steven 按 [🔓 信任10分鐘]
# → 第一個命令執行
# → 第二個 pending 命令自動執行（顯示 display_summary）
```

### trust_scope 命名規則

- **格式：** `{project}-{session-id}` 或 `{project}-{YYYY-MM-DD}`
- **範例：** `bouncer-deploy-2026-02-26`、`ztp-files-sprint9`
- **注意：** 同一 trust_scope 的所有請求在同一信任時段內執行

---

## 常見問題

**Q: 如果 pending 請求超過 20 個？**
A: 每次最多自動執行 20 個。超出的請求在下次 `bouncer_execute` 時，若信任仍活躍，
   由 `_check_trust_session` 即時執行。

**Q: 信任時段過期後的 pending 請求？**
A: 保持 `pending_approval` 狀態，等待下次手動審批或新信任時段。

**Q: display_summary 從哪裡來？**
A: 由 `generate_display_summary('execute', command=cmd)` 生成，
   在 `_submit_for_approval` 時寫入 DynamoDB。

---

## 相關設定

| 常數 | 預設值 | 說明 |
|------|--------|------|
| `TRUST_SESSION_MAX_COMMANDS` | 50 | 信任時段內最大命令數 |
| `TRUST_SESSION_MAX_UPLOADS` | 20 | 信任時段內最大上傳數 |
| `TRUST_SESSION_TTL_MINUTES` | 10 | 信任時段存活時間（分鐘） |
