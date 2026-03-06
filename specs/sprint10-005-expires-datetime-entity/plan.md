# Sprint 10-005: Plan — expires_at date_time entity

> Generated: 2026-03-03

---

## Technical Context

### 現狀分析

1. **`send_telegram_message()`**（`telegram.py:146`）：固定使用 `parse_mode: 'Markdown'`。不接受 `entities` 參數。

2. **通知函數**（`notifications.py`）：所有通知最終呼叫 `_send_message()` 或 `_send_message_silent()`，傳入 Markdown 格式文字和可選的 `reply_markup`。

3. **過期時間來源**：
   - 一般命令/Upload：`expires_at` 由 `create_approval_request()` 計算（`started_at + TTL`）→ 但通知函數只收到 `timeout_str`（相對時間字串，如「5 分鐘」）
   - Presigned：`expires_at` 已是 ISO 字串
   - Grant：`GRANT_APPROVAL_TIMEOUT` 常數

4. **Bot API date_time entity**：需確認是否有 Markdown/HTML 語法。如果只能透過 `entities` 參數傳遞，則需修改 `send_telegram_message()` 支援 entities。

### 關鍵技術決策

**若 parse_mode 和 entities 互斥（很可能）**，有兩個可行方案：

**方案 1（Incremental）**：只修改 `send_telegram_message()` 和 `_send_message()` 支援可選的 `entities` list。當 entities 存在時，不傳 `parse_mode`，改用 entities 處理**所有**格式。需要把現有 Markdown 轉成 entities（bold、code 等）。

**方案 2（Pragmatic）**：維持 Markdown，但將過期時間改為**顯示 UTC 和本地時間的文字**（如 `⏰ 過期：06:30 UTC / 14:30 UTC+8`），不使用 date_time entity。功能上達到目的，不需要重構。

**方案 3（Hybrid，如果 API 支援）**：在 HTML parse_mode 下使用 `<tg-date-time>` tag（待確認是否存在）。

**建議**：先做 R&D spike 確認 Bot API 9.5 date_time 的確切用法，再決定方案。

### 影響範圍

- `src/telegram.py` — `send_telegram_message()` 可能需要改 signature
- `src/notifications.py` — 6-8 處通知函數需改呼叫方式 + 傳入 expires_at timestamp
- `tests/` — 通知相關測試更新

## Implementation Phases

### Phase 0: R&D Spike（必須先做）

1. 查 Bot API 9.5 文件，確認 `date_time` entity 的：
   - 用法（entities 參數 vs HTML/Markdown tag）
   - `parse_mode` 是否與 `entities` 互斥
   - entity format（需要哪些額外欄位，如 timestamp）
2. 做一個 minimal test：發一條 Telegram 消息測試 date_time entity
3. 根據結果決定 Implementation 方案

### Phase 1: Telegram 層修改（telegram.py）

- 若需要 entities：修改 `send_telegram_message()` 接受 optional `entities` list
- 若 parse_mode 互斥：當 entities 存在時不傳 parse_mode

### Phase 2: 通知函數修改（notifications.py）

1. 通知函數 signature 新增 `expires_at: int`（Unix timestamp）
2. 計算 date_time entity 的 offset/length
3. 將 `⏰ *{timeout_str}後過期*` 改為 `⏰ Expires: <date_time_placeholder>`
4. 傳入 entities list

### Phase 3: 呼叫端更新

確保所有呼叫通知函數的地方傳入 `expires_at` timestamp（部分地方目前只有 timeout_str）

### Phase 4: 測試

1. Unit test: 確認 entities 正確計算 offset/length
2. Integration test: 發實際 Telegram 消息確認時間顯示
