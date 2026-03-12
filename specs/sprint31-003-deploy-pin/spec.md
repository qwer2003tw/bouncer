# Deploy Pin → Notifier Progress Msg + Auto Unpin

## Feature
部署審批通過後，將 Telegram 審批訊息 pin 起來作為進度追蹤，並在部署完成（成功或失敗）後自動 unpin；整合 Notifier Lambda 的進度訊息機制。

## User Stories
- As a Bouncer user, I want the deploy approval message to be pinned in Telegram when a deploy starts, so that I can track progress without scrolling up.
- As a Bouncer user, I want the pinned message to be automatically unpinned when the deploy completes (success or failure), so that the chat isn't cluttered with stale pinned messages.
- As a Bouncer user, I want the pinned message to be updated with progress stages (INITIALIZING → SCANNING → BUILDING → DEPLOYING → done), so that I have real-time visibility.

## Acceptance Scenarios

### Scenario 1: Deploy approved → message pinned
Given a deploy approval request is approved via Telegram
When `handle_deploy_callback` processes the approval
Then the approval message (`message_id`) is pinned in Telegram
And `telegram_message_id` is stored in the deploy record (already implemented)

### Scenario 2: Deploy completes (success) → message unpinned
Given the deploy is running and the approval message is pinned
When the Notifier Lambda receives `action=success`
Then the Telegram message is updated to show ✅ success
And the message is unpinned (already in `handle_success`)

### Scenario 3: Deploy fails → message unpinned
Given the deploy is running and the approval message is pinned
When the Notifier Lambda receives `action=failure`
Then the Telegram message is updated to show ❌ failure
And the message is unpinned (already in `handle_failure`)

### Scenario 4: Notifier progress updates use correct message_id
Given the Notifier Lambda receives `action=progress`
When updating the message
Then it fetches `telegram_message_id` from the deploy history record
And updates the existing approval message (not sends a new one)

### Scenario 5: Pin fails gracefully
Given the approval is processed
When `pin_telegram_message` fails (e.g. bot lacks admin rights)
Then a warning is logged
And the deploy proceeds normally (pin is best-effort)

## Edge Cases
- Notifier Lambda uses its own `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` env vars — must be configured
- If `telegram_message_id` is 0 or missing in history record → send new message instead of update
- Pin after `update_message("🚀 部署已啟動...")` — pin must happen AFTER the message is updated (otherwise pinned message shows stale content)
- Group chat with thread_id: the Notifier Lambda app.py uses plain `sendMessage` without `message_thread_id` — may need to add thread support

## Requirements

### Functional
- `handle_deploy_callback` in `callbacks.py`: after `update_message("🚀 部署已啟動...")`, call `pin_message(message_id)` (best-effort)
- Notifier Lambda (`deployer/notifier/app.py`): `handle_progress` must fetch `telegram_message_id` from history table and update the existing approval message (already does this for its own message_id — verify it uses the stored one)
- `handle_success` and `handle_failure` in Notifier already call `unpin_telegram_message` — verify implementation is correct
- All Notifier functions must use `TELEGRAM_CHAT_ID` and `MESSAGE_THREAD_ID` env vars (for group+topic support)

### Non-functional
- Pin/unpin are best-effort: errors must be caught and logged, not propagated
- Notifier Lambda is a separate deployed unit — changes require `deployer/notifier/` update AND `template.yaml` env var check

## Interface Contract
- DDB: `telegram_message_id` already stored in deploy history by `handle_deploy_callback` (line ~749)
- Notifier Lambda reads `telegram_message_id` from history table in `handle_progress`/`handle_success`/`handle_failure`
- New env var: `MESSAGE_THREAD_ID` in Notifier Lambda (for group topic support)
