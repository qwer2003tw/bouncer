# Sprint 10-005: feat: expires_at date_time entity

> GitHub Issue: #42
> Priority: P1
> Generated: 2026-03-03

---

## Feature Name

date_time MessageEntity for expires_at — 使用 Bot API 9.5 的 `date_time` entity 標記通知中的過期時間，讓 Telegram 客戶端根據用戶時區自動格式化。

## Background

### Bot API 9.5 (2026-03-01) 新增

`MessageEntity` type `date_time`：
- Telegram 客戶端自動根據用戶時區顯示（Steven 看到 UTC+8，其他人看到各自時區）
- 需提供 Unix timestamp，客戶端渲染為本地時間
- 不依賴 parse_mode（是 entity，不是 Markdown 語法）

### 目前行為

`notifications.py` 中的過期時間顯示方式：
- L211: `f"⏰ *{timeout_str}後過期*"` — 相對時間（如「5 分鐘後過期」）
- L230: 同上
- L264/273: `f"⏰ *5 分鐘後過期*"` — 固定文字
- L399: `f"⏰ *審批期限：{GRANT_APPROVAL_TIMEOUT // 60} 分鐘*"`
- L599: `f"⏰ *{timeout_str}後過期*"`
- L651: `f"過期：\`{safe_expires_at}\`"` — presigned notification，已有 expires_at 字串

目前都是**相對時間**或**固定 UTC 字串**，沒有用 entity 標記。

## User Stories

**US-1: 時區自動轉換**
As **Steven**,
I want the expiry time displayed in my local timezone (UTC+8),
So that I can quickly judge when the approval will expire without mental timezone conversion.

## Acceptance Scenarios

### Scenario 1: 一般命令審批 — 過期時間用 date_time entity
- **Given**: 命令審批通知，5 分鐘 TTL
- **When**: Telegram 發送通知
- **Then**: 訊息中顯示「⏰ 過期：14:30」（Steven 的台北時間）
- **And**: 使用 `entities` 參數而非 Markdown 格式化時間部分

### Scenario 2: Presigned URL 通知 — expires_at 用 date_time entity
- **Given**: Presigned URL 生成通知
- **When**: 已有 expires_at Unix timestamp
- **Then**: 過期時間用 date_time entity 顯示

### Scenario 3: Grant session 通知
- **Given**: Grant session 審批通知
- **When**: 有審批期限
- **Then**: 審批期限用 date_time entity 顯示

## Technical Challenge

### Markdown parse_mode + entities 共存

目前所有通知使用 `parse_mode: 'Markdown'`。Bot API 支援同時傳 `parse_mode` 和 `entities`，但需要注意：

1. **parse_mode 會覆蓋 entities**：如果用 `parse_mode`，Bot API 會自動解析 Markdown 生成 entities，**忽略手動傳的 entities**。
2. **解決方案**：
   - **方案 A**：移除 `parse_mode`，全部改用 `entities` 手動指定格式。工程量太大。
   - **方案 B**：只在時間部分改用 `entities`，其他保持 Markdown。但 parse_mode 和 entities 互斥。
   - **方案 C**：改用 `parse_mode: 'MarkdownV2'` 或 `HTML`，在文字中嵌入 `<tg-emoji>` 或類似 tag。
   - **方案 D（推薦）**：先發送含 Markdown 的訊息，再用 `editMessageText` 加 entities。但這會產生兩次 API call。
   - **方案 E（最實際）**：將過期時間從相對（「5 分鐘後過期」）改為顯示**絕對 Unix timestamp**，然後在 Markdown 中用特殊格式（如果 Bot API 有內建 timestamp 語法）。

**需確認**：Bot API 9.5 的 `date_time` entity 是否有對應的 Markdown/HTML 語法（如 Discord 的 `<t:timestamp:R>`）。如果有，直接在 Markdown 中使用；如果沒有，需要重構為 entities-only 方案。

## Requirements

- **R1**: 過期時間改用 `date_time` entity，顯示用戶本地時間
- **R2**: 不能 break 現有的 Markdown 格式
- **R3**: 需確認 Bot API 9.5 date_time 的使用方式（Markdown 語法 vs entities 參數）
- **R4**: 所有含「過期」的通知都要改（至少 6 處）
- **R5**: Presigned notification 的 expires_at 也要改

## Affected Code Locations

| 行號 | 函數 | 現有格式 |
|------|------|----------|
| L211 | `send_approval_request` (dangerous) | `⏰ *{timeout_str}後過期*` |
| L230 | `send_approval_request` (normal) | `⏰ *{timeout_str}後過期*` |
| L264 | `send_account_approval_request` (add) | `⏰ *5 分鐘後過期*` |
| L273 | `send_account_approval_request` (remove) | `⏰ *5 分鐘後過期*` |
| L399 | `send_grant_request_notification` | `⏰ *審批期限：N 分鐘*` |
| L599 | `send_batch_upload_notification` | `⏰ *{timeout_str}後過期*` |
| L651 | `send_presigned_notification` | `過期：\`{safe_expires_at}\`` |
| L664+ | `send_presigned_batch_notification` | 同上 |

## Open Questions

1. Bot API 9.5 `date_time` entity 是否有 Markdown/HTML 語法？需查 API 文件。
2. `parse_mode` 和手動 `entities` 能否共存？如果不能，需要更大的重構。
3. 通知函數是否需要接收 `expires_at` Unix timestamp（目前部分只有 timeout_str 相對時間）？
