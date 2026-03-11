# Pin/Unpin Deploy Message — Sprint 25

## Overview
Pin the deploy approval notification message when a deploy is approved, and unpin it when the deploy completes (SUCCESS or FAILED). This prevents the status message from being lost in busy Telegram chats.

## User Stories
1. As a developer, I want the deploy status message pinned in Telegram so that I can easily find it in a busy chat without scrolling.
2. As a chat admin, I want the message automatically unpinned when the deploy finishes so that the pinned message doesn't become stale.
3. As a system operator, I want pin/unpin failures to be logged but not break the deploy flow so that deploy succeeds even if Telegram permissions are missing.

## Acceptance Scenarios

### Scenario 1: Pin on Approval
**Given** a deploy request is approved via Telegram
**When** the approval callback handler runs and creates the deploy execution
**Then** the Telegram approval message is pinned in the chat
**And** the deploy starts normally regardless of pin success/failure

### Scenario 2: Unpin on Success
**Given** a deploy is running and the status message is pinned
**When** the deploy completes with status `"SUCCESS"`
**Then** the Telegram message is unpinned
**And** the deploy record is updated to `status="SUCCESS"` regardless of unpin result

### Scenario 3: Unpin on Failure
**Given** a deploy is running and the status message is pinned
**When** the deploy fails with status `"FAILED"`
**Then** the Telegram message is unpinned
**And** the failure notification is sent
**And** the deploy record is updated to `status="FAILED"` regardless of unpin result

### Scenario 4: Pin Failure Doesn't Block Deploy
**Given** the Telegram bot lacks `can_pin_messages` permission
**When** a deploy is approved
**Then** the pin call fails with a Telegram API error
**And** a warning is logged: `"[deployer] Failed to pin message (ignored): <error>"`
**And** the deploy starts successfully

### Scenario 5: Unpin Failure Doesn't Block Completion
**Given** the pinned message was deleted by a user
**When** the deploy completes
**Then** the unpin call fails with "message not found"
**And** a warning is logged
**And** the deploy record is updated normally

## Interface Contract

### New Telegram Functions (telegram.py)
```python
def pin_message(message_id: int, disable_notification: bool = True) -> bool:
    """Pin a message in the approved chat.

    Args:
        message_id: Telegram message ID to pin
        disable_notification: If True, pin silently without notifying users

    Returns:
        True if successful, False otherwise (best-effort)
    """

def unpin_message(message_id: int) -> bool:
    """Unpin a specific message.

    Args:
        message_id: Telegram message ID to unpin

    Returns:
        True if successful, False otherwise (best-effort)
    """
```

### Telegram Bot API Calls
**Pin:**
```http
POST https://api.telegram.org/bot<token>/pinChatMessage
{
  "chat_id": "<APPROVED_CHAT_ID>",
  "message_id": 12345,
  "disable_notification": true
}
```

**Unpin:**
```http
POST https://api.telegram.org/bot<token>/unpinChatMessage
{
  "chat_id": "<APPROVED_CHAT_ID>",
  "message_id": 12345
}
```

## Implementation Notes

### Files to Modify
- `src/telegram.py`:
  - Add `pin_message(message_id, disable_notification=True) -> bool`
  - Add `unpin_message(message_id) -> bool`
  - Both functions use `_telegram_request()` and return `True/False` (best-effort)

- `src/deployer.py`:
  - **Line 1094-1111** (`send_deploy_approval_request`): After sending approval message, store `telegram_message_id` in DDB (already done for EventBridge expiry cleanup #75)
  - **New location** (approval callback handler in `app.py`): After deploy is approved and execution starts, call `pin_message(telegram_message_id)`
  - **Line 785-787** (deploy completion in `get_deploy_status`): When status transitions to SUCCESS/FAILED, call `unpin_message(telegram_message_id)`

- `src/app.py`:
  - In the approval callback handler (wherever `approve:{request_id}` is processed), add:
    ```python
    telegram_message_id = callback_query.get('message', {}).get('message_id')
    if telegram_message_id:
        try:
            from telegram import pin_message
            pin_message(telegram_message_id, disable_notification=True)
        except Exception as e:
            logger.warning(f"[deploy] Failed to pin message (ignored): {e}")
    ```

### New Files
- None

### DynamoDB Changes
- **No schema change required**: `telegram_message_id` is already stored in the requests table (added in Sprint 7 #75 for expiry cleanup)
- The deploy record may optionally store `pinned_message_id` if we want to track which message was pinned, but it's not strictly necessary (we can retrieve it from the approval request record)

### Telegram Bot Permissions
**Required permission:** `can_pin_messages`

**How to grant:**
1. Open Telegram group chat settings
2. Go to "Administrators"
3. Edit the Bouncer bot permissions
4. Enable "Pin messages"

**Note in spec:** The spec should document this requirement in a "Prerequisites" section so that operators know to grant the permission before deploying.

### Security Considerations
- **Best-effort only**: Pin/unpin failures do **not** block the deploy operation
- **No sensitive data**: Message IDs are not sensitive (they're public within the chat)
- **Rate limiting**: Telegram allows ~30 pin/unpin calls per minute; bouncer deploy frequency is much lower (~1-5 per hour), so no risk
- **Permission check**: The bot will fail silently if it lacks `can_pin_messages`; log a warning for debugging

### Error Handling Pattern
```python
try:
    from telegram import pin_message
    success = pin_message(message_id, disable_notification=True)
    if not success:
        logger.warning("[deploy] pin_message returned False (missing permissions?)")
except Exception as e:
    logger.warning(f"[deploy] Failed to pin message (ignored): {e}")
```

### Testing Strategy
- **Unit test (mock)**: Mock `_telegram_request` to return success/failure, verify pin/unpin called with correct args
- **Integration test (manual)**:
  1. Approve a deploy → verify message is pinned in Telegram
  2. Wait for deploy to complete → verify message is unpinned
  3. Remove bot `can_pin_messages` permission → verify deploy still succeeds with warning log
- **Edge case test**: Delete the pinned message before deploy completes → verify unpin fails gracefully

## TCS Score
| D1 Files | D2 Cross-module | D3 Testing | D4 Infrastructure | D5 External | Total |
|----------|-----------------|------------|-------------------|-------------|-------|
| 2        | 1               | 2          | 0                 | 1           | 6     |

**TCS = 6 (Simple)**

**Breakdown:**
- D1 (Files): 2 — Modify `telegram.py` (add 2 functions), `deployer.py` (add pin/unpin calls), `app.py` (add pin on approval)
- D2 (Cross-module): 1 — telegram.py → deployer.py → app.py (linear dependency chain)
- D3 (Testing): 2 — Mock tests for pin/unpin + manual integration test in Telegram
- D4 (Infrastructure): 0 — No DDB or IAM changes (message_id already stored)
- D5 (External): 1 — Depends on Telegram Bot API `pinChatMessage`/`unpinChatMessage` endpoints (new API calls)

---

## Prerequisites (Important!)
Before deploying this feature, ensure the Telegram bot has the `can_pin_messages` permission in the approval chat:
1. Open Telegram → group chat → Settings → Administrators
2. Edit Bouncer bot → Enable "Pin messages"

**If this permission is missing**, pin/unpin calls will fail silently and log warnings, but deploys will continue to work normally.
