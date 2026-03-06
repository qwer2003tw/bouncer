# Bouncer Sprint 7 — Full Specification

> Generated: 2026-03-01
> Codebase: 13,848 LOC across 30 source files (`src/*.py`)

---

## Task 1: bouncer-sprint7-001 — fix: `bouncer_execute` `&&` 串接命令靜默失敗 (#30)

### User Story
As an **MCP client** (AI agent),
I want `bouncer_execute` to correctly execute chained commands like `aws s3 ls && aws sts get-caller-identity`,
So that I get the combined output instead of silent failure.

### Background
`execute_command()` in `src/commands.py:357` uses `awscli.clidriver.create_clidriver()` to execute commands in-process.
The `aws_cli_split()` function (`src/commands.py:233`) is a custom tokenizer that does NOT understand shell operators
(`&&`, `||`, `;`, `|`). When a user sends `aws cmd1 && aws cmd2`, the entire string is parsed as a single command,
causing `awscli` to receive unexpected tokens and fail silently (or produce cryptic errors).

### Acceptance Scenarios

**Scenario 1: Single `&&` chain**
- Given: command = `aws s3 ls --region us-east-1 && aws sts get-caller-identity`
- When: `execute_command()` is called
- Then: Both commands execute sequentially; output is concatenated; if cmd1 fails, cmd2 is skipped (shell `&&` semantics)

**Scenario 2: Multiple `&&` chains**
- Given: command = `aws s3 ls && aws iam list-users && aws sts get-caller-identity`
- When: `execute_command()` is called
- Then: All three commands run sequentially; all outputs concatenated; execution stops at first failure

**Scenario 3: Single command (no `&&`)**
- Given: command = `aws s3 ls`
- When: `execute_command()` is called
- Then: Behaves exactly as before (no regression)

**Scenario 4: `&&` inside quoted strings**
- Given: command = `aws logs filter-log-events --filter-pattern "foo && bar"`
- When: `execute_command()` is called
- Then: Treated as a single command (the `&&` is inside quotes, not a shell operator)

**Scenario 5: Risk scoring with chained commands**
- Given: command = `aws s3 ls && aws iam delete-user --user-name test`
- When: The pipeline processes this command
- Then: Each sub-command is individually checked for blocked/dangerous patterns; if any sub-command is blocked, the entire chain is blocked

### Implementation Notes
- **File:** `src/commands.py`
- **Function:** `execute_command()` / `_execute_locked()`
- Add a helper `_split_chain(command: str) -> list[str]` that splits on `&&` outside of quotes/brackets
- The split must respect the same quoting rules as `aws_cli_split()`
- Each sub-command goes through `_execute_locked()` individually
- Concatenate outputs with a separator like `\n--- cmd N ---\n`
- **Risk check integration:** `_normalize_command()` in `src/mcp_execute.py:71` and blocking checks need to handle each sub-command
- **Security:** Do NOT use `subprocess.Popen(shell=True)`. Keep in-process execution via `clidriver`

### Tests Required
- unit test: `tests/test_commands.py` — test `_split_chain()` with various patterns; test `execute_command()` with `&&` chains
- unit test: `tests/test_mcp_execute.py` — test that risk scoring applies to each sub-command in a chain
- edge cases: `&&` inside JSON, inside quotes, at beginning/end, double `&&`, empty segments

---

## Task 2: bouncer-sprint7-002 — fix: 過期請求按鈕未自動移除，用 EventBridge Scheduler 清除 (#21)

### User Story
As a **Telegram user** (Steven),
I want expired approval request buttons to be automatically removed from Telegram messages,
So that I don't accidentally tap on stale buttons that are already expired.

### Background
When a request expires (TTL passes), the DynamoDB record is cleaned up, but the Telegram message retains its inline keyboard buttons. Tapping an expired button produces an error. Currently there is no mechanism to proactively remove buttons after expiration.

The notification functions in `src/notifications.py` send messages via `_send_message()` which returns the Telegram API response containing `message_id`. The `update_message()` function in `src/telegram.py:186` can edit a message with `remove_buttons=True`.

### Acceptance Scenarios

**Scenario 1: Button removal after command request expiry**
- Given: A command approval request with `request_id=REQ-001` is sent with a 60s timeout
- When: 60 seconds pass without action
- Then: The Telegram inline keyboard is removed (via `editMessageReplyMarkup` or `editMessageText`)

**Scenario 2: Button removal after upload request expiry**
- Given: An upload approval request is sent with timeout
- When: Timeout elapses
- Then: Buttons are removed; message is updated to show "⏰ 已過期"

**Scenario 3: Approved before expiry**
- Given: A request is approved within the timeout
- When: The scheduled cleanup fires
- Then: No-op (message was already updated by the callback handler)

**Scenario 4: No EventBridge permission issues**
- Given: Lambda function has `scheduler:CreateSchedule` and `scheduler:DeleteSchedule` permissions
- When: A new approval request is created
- Then: A one-time EventBridge Scheduler schedule is created for the expiry time

### Implementation Notes
- **Option A (Recommended): EventBridge Scheduler one-time schedule**
  - When `send_approval_request()` returns a successful message, create an EventBridge Scheduler one-time schedule
  - Schedule target: same Lambda function with a special event payload `{"action": "cleanup_expired", "request_id": "...", "message_id": 12345}`
  - In `src/app.py`, add a handler for this event type
  - **Files:** `src/notifications.py` (store message_id from send result, create schedule), `src/app.py` (handle cleanup event), `src/telegram.py` (already has `update_message`), `template.yaml` (IAM policy for Scheduler)
- **Option B: DynamoDB Streams + TTL event**
  - Use DynamoDB Streams with a filter on TTL deletions — more complex, higher latency
- **Choose Option A** for simplicity and precise timing
- Need to store `telegram_message_id` in the DynamoDB request item when the notification is sent
- `template.yaml` needs: `AWS::Scheduler::ScheduleGroup`, IAM policy additions for `scheduler:*`

### Tests Required
- unit test: `tests/test_notifications.py` — verify `message_id` is stored after sending notification
- unit test: `tests/test_app.py` — verify cleanup event handler calls `update_message(remove_buttons=True)`
- integration test: mock EventBridge Scheduler creation/deletion

---

## Task 3: bouncer-sprint7-003 — fix: mcp_history / telegram_commands 改用 GSI Query 取代 Scan

### User Story
As a **system operator**,
I want `mcp_history` and `telegram_commands` to use DynamoDB GSI Query instead of full table Scan,
So that query performance remains consistent as the table grows.

### Background
Current scan locations:
- `src/mcp_history.py:154` — `table.scan()` on requests table
- `src/mcp_history.py:198` — `cmd_table.scan()` on command-history table
- `src/mcp_history.py:320,328` — `table.scan()` for stats
- `src/telegram_commands.py:104,138,178,184` — `table.scan()` for `/trust`, `/pending`, `/stats` commands

Existing GSIs on the requests table (`template.yaml:82-93`):
- `source-created-index` (PK: `source`, SK: `created_at`) — INCLUDE projection (status only)
- `status-created-index` (PK: `status`, SK: `created_at`) — ALL projection

### Acceptance Scenarios

**Scenario 1: History query by status**
- Given: 10,000 items in the requests table
- When: `mcp_tool_history` is called with `status=approved`
- Then: Uses `status-created-index` GSI Query instead of Scan; returns results sorted by `created_at` DESC

**Scenario 2: History query by source**
- Given: Many items in the requests table
- When: `mcp_tool_history` is called with `source=Private Bot`
- Then: Uses `source-created-index` GSI Query; returns matching results

**Scenario 3: /pending command**
- Given: Telegram `/pending` command received
- When: `handle_pending_command()` executes
- Then: Uses `status-created-index` with `status=pending` instead of full Scan

**Scenario 4: /trust command**
- Given: Telegram `/trust` command received
- When: `handle_trust_command()` executes
- Then: Uses `status-created-index` or a type-based GSI to find `type=trust_session` records

**Scenario 5: /stats with time range**
- Given: `/stats 24` command
- When: `handle_stats_command()` executes
- Then: Uses GSI Query with `created_at >= since_ts` condition

**Scenario 6: Fallback for unfiltered queries**
- Given: A query with no filter parameters
- When: `mcp_tool_history` is called with no filters
- Then: Falls back to Scan (acceptable for admin use) with a log warning

### Implementation Notes
- **Files:** `src/mcp_history.py`, `src/telegram_commands.py`
- **Existing GSIs already cover most cases:**
  - `status-created-index` for `/pending` (status=pending), history by status
  - `source-created-index` for history by source
- For `/trust` (type=trust_session), need a new GSI or use `status-created-index` if trust sessions have a distinguishable status
- **Key change:** Replace `table.scan(FilterExpression=...)` with `table.query(IndexName=..., KeyConditionExpression=...)`
- The `source-created-index` has INCLUDE projection (only `status`). For full item data, either:
  1. Change projection to ALL (requires table replacement or adding attributes)
  2. Do a follow-up `get_item()` for each result (batch_get_item for efficiency)
  3. Accept limited fields for source-based queries
- **Recommendation:** Change `source-created-index` to `ProjectionType: ALL` in `template.yaml`
- **command-history table** may need its own GSI — check if it exists in template.yaml

### Tests Required
- unit test: `tests/test_history.py` — verify Query is used instead of Scan with moto mocks
- unit test: `tests/test_telegram_main.py` — verify `/pending`, `/trust`, `/stats` use Query
- performance test (manual): compare Scan vs Query with >1000 items

---

## Task 4: bouncer-sprint7-004 — fix: CloudWatch Logs 輸出截斷 (#27)

### User Story
As an **MCP client**,
I want `bouncer_execute` to return the full command output even when it's large,
So that I can see complete CloudWatch Logs query results.

### Background
`src/constants.py:178` sets `OUTPUT_MAX_INLINE = 3500` characters. Output longer than this is split into pages via `store_paged_output()` in `src/paging.py`. The paging system stores pages in DynamoDB and returns a `next_page` token.

The issue (#27) is that `awscli` commands like `aws logs filter-log-events` can produce very large output, and the paging mechanism may truncate rather than fully paginate. The `OUTPUT_PAGE_SIZE = 3000` (`src/constants.py:177`) determines page chunk size.

### Acceptance Scenarios

**Scenario 1: Large output is fully paginated**
- Given: A command produces 15,000 characters of output
- When: `execute_command()` returns and `store_paged_output()` is called
- Then: Output is split into 5 pages of 3000 chars each; all pages are stored in DynamoDB

**Scenario 2: MCP client can retrieve all pages**
- Given: Paginated output with 5 pages
- When: Client calls `bouncer_get_page` with each `next_page` token
- Then: All 5 pages are retrieved sequentially, reconstructing the full output

**Scenario 3: Telegram auto-sends remaining pages**
- Given: Command output is 10,000 characters
- When: Callback processes the result
- Then: `send_remaining_pages()` auto-sends pages 2+ to Telegram

**Scenario 4: Very large output (>100KB) is handled gracefully**
- Given: A command produces 200,000 characters
- When: The output is processed
- Then: Output is capped at a reasonable limit (e.g., 100KB) with a truncation notice; DynamoDB 400KB item limit is respected

### Implementation Notes
- **Files:** `src/paging.py`, `src/constants.py`, `src/callbacks.py`
- Review `store_paged_output()` in `src/paging.py:59` — ensure it pages ALL content, not just the first chunk
- Increase `OUTPUT_MAX_INLINE` from 3500 to a higher value (e.g., 8000) to reduce unnecessary pagination
- Add a hard cap for total output size (e.g., 100KB) before pagination to avoid DynamoDB cost explosion
- Verify `send_remaining_pages()` handles all pages (currently loops from page 2 to total)
- Consider S3 for very large outputs instead of DynamoDB

### Tests Required
- unit test: `tests/test_paging.py` (new or extend) — test pagination with 5K, 15K, 100K+ char outputs
- unit test: verify `bouncer_get_page` retrieves all pages correctly
- edge case: empty output, output exactly at boundary

---

## Task 5: bouncer-sprint7-005 — feat: sam_deploy.py 自動 import 已存在 CFN resource (#28)

### User Story
As a **deployer**,
I want `sam_deploy.py` to automatically import pre-existing CloudFormation resources when a deploy fails due to "resource already exists",
So that I don't have to manually run `aws cloudformation import` for each conflicting resource.

### Background
`deployer/scripts/sam_deploy.py` runs `sam deploy` via subprocess. When a stack update encounters a resource that already exists outside CFN (e.g., a DynamoDB table created manually), it fails with `CREATE_FAILED` and an "already exists" error.

The current script has no retry/import logic — it just exits with the subprocess return code.

### Acceptance Scenarios

**Scenario 1: Deploy with pre-existing resource**
- Given: `sam deploy` fails because a DynamoDB table already exists
- When: `sam_deploy.py` detects the "already exists" error
- Then: It automatically runs `aws cloudformation create-change-set --change-set-type IMPORT` for the conflicting resource, then retries the deploy

**Scenario 2: Deploy succeeds without conflicts**
- Given: No pre-existing resource conflicts
- When: `sam_deploy.py` runs
- Then: Normal `sam deploy` proceeds without import logic

**Scenario 3: Import fails**
- Given: An import attempt fails (e.g., resource identifier mismatch)
- When: The import changeset fails
- Then: Error is logged clearly with the resource logical ID and physical ID; deploy aborts with clear error message

**Scenario 4: Multiple conflicting resources**
- Given: Multiple resources already exist
- When: `sam_deploy.py` detects multiple conflicts
- Then: All are imported in a single changeset before retrying

### Implementation Notes
- **File:** `deployer/scripts/sam_deploy.py`
- After `subprocess.run(cmd)` returns non-zero:
  1. Parse CloudFormation events to find `CREATE_FAILED` with "already exists"
  2. Extract logical resource ID and resource type
  3. Build an import changeset using `aws cloudformation create-change-set --change-set-type IMPORT`
  4. Execute the import changeset
  5. Retry the original `sam deploy`
- Need `boto3` in the CodeBuild environment (already available)
- This is complex — consider a separate function `_try_import_existing_resources(stack_name, failed_events)`
- **Risk:** Import can change resource ownership. Must be careful with stateful resources (DynamoDB tables with data)

### Tests Required
- unit test: `deployer/tests/test_sam_deploy.py` (new) — mock subprocess and CloudFormation API calls
- test: simulate "already exists" error and verify import logic
- test: verify no import attempted on other error types

---

## Task 6: bouncer-sprint7-006 — fix: trust scope 加入 source 綁定驗證 (bouncer-sec-010)

### User Story
As a **security-conscious admin**,
I want trust sessions to be bound to the original `source` that was approved,
So that a different source cannot reuse another source's trust session.

### Background
`src/trust.py` currently matches trust sessions using `trust_scope + account_id` only (`get_trust_session()` at line 52). The `source` field is stored (line 118-119) but only for display — it does NOT participate in trust matching (as documented in line 5-7).

This means if Source A gets a trust session approved, Source B with the same `trust_scope` could theoretically piggyback on it.

### Acceptance Scenarios

**Scenario 1: Same source uses trust session**
- Given: Trust session created for source="Private Bot (deploy)" with trust_scope="session-abc"
- When: A request comes from source="Private Bot (deploy)" with trust_scope="session-abc"
- Then: Trust session matches; auto-approved

**Scenario 2: Different source cannot use trust session**
- Given: Trust session created for source="Private Bot (deploy)" with trust_scope="session-abc"
- When: A request comes from source="Public Bot" with trust_scope="session-abc"
- Then: Trust session does NOT match; requires manual approval

**Scenario 3: Backward compatibility — empty source**
- Given: Legacy trust session with no source binding (source='')
- When: Any request with matching trust_scope comes in
- Then: Trust session matches (backward compatible; warn in logs)

**Scenario 4: Source binding stored in DynamoDB**
- Given: A new trust session is created
- When: `create_trust_session()` is called
- Then: `bound_source` is stored alongside `source` (for matching)

### Implementation Notes
- **File:** `src/trust.py`
- **Functions:** `create_trust_session()` (line 94), `get_trust_session()` (line 52), `should_trust_approve()` (line 221)
- Add `source` parameter to `get_trust_session()` and add source matching:
  ```python
  if session.get('bound_source') and source and session['bound_source'] != source:
      return None  # Source mismatch
  ```
- Store `bound_source` in `create_trust_session()` (distinct from display-only `source`)
- Backward compatible: if `bound_source` is empty/missing, skip source check (legacy sessions)
- Also update `should_trust_approve()` and `should_trust_approve_upload()` to pass `source` through

### Tests Required
- unit test: `tests/test_trust.py` — test source binding match/mismatch
- unit test: test backward compatibility with legacy sessions (no bound_source)
- security test: verify cross-source trust reuse is blocked

---

## Task 7: bouncer-sprint7-007 — refactor: 消除重複函數 `send_telegram_message_to` / `_sanitize_filename`

### User Story
As a **developer**,
I want duplicate functions to be consolidated into a single location,
So that bug fixes and changes only need to be made in one place.

### Background
Duplicated functions found:
1. `send_telegram_message_to()`:
   - `src/telegram.py:175`
   - `src/telegram_commands.py:27`
2. `_sanitize_filename()`:
   - `src/mcp_presigned.py:54`
   - `src/mcp_upload.py:482`

### Acceptance Scenarios

**Scenario 1: send_telegram_message_to unified**
- Given: `telegram_commands.py` imports `send_telegram_message_to` from `telegram.py`
- When: Any telegram command handler calls `send_telegram_message_to()`
- Then: The canonical version in `telegram.py` is used

**Scenario 2: _sanitize_filename unified**
- Given: `_sanitize_filename` is defined in one canonical location (e.g., `utils.py`)
- When: Both `mcp_presigned.py` and `mcp_upload.py` need it
- Then: Both import from the same module

**Scenario 3: No behavioral change**
- Given: Both copies of each function are functionally identical
- When: The duplicate is removed and replaced with an import
- Then: All existing tests pass without modification

### Implementation Notes
- **send_telegram_message_to:**
  - Keep in `src/telegram.py:175` (canonical location for Telegram functions)
  - In `src/telegram_commands.py`, replace local definition at line 27 with: `from telegram import send_telegram_message_to`
- **_sanitize_filename:**
  - Move to `src/utils.py` (already has 279 LOC of utilities)
  - In `src/mcp_presigned.py:54` and `src/mcp_upload.py:482`, replace with: `from utils import sanitize_filename` (drop underscore for public API)
- Verify both copies are identical before deleting one; if they differ, merge the best of both

### Tests Required
- unit test: Run existing tests to verify no regression
- unit test: `tests/test_utils.py` — add test for `sanitize_filename` in its new location

---

## Task 8: bouncer-sprint7-008 — refactor: DynamoDB table 初始化統一到 db.py

### User Story
As a **developer**,
I want all DynamoDB table initialization to go through `src/db.py`,
So that table management is centralized and easier to mock in tests.

### Background
`src/db.py` already has a `_LazyTable` pattern for `table` and `accounts_table`. However, many other modules create their own boto3 DynamoDB resources:

- `src/rate_limit.py:23-30` — `_table` with lazy init
- `src/accounts.py:30-37` — `_accounts_table` with lazy init (redundant with `db.py`)
- `src/deployer.py:28-62` — `projects_table`, `history_table`, `locks_table`
- `src/mcp_execute.py:138` — `dynamodb.Table(SHADOW_TABLE_NAME)` inline
- `src/mcp_history.py:41-49` — `_get_dynamodb_resource()` + `_get_command_history_table()`
- `src/paging.py:23-30` — `_table` with lazy init
- `src/sequence_analyzer.py:59-67` — `_history_table` with lazy init
- `src/trust.py:41-48` — `_table` with lazy init

### Acceptance Scenarios

**Scenario 1: All tables accessed via db.py**
- Given: Any module needs a DynamoDB table
- When: It accesses the table
- Then: It imports from `db.py` (e.g., `from db import rate_limit_table`)

**Scenario 2: Lazy initialization preserved**
- Given: Lambda cold start
- When: Only certain modules are accessed
- Then: Only the tables actually used are initialized (no eager loading)

**Scenario 3: Test mocking simplified**
- Given: Tests use moto for DynamoDB
- When: `db.reset_tables()` is called in test teardown
- Then: All table caches are cleared across all modules

**Scenario 4: No behavioral change**
- Given: Refactored code
- When: All existing tests run
- Then: All pass without modification (other than import changes)

### Implementation Notes
- **File:** `src/db.py` — add new `_LazyTable` instances:
  ```python
  rate_limit_table = _LazyTable('RATE_LIMIT_TABLE', RATE_LIMIT_TABLE_NAME)
  deployer_projects_table = _LazyTable('PROJECTS_TABLE', PROJECTS_TABLE)
  deployer_history_table = _LazyTable('HISTORY_TABLE', HISTORY_TABLE)
  deployer_locks_table = _LazyTable('LOCKS_TABLE', LOCKS_TABLE)
  shadow_table = _LazyTable('SHADOW_TABLE', SHADOW_TABLE_NAME)
  command_history_table = _LazyTable('COMMAND_HISTORY_TABLE', COMMAND_HISTORY_TABLE_NAME)
  paging_table = _LazyTable('TABLE_NAME', TABLE_NAME)  # same as main table for paging
  trust_table = _LazyTable('TRUST_TABLE', TRUST_TABLE_NAME)
  sequence_history_table = _LazyTable('HISTORY_TABLE', HISTORY_TABLE_NAME)
  ```
- Update `reset_tables()` to reset ALL tables
- Update each module to import from `db.py` instead of local init
- Some tables share the same underlying DynamoDB table (e.g., `paging` uses the main `table`). Consolidate those.
- **Caution with deployer.py:** Tests set `deployer.history_table = moto_table` directly (line 26 comment). The `_LazyTable` proxy pattern should support test injection.

### Tests Required
- unit test: Run full test suite to verify no regression
- unit test: `tests/test_db.py` (new) — verify all `_LazyTable` instances initialize correctly
- unit test: verify `reset_tables()` clears all caches

---

## Task 9: bouncer-sprint7-009 — ops: Lambda Memory 256MB → 512MB

### User Story
As a **system operator**,
I want the Lambda function memory increased from 256MB to 512MB,
So that `awscli` import and execution don't cause OOM errors or slow cold starts.

### Background
`template.yaml:52` sets `MemorySize: 256` in the `Globals.Function` section. The `awscli` library is heavy (~100MB+ in memory), and combined with DynamoDB boto3 operations, 256MB is tight. Cold starts are slower due to memory pressure, and complex commands may OOM.

### Acceptance Scenarios

**Scenario 1: Memory increased in template**
- Given: `template.yaml` Globals.Function.MemorySize
- When: Value is changed from 256 to 512
- Then: Next deploy applies the new memory setting to all Lambda functions

**Scenario 2: No functional change**
- Given: Increased memory
- When: All existing functionality is tested
- Then: Everything works as before, but with better performance

**Scenario 3: Cost impact acceptable**
- Given: Memory doubled from 256MB to 512MB
- When: Monthly cost is estimated
- Then: Cost increase is marginal (Lambda pricing is per GB-second)

### Implementation Notes
- **File:** `template.yaml` — line 52
- Change: `MemorySize: 256` → `MemorySize: 512`
- This is a one-line change
- Consider also reviewing `Timeout: 60` — may need increase for complex chained commands (#001)
- Verify the deployer Lambda doesn't need separate memory config

### Tests Required
- validation: Verify `template.yaml` passes `sam validate`
- ops test: Post-deploy, confirm Lambda memory via `aws lambda get-function-configuration`

---
