# Sprint 12-003: CLEANUP handler — message_id 加入 schedule payload

> GitHub Issue: #70
> Priority: P0
> TCS: 5
> Generated: 2026-03-05

---

## Problem Statement

`handle_cleanup_expired()` 在 `app.py:91` 會從 DynamoDB 取 `telegram_message_id` 來更新過期訊息。但這個 `telegram_message_id` 是由 `post_notification_setup()` 在 Telegram 送出通知**之後**才寫入 DDB 的（`notifications.py:95`）。

存在一個 race condition：

1. EventBridge Scheduler payload 只含 `request_id`（`scheduler_service.py:131-135`）
2. Cleanup handler 收到 trigger → DDB `get_item(request_id)` → 取 `telegram_message_id`
3. 如果 DDB write 延遲或 `post_notification_setup()` 失敗，`telegram_message_id` 不存在 → cleanup 跳過訊息更新

**修復方向**：在 `create_expiry_schedule()` 的 Target Input 中也帶上 `message_id`，讓 cleanup handler 有 fallback 來源，不完全依賴 DDB。

### Current Flow

```
send_approval_request()
  → Telegram API → 取得 message_id
  → post_notification_setup(request_id, message_id, expires_at)
    → DDB update: SET telegram_message_id = :mid
    → scheduler.create_expiry_schedule(request_id, expires_at)
      → EventBridge Input: { source, action, request_id }  ← ❌ 沒有 message_id
```

### Desired Flow

```
send_approval_request()
  → Telegram API → 取得 message_id
  → post_notification_setup(request_id, message_id, expires_at)
    → DDB update: SET telegram_message_id = :mid
    → scheduler.create_expiry_schedule(request_id, expires_at, message_id=message_id)
      → EventBridge Input: { source, action, request_id, message_id }  ← ✅ 帶 message_id
```

## Root Cause

`SchedulerService.create_expiry_schedule()` 的簽章和 `Target.Input` 是在 sprint7-002 設計的，當時只考慮 `request_id`，沒有預想到 DDB write 可能延遲或失敗的情況。

## User Stories

**US-1: Reliable Cleanup**
As the **Bouncer system**,
I want the cleanup handler to have `message_id` in the scheduler event payload as a fallback,
So that expired request messages can always be updated/cleaned up, even if DDB `telegram_message_id` write failed.

## Scope

- `scheduler_service.py`: `create_expiry_schedule()` 增加 `message_id` 可選參數
- `notifications.py`: `post_notification_setup()` 傳 `message_id` 給 scheduler
- `app.py`: `handle_cleanup_expired()` 優先用 DDB，fallback 用 event payload
- 測試覆蓋

## Out of Scope

- 不改 `TrustExpiryNotifier` 的 schedule payload
- 不改 EventBridge Scheduler 的基礎設施（group/role 不變）
- 不改 `handle_trust_expiry()` handler
