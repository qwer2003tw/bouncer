# Sprint 11-010: Plan — trust expiry + pending 應響鈴

> Generated: 2026-03-04

---

## Technical Context

### 現狀分析

`_send_trust_expiry_notification()` (`app.py:331-390`):
```python
def _send_trust_expiry_notification(trust_id, source, trust_scope, pending_count, pending_requests):
    from telegram import send_telegram_message_silent
    # ... build text ...
    send_telegram_message_silent(text)  # ← always silent
```

### Design

Simple conditional: import both functions, choose based on `pending_count`.

```python
from telegram import send_telegram_message, send_telegram_message_silent

if pending_count > 0:
    send_telegram_message(text)       # rings — action required
else:
    send_telegram_message_silent(text) # silent — informational only
```

### Risk

- **Minimal**: One line change. `send_telegram_message()` is well-tested, same signature as `send_telegram_message_silent()` (minus `disable_notification`).
- No data flow changes. No DDB changes.

### Files Changed

| File | Change |
|------|--------|
| `src/app.py` | `_send_trust_expiry_notification()`: conditional ring/silent based on pending_count |
| `tests/test_trust_expiry.py` (or equivalent) | Test ring when pending > 0; test silent when pending == 0 |
