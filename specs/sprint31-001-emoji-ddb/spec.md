# Emoji Based on Exit Code + command_status to DDB

## Feature
將命令執行後的 ❌/✅ emoji 統一改為依據 exit code 判斷，並將 `command_status` 欄位寫入 DynamoDB。

## User Stories
- As a Bouncer operator, I want the result notification emoji to reflect actual command success/failure (based on exit code), so that I can immediately tell whether a command succeeded without reading the output.
- As a Bouncer developer, I want `command_status` stored in DynamoDB, so that I can query/audit command outcomes and build metrics dashboards.

## Acceptance Scenarios

### Scenario 1: Manual approve — command succeeds (exit code 0)
Given a command is approved manually via Telegram callback
When the command exits with code 0
Then the notification title shows "✅ *已批准並執行*"
And `command_status = 'success'` is stored in DynamoDB

### Scenario 2: Manual approve — command fails (exit code non-zero)
Given a command is approved manually via Telegram callback
When the command exits with a non-zero exit code (e.g. exit code 2 for usage error)
Then the notification title shows "❌ *已批准但執行失敗*"
And `command_status = 'failed'` is stored in DynamoDB

### Scenario 3: Trust auto-approve — command fails
Given a trust session is active and a command is auto-approved
When the command exits with a non-zero exit code
Then the notification shows ❌ emoji in the result section
And `command_status = 'failed'` is stored in DynamoDB

### Scenario 4: Grant auto-execute — command fails
Given a grant session executes a command
When the command output starts with "usage:" (exit code 2)
Then `extract_exit_code` detects code 2
And `command_status = 'failed'` is stored in DynamoDB

### Scenario 5: Auto-approve path — command fails
Given a command is auto-approved (safelist)
When the command fails (exit code != 0)
Then `command_status = 'failed'` is stored in DynamoDB

## Edge Cases
- `extract_exit_code` returns `None` for non-AWS output → treat as success (no exit code info)
- Output starts with `❌` (Bouncer-formatted error) → exit code = -1, treat as failed
- Output starts with `usage:` / `Usage:` → exit code = 2, treat as failed
- Trust callback: DDB update_item already has update expression — must ADD `command_status` without breaking existing fields

## Requirements

### Functional
- `_format_approval_response` in `callbacks.py`: title must be `❌ *已批准但執行失敗*` when `_is_execute_failed(result)` is True
- `update_message(message_id, ...)` for the approval-done indicator: use ❌/✅ based on exit code
- `_execute_and_store_result` must include `command_status` in the DDB `update_expr`
- Trust callback DDB update must also include `command_status`
- All 5 code paths: `manual_approve`, `approve_trust`, `trust_callback`, `auto_approve`, `grant_execute`

### Non-functional
- No new DDB GSI required; `command_status` is an attribute on existing records
- Backward compatible: existing records without `command_status` are treated as unknown
- No performance impact (exit code extraction already happens)

## Interface Contract
- DDB schema addition: `command_status: 'success' | 'failed'` on execute request records
- Affects tables: `TABLE_NAME` (approval-requests table)
- `_execute_and_store_result` return dict unchanged (internal change only)
