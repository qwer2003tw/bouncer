# Sprint 11-010: Tasks — trust expiry + pending 應響鈴

> Generated: 2026-03-04

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | app.py (1 file) |
| D2 Cross-module | 0 | 無跨模組（send_telegram_message 已存在） |
| D3 Testing | 1 | 2 個測試案例（ring / silent） |
| D4 Infrastructure | 0 | 無 |
| D5 External | 0 | 無新 external call |
| **Total TCS** | **2** | ✅ 不需拆分 |

## Task List

```
[010-T1] [P0] [US-1] _send_trust_expiry_notification(): pending_count > 0 → send_telegram_message()（響鈴）
[010-T2] [P0] [US-1] _send_trust_expiry_notification(): pending_count == 0 → send_telegram_message_silent()（靜音）
[010-T3] [P1] 測試: pending_count=3 → 呼叫 send_telegram_message（非 silent）
[010-T4] [P1] 測試: pending_count=0 → 呼叫 send_telegram_message_silent
```
