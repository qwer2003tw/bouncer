# Immediate Feedback After Deploy Approval

## Feature
部署審批通過後，立即更新 Telegram 訊息顯示「部署啟動中」進度，而非等待 `start_deploy()` 完成後才更新。

## User Stories
- As a Bouncer user approving a deploy, I want the Telegram approval message to update immediately after I press Approve, so that I know my action was registered and don't see a stale "pending approval" message.
- As a Bouncer user, I want feedback within 1 second of pressing Approve, so that I'm not left wondering if the button click worked.

## Acceptance Scenarios

### Scenario 1: Approve button pressed — immediate feedback
Given a deploy approval request is pending in Telegram
When the user presses "✅ Approve Deploy"
Then within 1 second, the message updates to show "🚀 部署啟動中..." (or the approval message buttons are removed)
And the `answer_callback` toast appears immediately

### Scenario 2: start_deploy succeeds — full status shown
Given the user pressed Approve and received immediate feedback
When `start_deploy()` completes successfully
Then the message is updated again with the full deploy details (deploy_id, branch, commit info)

### Scenario 3: start_deploy fails — error shown
Given the user pressed Approve and received immediate feedback
When `start_deploy()` fails or returns a conflict
Then the message is updated to show the error details
And the immediate feedback message is overwritten with the error

### Scenario 4: answer_callback toast
Given the user presses Approve
Then `answer_callback` returns immediately with "🚀 啟動部署中..." toast (already implemented)
And `update_message` is also called immediately before `start_deploy` to remove buttons

## Edge Cases
- `update_message` call before `start_deploy` may fail (Telegram API timeout) → log warning, continue with `start_deploy`
- If `update_message` is called twice (before and after `start_deploy`), the second call overwrites the first — this is expected behavior
- Deploy frontend callback (`handle_deploy_frontend_callback`) has the same pattern — must fix both

## Requirements

### Functional
- In `handle_deploy_callback` (callbacks.py line ~703): add an `update_message` call immediately after `answer_callback` to show "🚀 *部署啟動中...*" with buttons removed, BEFORE calling `start_deploy`
- The immediate update should remove inline buttons (prevent double-click)
- The final update after `start_deploy` completes remains as-is
- Same fix for `handle_deploy_frontend_callback` (callbacks.py line ~1544)

### Non-functional
- The immediate `update_message` call should be best-effort (catch exceptions, log, continue)
- No additional DDB writes required
- No Lambda timeout risk (Telegram API calls are fast, <500ms)

## Interface Contract
- No DDB schema changes
- No API changes
- `handle_deploy_callback` internal flow change only
