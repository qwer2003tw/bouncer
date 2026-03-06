# Sprint 11-008: button style urlencode → JSON body

> GitHub Issue: #60
> Priority: P0
> TCS: 3
> Generated: 2026-03-04

---

## Problem Statement

Telegram API `sendMessage` with `reply_markup` containing `inline_keyboard` buttons is sent via `urlencode` (form-encoded) by default in `_telegram_request()`. When `json_body=False` (default), the `reply_markup` field is first `json.dumps()`'d, then the entire data dict is `urlencode()`'d. This causes:

1. **Double encoding**: `reply_markup` is a JSON string inside urlencode'd body → Telegram sometimes fails to parse complex button layouts.
2. **Invalid `style` field**: Button dicts include `'style': 'primary'/'success'/'danger'` which is not a valid Telegram `InlineKeyboardButton` field. Telegram silently ignores it with `json_body`, but with `urlencode` this can cause unexpected behavior.

### Current State

- `send_telegram_message()` (`telegram.py:146`): Default `json_body=False` → urlencode.
- `send_telegram_message_silent()` (`telegram.py:162`): Default `json_body=False` → urlencode.
- `send_telegram_message_to()` (`telegram.py:183`): Uses `json_body=True` ✅ (already correct).
- **All notification functions** (`notifications.py`): Build `inline_keyboard` buttons with `'style'` field (not a Telegram API field), pass to `send_telegram_message()` → urlencode'd.

### Impact

- Buttons may not render correctly in some Telegram clients.
- `style` field is silently included in API calls (no-op but noisy).
- Inconsistency: `send_telegram_message_to` uses JSON, others use urlencode.

## Root Cause

`send_telegram_message()` and `send_telegram_message_silent()` were written before `json_body` support was added. The `style` field was added for future UI styling but never stripped before sending.

## User Stories

**US-1: Reliable Button Rendering**
As a **user reviewing approval requests**,
I want inline keyboard buttons to always render correctly,
So that I can approve/reject/trust without button display issues.

## Acceptance Criteria

1. `send_telegram_message()` uses `json_body=True` when `reply_markup` is present.
2. `send_telegram_message_silent()` uses `json_body=True` when `reply_markup` is present.
3. The `style` field is stripped from all button dicts before sending to Telegram API.
4. Messages without `reply_markup` continue to use urlencode (no change).
5. All existing notification paths (approval, trust, grant, deploy_frontend) render buttons correctly.

## Out of Scope

- Changing button labels or callback_data format.
- Adding actual colored button support (Telegram doesn't support it natively).
