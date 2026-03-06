# Sprint 12-003: Tasks — CLEANUP handler message_id in schedule payload

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 2 | scheduler_service.py, notifications.py, app.py |
| D2 Cross-module | 1 | scheduler ↔ notifications ↔ app（已有串接） |
| D3 Testing | 2 | scheduler 單元 + cleanup handler fallback 測試 |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | EventBridge payload 格式變更（不需新 IAM/resource） |
| **Total TCS** | **5** | ✅ 不需拆分 |

## Task List

```
[003-T1] [P0] [US-1] scheduler_service.py: create_expiry_schedule() 增加 message_id: int | None = None 參數，寫入 Target Input
[003-T2] [P0] [US-1] notifications.py: post_notification_setup() 傳 message_id 給 create_expiry_schedule()
[003-T3] [P0] [US-1] app.py: handle_cleanup_expired() 增加 fallback — event.get('message_id') 當 DDB 無值時使用
[003-T4] [P1] 測試: create_expiry_schedule() 帶 message_id → Target Input 含 message_id
[003-T5] [P1] 測試: create_expiry_schedule() 不帶 message_id → Target Input 無 message_id（向後相容）
[003-T6] [P1] 測試: handle_cleanup_expired() DDB 有 telegram_message_id → 用 DDB 值
[003-T7] [P1] 測試: handle_cleanup_expired() DDB 無 telegram_message_id、event 有 message_id → 用 event 值
[003-T8] [P2] 測試: handle_cleanup_expired() 兩者都無 → _mark_request_timeout() + skip（現有行為不變）
```
