# Sprint 9-007: Plan — 批次信任執行後 Telegram 摘要

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **Trust session 生命週期**：
   - 建立：callback `approve_trust` → `create_trust_session()`（`trust.py:210`）
   - 使用：`should_trust_approve()`（`trust.py:360`）→ execute → `send_trust_auto_approve_notification()`
   - 結束：到期（TTL）或手動 `revoke_trust` callback
   - TTL 在 DDB 層面（非同步清除，delay 最多 48 小時）

2. **Trust session DDB item**：存在 requests 表（與 pending 共用），有 `trust_scope`, `account_id`, `max_commands`, `command_count`, `max_uploads`, `upload_count` 等欄位。

3. **Revoke callback**（`callbacks.py`）：`revoke_trust:{request_id}` → 更新 DDB status → 回覆 Telegram。

4. **Expiry 機制**：目前靠 `scheduler_service.py` 的 EventBridge scheduled event 或 DDB TTL。沒有 session 到期的主動回調。

### 設計挑戰

**到期時機**：DDB TTL 清除是非同步的。要在到期時發送摘要，需要：
- 方案 A: EventBridge scheduled rule（session 建立時註冊一個定時觸發）— 已有 `scheduler_service.py` 基礎
- 方案 B: 在每次 trust check 時判斷是否過期 → 第一次偵測到過期時發摘要
- 方案 C: 只在 revoke 時發摘要（不處理自然到期）

**推薦方案 A + C 組合**：
- revoke 時立即發摘要（簡單）
- session 建立時註冊 EventBridge 定時觸發，到期時 Lambda 發摘要

## Implementation Phases

### Phase 1: 命令追蹤（trust.py）

1. 在 trust session DDB item 新增 `commands_executed` (List) 和 `uploads_executed` (List)
2. 每次 `should_trust_approve()` 成功 + 執行後，append 命令摘要到 list
3. 使用 DDB `update_item` + `list_append`（atomic operation）
4. 限制 list 最大 50 items（防 DDB item 超過 400KB）

### Phase 2: Revoke 摘要（callbacks.py + notifications.py）

1. 在 `revoke_trust` callback 中：
   a. 讀取 trust session item（含 commands_executed）
   b. 呼叫新函數 `send_trust_session_summary()`
   c. 更新 Telegram 訊息
2. `notifications.py` 新增 `send_trust_session_summary()` 函數

### Phase 3: 到期摘要（scheduler_service.py）

1. 在 trust session 建立時（`create_trust_session()`），註冊 EventBridge scheduled event
   - Scheduled at: session 到期時間 + 30 秒 buffer
   - Target: Lambda function
   - Input: `{"action": "trust_session_summary", "request_id": "..."}`
2. Lambda handler 接收事件 → 讀取 session → 發送摘要
3. 若 session 已被 revoke（status != active），跳過（revoke 已發過摘要）

### Phase 4: 測試

1. 單元測試：命令追蹤 list_append
2. 單元測試：send_trust_session_summary() 格式
3. Integration test：revoke 路徑發摘要
