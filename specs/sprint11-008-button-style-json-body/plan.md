# Sprint 11-008: Plan — button style urlencode → JSON body

> Generated: 2026-03-04

---

## Technical Context

### 現狀分析

1. **`_telegram_request()`** (`telegram.py:60`): Has `json_body` parameter. When `True`, sends `Content-Type: application/json` with `json.dumps(data)`. When `False`, sends urlencode'd form data.

2. **`send_telegram_message()`** (`telegram.py:146`): Manually `json.dumps(reply_markup)` into `data['reply_markup']`, then calls `_telegram_request()` with default `json_body=False`. The result: `reply_markup` is a JSON string that gets urlencode'd.

3. **`send_telegram_message_silent()`** (`telegram.py:162`): Same pattern as above.

4. **Button `style` field**: All `inline_keyboard` button dicts in `notifications.py` include `'style': 'primary'/'success'/'danger'`. This is not a valid Telegram `InlineKeyboardButton` field.

### Design

#### Option A: Switch to json_body=True when reply_markup present (Recommended)

In `send_telegram_message()` and `send_telegram_message_silent()`:
- When `reply_markup` is present: pass `reply_markup` as a dict (not JSON string), set `json_body=True`.
- When no `reply_markup`: keep current behavior (`json_body=False`).

#### Option B: Strip style + keep urlencode

Strip `style` from buttons, keep `json.dumps()` + urlencode. Less clean — still double-encodes.

**Decision: Option A** — cleaner, consistent with `send_telegram_message_to()`.

#### Style Stripping

Add a helper `_strip_button_style(reply_markup)` that deep-clones `reply_markup` and removes `style` key from each button dict. Call before sending.

### Files Changed

| File | Change |
|------|--------|
| `src/telegram.py` | `send_telegram_message()` + `send_telegram_message_silent()`: use `json_body=True` + strip style |
| `tests/test_telegram.py` (or equivalent) | Test json_body used with reply_markup; test style stripped |
