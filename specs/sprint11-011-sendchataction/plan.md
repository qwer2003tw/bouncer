# Sprint 11-011: Plan — sendChatAction typing

> Generated: 2026-03-04

---

## Technical Context

### 現狀分析

1. **`telegram.py`**: Has `_telegram_request()`, `send_telegram_message()`, `send_telegram_message_silent()`, etc. No `sendChatAction` support.

2. **`app.py:555`**: `handle_mcp_tool_call()` dispatches to tool handlers. No pre-processing hook for typing.

3. **Telegram `sendChatAction` API**: Simple POST with `chat_id` + `action` (typing/upload_photo/etc). Returns quickly. Action auto-expires after ~5 seconds.

### Design

#### New Function in `telegram.py`

```python
def send_chat_action(action: str = 'typing') -> None:
    """Send chat action (typing indicator) to Telegram. Fire-and-forget."""
    try:
        _telegram_request('sendChatAction', {
            'chat_id': APPROVED_CHAT_ID,
            'action': action,
        }, timeout=3)
    except Exception:
        pass  # Non-critical, don't break main flow
```

#### Integration Points

1. **`handle_mcp_tool_call()`** (`app.py`): Call `send_chat_action()` at the top, before dispatching to handler.
2. **Long callbacks** (optional enhancement): `handle_deploy_frontend_callback()` can call `send_chat_action()` periodically during file copy loop.

#### Fire-and-Forget

All `send_chat_action()` calls are wrapped in try/except. Failure never propagates.

### Files Changed

| File | Change |
|------|--------|
| `src/telegram.py` | Add `send_chat_action()` function |
| `src/app.py` | `handle_mcp_tool_call()`: call `send_chat_action()` before dispatch |
| `tests/test_telegram.py` | Test `send_chat_action` calls `_telegram_request` correctly |
| `tests/test_app.py` | Test typing action sent during MCP tool call processing |
