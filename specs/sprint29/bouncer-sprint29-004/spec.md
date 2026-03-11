# Deploy Pin → Notifier Progress Message

## Summary
Move deploy message pinning from approval callback to Notifier's progress message for better UX and consistency with actual deploy state.

## Background / Motivation
Currently, deploy message pinning happens at approval time (`src/callbacks.py:756`), pinning the static approval message. However:
- **Problem 1:** Static approval message doesn't show deploy progress (user can't see if deploy is in progress or stuck)
- **Problem 2:** Notifier already sends live progress messages (`deployer/notifier/app.py:61`) but they aren't pinned
- **Problem 3:** Unpinning happens at deploy completion (`src/deployer.py:813`), but it unpins the wrong message (approval message, not progress message)

**Solution:** Pin the Notifier's dynamic progress message instead of the static approval message. The Notifier already stores `telegram_message_id` in the deploy record (line 66), making this straightforward.

**UX Rationale:** Users should see the **current deploy state** pinned (e.g., "Deploying openclaw-web... [Stage 2/5]"), not the historical approval event.

## User Stories
- **US1:** As a user, when I approve a deploy, then the pinned message shows live deploy progress (not static "Approved ✅").
- **US2:** As a user, when I check the Telegram channel during a deploy, then I immediately see the pinned progress message at the top (don't need to scroll).
- **US3:** As a user, when the deploy completes or fails, then the progress message is unpinned automatically.

## Acceptance Scenarios

### Scenario 1: Successful Deploy with Pin
- **Given:** User approves frontend deploy via Telegram callback
- **When:** Notifier sends first progress message ("Deploy started...")
- **Then:** Progress message is pinned to chat
- **And:** Previous approval message is **not** pinned
- **When:** Deploy progresses through stages
- **Then:** Pinned message updates with latest progress
- **When:** Deploy completes successfully
- **Then:** Final success message is sent
- **And:** Progress message is unpinned

### Scenario 2: Failed Deploy with Pin
- **Given:** Deploy starts and progress message is pinned
- **When:** Deploy fails at stage 3
- **Then:** Notifier sends failure message
- **And:** Progress message is unpinned
- **And:** Failure message is **not** pinned (user can scroll past it)

### Scenario 3: Multiple Concurrent Deploys
- **Given:** Two deploys running for different projects
- **When:** Both Notifiers send progress messages
- **Then:** Only the **most recent** deploy's progress message is pinned
- **And:** Previous deploy's pin is replaced (Telegram allows 1 pinned message per chat)

### Scenario 4: Notifier Restart During Deploy
- **Given:** Deploy in progress with pinned progress message
- **When:** Notifier Lambda crashes/restarts
- **Then:** Next progress update re-pins the message (no permanent pin loss)
- **And:** Deploy continues normally

## Technical Design

### Files to Change
| File | Change |
|------|--------|
| `src/callbacks.py:756` | **Remove** `pin_message()` call after approval |
| `deployer/notifier/app.py:61-70` | **Add** `pin_message()` call after sending progress message |
| `deployer/notifier/app.py:152` | Verify `unpin_message()` already exists in `handle_complete()` |
| `deployer/notifier/app.py:205` | Verify `unpin_message()` already exists in `handle_fail()` |

### Key Implementation Notes

#### 1. Remove Pin from Approval Callback
**Current code in `src/callbacks.py:756`:**
```python
# After approval
try:
    telegram.pin_message(chat_id, message_id)  # ← REMOVE THIS
    logger.info(f"[callback] pinned approval message {message_id}")
except Exception as e:
    logger.warning(f"[callback] pin failed: {e}")
```

**Change:** Delete the entire `pin_message()` block (lines 756-760, approximate). Keep approval message send, just remove pinning logic.

**Rationale:** Approval message is static ("Deploy approved ✅ by @user"). Pinning it provides no value once deploy starts.

#### 2. Add Pin to Notifier Progress Message
**Current code in `deployer/notifier/app.py:61-70`:**
```python
def handle_start(deploy_record: dict):
    """Send deploy start notification."""
    msg = format_start_message(deploy_record)
    resp = send_telegram_message(CHAT_ID, msg)

    # Store message ID in deploy record
    if resp and "message_id" in resp:
        msg_id = resp["message_id"]
        update_deploy_record(deploy_id, {"telegram_message_id": msg_id})
        logger.info(f"[notifier] stored telegram_message_id={msg_id}")
```

**Add after line 68:**
```python
def handle_start(deploy_record: dict):
    """Send deploy start notification and pin it."""
    msg = format_start_message(deploy_record)
    resp = send_telegram_message(CHAT_ID, msg)

    # Store message ID in deploy record
    if resp and "message_id" in resp:
        msg_id = resp["message_id"]
        update_deploy_record(deploy_id, {"telegram_message_id": msg_id})
        logger.info(f"[notifier] stored telegram_message_id={msg_id}")

        # NEW: Pin the progress message
        try:
            pin_telegram_message(CHAT_ID, msg_id)
            logger.info(f"[notifier] pinned progress message {msg_id}")
        except Exception as e:
            logger.warning(f"[notifier] pin failed: {e}")
```

**Implementation Detail:** `pin_telegram_message()` function likely already exists in `telegram.py` module (or create if missing):
```python
def pin_telegram_message(chat_id: str, message_id: int):
    """Pin a message in a Telegram chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "disable_notification": True,  # Don't spam users
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()
```

#### 3. Verify Unpin Logic Exists
**Check `deployer/notifier/app.py:handle_complete()`:**
```python
def handle_complete(deploy_record: dict):
    """Send deploy completion notification."""
    msg = format_complete_message(deploy_record)
    send_telegram_message(CHAT_ID, msg)

    # Unpin progress message
    msg_id = deploy_record.get("telegram_message_id")
    if msg_id:
        try:
            unpin_telegram_message(CHAT_ID, msg_id)  # ← Verify this exists
            logger.info(f"[notifier] unpinned progress message {msg_id}")
        except Exception as e:
            logger.warning(f"[notifier] unpin failed: {e}")
```

**Check `deployer/notifier/app.py:handle_fail()`:** Same pattern as above.

**Action Required:**
- If unpin logic already exists (as suggested in task context line: *"unpin 在 handle_complete() / handle_fail() 已存在（line 152/205）"*), then **no changes needed** for unpin.
- If unpin logic is missing, add the same pattern as `handle_complete()` above.

#### 4. Handle Edge Cases
**Case 1: Notifier sends multiple progress updates**
- Only pin on `handle_start()` (first message)
- Subsequent progress updates (via `handle_progress()` if exists) don't re-pin (avoid spam)

**Case 2: Pin fails due to Telegram API error**
- Log warning but don't fail deploy
- Deploy continues normally (pinning is UX nicety, not critical path)

**Case 3: User manually unpins message mid-deploy**
- No action needed (user preference respected)
- Deploy completion will attempt unpin (fails silently, no issue)

### Security Considerations
- **No new permissions:** Telegram bot already has `can_pin_messages` permission (required for existing unpin logic)
- **Rate limiting:** Pinning happens once per deploy (low frequency, no abuse risk)
- **User privacy:** `disable_notification=True` prevents spamming all chat members with pin notifications

## Task Complexity Score (TCS)
| D1 Files | D2 Cross-module | D3 Testing | D4 Infra | D5 External | Total |
|----------|-----------------|------------|----------|-------------|-------|
| 3        | 1               | 2          | 0        | 1           | 7     |

**TCS = 7 (Medium)**

- **D1 Files (3):** Modify 2 files (`callbacks.py`, `notifier/app.py`), touch 4 functions
- **D2 Cross-module (1):** `callbacks.py` removal affects approval flow; `notifier/app.py` addition affects deploy lifecycle
- **D3 Testing (2):** Test pin on deploy start, verify unpin on completion/failure, handle pin errors
- **D4 Infra (0):** No template.yaml or IAM changes
- **D5 External (1):** Telegram API call (`pinChatMessage`) - external dependency, must handle failures

## Test Requirements

### Unit Tests
- **Test:** `handle_start()` calls `pin_telegram_message()` with correct `chat_id` and `message_id`
- **Test:** Pin failure (Telegram API error) logs warning but doesn't raise exception
- **Test:** `handle_complete()` calls `unpin_telegram_message()` with stored `telegram_message_id`
- **Test:** `handle_fail()` calls `unpin_telegram_message()` with stored `telegram_message_id`

### Integration Tests (Post-Deploy)
1. **Trigger deploy in dev environment**
2. **Verify pin:** Check Telegram chat, confirm progress message is pinned
3. **Wait for deploy completion**
4. **Verify unpin:** Check Telegram chat, confirm message is unpinned
5. **Check CloudWatch logs:** Verify log entries for pin/unpin actions

### Mock Paths
- `send_telegram_message()` → mock response with `{"message_id": 12345}`
- `pin_telegram_message()` → mock successful pin (or raise exception for error test)
- `unpin_telegram_message()` → mock successful unpin

### Edge Cases
1. **Telegram API timeout during pin** → Log warning, deploy proceeds
2. **`telegram_message_id` missing from deploy record** → Unpin skipped (log info, no error)
3. **User manually pins different message** → Notifier's pin overwrites it (expected Telegram behavior)
4. **Multiple deploys pin messages rapidly** → Each pin replaces previous (Telegram limit: 1 pinned message per chat)
5. **Bot lacks `can_pin_messages` permission** → Telegram API returns 403, log error but deploy succeeds

## Implementation Checklist

### Step 1: Remove Pin from Approval
- [ ] Open `src/callbacks.py`
- [ ] Locate `pin_message()` call after approval (around line 756)
- [ ] Delete pin block (3-5 lines)
- [ ] Verify approval flow still works (approval message sent, just not pinned)

### Step 2: Add Pin to Notifier Start
- [ ] Open `deployer/notifier/app.py`
- [ ] Locate `handle_start()` function (around line 61)
- [ ] After `update_deploy_record()`, add `pin_telegram_message()` call
- [ ] Wrap in try/except to handle pin failures gracefully

### Step 3: Verify Unpin Logic
- [ ] Check `handle_complete()` for `unpin_telegram_message()` call
- [ ] Check `handle_fail()` for `unpin_telegram_message()` call
- [ ] If missing, add unpin logic (same pattern as pin)

### Step 4: Verify Telegram Module
- [ ] Check if `pin_telegram_message()` exists in `telegram.py`
- [ ] If missing, implement function (see implementation notes above)
- [ ] Check if `unpin_telegram_message()` exists
- [ ] If missing, implement: `unpinChatMessage` API call

## Deployment Plan

1. **Deploy code changes** (no infra changes needed)
2. **Test in dev environment:**
   - Approve a test deploy
   - Verify approval message is **not** pinned
   - Verify progress message **is** pinned
   - Wait for deploy completion
   - Verify progress message is unpinned
3. **Deploy to production** after dev validation
4. **Monitor CloudWatch logs** for pin/unpin events

## Rollback Plan
If pinning causes issues (e.g., Telegram rate limits, permission errors):
1. **Revert notifier code:** Remove `pin_telegram_message()` call from `handle_start()`
2. **Keep approval callback unchanged:** Don't restore old pin logic (static message pinning was the original problem)
3. **Investigate Telegram API issue** (check bot permissions, rate limits)

**Risk:** Low (pin/unpin are non-critical features; deploy functionality unaffected by pin failures)

## Success Metrics
- **UX improvement:** Users report seeing live deploy status in pinned message (qualitative feedback)
- **No errors:** CloudWatch logs show successful pin/unpin for >95% of deploys
- **No regressions:** Deploy success rate unchanged (pinning doesn't interfere with deploy logic)
