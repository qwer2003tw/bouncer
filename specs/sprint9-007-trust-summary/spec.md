# Sprint 9-007: feat: 批次信任執行後 Telegram 摘要

> Priority: P2
> Generated: 2026-03-02

---

## Feature Name

Trust Session Execution Summary — 信任 session 到期或結束後，發送 Telegram 摘要，列出所有在該 session 中執行的命令和結果。

## Background

目前信任 session 中的每個命令都有獨立的靜默 Telegram 通知（`send_trust_auto_approve_notification`），但 session 結束時沒有統整的摘要。Steven 需要翻多則通知才能了解整個 session 做了什麼。

現有通知結構（`notifications.py:286`）：
- 每次 trust auto-approve 都發一則靜默通知
- 包含命令、結果預覽、剩餘次數
- 信任 revoke 時有通知（`revoke_trust` callback）

缺少的：session 結束時的統整摘要。

## User Stories

**US-1: Session 執行摘要**
As **Steven**,
I want a summary message when a trust session ends,
So that I can quickly review everything that was done during the session.

**US-2: 快速異常偵測**
As **Steven**,
I want the summary to highlight any failed commands,
So that I can quickly spot issues without reading every individual notification.

## Acceptance Scenarios

### Scenario 1: Trust session 正常到期 — 發送摘要
- **Given**: Trust session 在 10 分鐘後到期
- **And**: 期間執行了 8 個命令，7 個成功，1 個失敗
- **When**: Session 到期
- **Then**: 發送 Telegram 摘要訊息，包含：
  - Session 持續時間
  - 總命令數 + 成功/失敗數
  - 失敗命令清單（高亮）
  - 成功命令清單（簡略）

### Scenario 2: Trust session 手動 revoke — 發送摘要
- **Given**: Steven 點擊「🛑 結束信任」
- **When**: Session 被 revoke
- **Then**: 發送相同格式的摘要（標注為手動結束）

### Scenario 3: Trust session 無任何命令 — 不發送或簡短通知
- **Given**: Trust session 到期
- **And**: 期間沒有執行任何命令
- **When**: Session 到期
- **Then**: 發送簡短通知「信任 session 到期，無命令執行」或不發送

### Scenario 4: Upload trust session 摘要
- **Given**: Trust session 用於 upload
- **And**: 期間上傳了 5 個檔案
- **When**: Session 到期
- **Then**: 摘要包含上傳檔案清單和大小

## Edge Cases

1. **大量命令**：session 內 50+ 個命令 → 摘要只顯示前 20 個，其餘「... 還有 N 個」
2. **Session 已過期被 TTL 清除**：DDB TTL 清除是非同步的，可能在 summary 生成前就被清除 → 需要在 session 活躍期間就記錄命令
3. **Concurrent summary**：多個 trust session 同時到期 → 分別發送，不合併

## Requirements

- **R1**: Trust session 結束（到期或 revoke）時發送 Telegram 摘要
- **R2**: 摘要包含：session ID、持續時間、命令數、成功/失敗分類、命令清單
- **R3**: 失敗命令用醒目格式高亮
- **R4**: 命令清單超過 20 個時截斷
- **R5**: 需要一個機制追蹤 session 內的命令（目前 log_decision 記錄各自獨立）

## Interface Contract

### DDB Schema 考量

目前 `log_decision()` 記錄的 audit log 是獨立的 items，沒有 session 聚合索引。

兩個方案：
1. **Query by trust_scope + time range**：利用現有 `source` GSI 或新增 `trust_scope` GSI 查詢
2. **Session item 累積命令清單**：在 trust session DDB item 中維護 `commands_executed` list

推薦方案 2（更簡單，不需要新 GSI）。

### Telegram 摘要格式

```
📊 信任 Session 摘要

🔑 Session: abc123
⏱ 持續：10 分鐘
📋 命令：8 個（✅ 7 成功 ❌ 1 失敗）

❌ 失敗命令：
  1. aws s3 cp /bad s3://... → (exit code: 1)

✅ 成功命令：
  1. aws s3 ls
  2. aws ec2 describe-instances --region us-east-1
  3. aws cloudformation describe-stacks ...
  ... 還有 4 個
```

### Trust Session DDB Item 新增欄位

| 欄位 | 類型 | 說明 |
|------|------|------|
| `commands_executed` | List | 執行過的命令摘要 `[{cmd, status, timestamp}]` |
| `uploads_executed` | List | 上傳過的檔案摘要 `[{filename, size, timestamp}]` |
