# Bouncer Sprint 8 — Full Specification

> Generated: 2026-03-01
> Sprint Focus: CFN import automation, deploy reliability, REST Unicode normalization, CLI fixes, upload verification, trust expiry notifications

---

## Task 1: bouncer-sprint8-001 — fix: import LambdaLogGroup into CFN（用 SAM-transformed template）

### User Story
As a **DevOps operator**,
I want the Lambda log group `/aws/lambda/bouncer-prod-function` to be managed by CloudFormation,
So that retention policies and other log group settings are declarative and version-controlled.

### Background
Lambda auto-creates its log group on first invocation, before the CFN stack deploys. This means
`AWS::Logs::LogGroup` in `template.yaml` would fail with "already exists". Sprint 7 added
auto-import logic in `deployer/scripts/sam_deploy.py` for generic resources, but the
`LambdaLogGroup` was explicitly **excluded** from the template (see `template.yaml:530`):

```
# LambdaLogGroup: NOT in CFN — manual import needed.
# The log group /aws/lambda/bouncer-prod-function exists outside CFN.
# CFN IMPORT requires a SAM-transformed template (not raw SAM YAML).
```

The challenge: CFN `create-change-set --change-set-type IMPORT` requires a **transformed** (packaged)
CloudFormation template, not the raw SAM YAML. The existing `CloudFormationImporter` in `sam_deploy.py`
uses the raw template, which won't work for import.

Additionally, `_RESOURCE_ID_KEYS` in `sam_deploy.py` is missing the `AWS::Logs::LogGroup` resource type
mapping (the identifier key should be `LogGroupName`).

### Acceptance Scenarios

**Scenario 1: Successful LambdaLogGroup import**
- Given: The log group `/aws/lambda/bouncer-prod-function` exists outside the CFN stack
- And: `template.yaml` defines `LambdaLogGroup` as `AWS::Logs::LogGroup`
- When: `sam_deploy.py` detects the "already exists" conflict during deploy
- Then: It runs `sam build && sam package` to produce a transformed template URL
- And: Uses the transformed template with `create-change-set --change-set-type IMPORT`
- And: The import succeeds; subsequent deploy uses the imported resource

**Scenario 2: Dry-run import shows plan**
- Given: `--dry-run-import` flag is set
- When: Conflict is detected for LambdaLogGroup
- Then: The import plan is printed with logical ID, resource type, and physical ID
- And: No actual import or deploy occurs

**Scenario 3: Resource ID key mapping for LogGroup**
- Given: `_RESOURCE_ID_KEYS` does not contain `AWS::Logs::LogGroup`
- When: The import is attempted for a LogGroup
- Then: It falls back to `"Id"` which is incorrect
- Expected fix: Add `"AWS::Logs::LogGroup": "LogGroupName"` to `_RESOURCE_ID_KEYS`

**Scenario 4: Transformed template caching**
- Given: `sam build && sam package` has been run for the import
- When: The import succeeds and a retry deploy is triggered
- Then: The already-built/packaged artifacts are reused (no redundant build)

### Implementation Notes
- **Files:** `deployer/scripts/sam_deploy.py`, `template.yaml`
- **Add to template.yaml:** Uncomment/add `LambdaLogGroup` resource definition:
  ```yaml
  LambdaLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: !Sub "/aws/lambda/${BouncerFunction}"
      RetentionInDays: 30
    DeletionPolicy: Retain
  ```
- **Add to `_RESOURCE_ID_KEYS`:** `"AWS::Logs::LogGroup": "LogGroupName"`
- **Modify `import_resources()`:** When dealing with SAM templates, run `sam build` + `sam package --output-template-file` to get the transformed template, then pass it to `create-change-set` via `--template-url` or `--template-body`
- **DeletionPolicy:** Must be `Retain` for imported resources
- **Buildspec integration:** The CodeBuild buildspec already runs `sam build`; consider reusing the artifact

### Tests Required
- unit test: `tests/test_sam_deploy.py` — test `_physical_id_to_identifier` returns `{"LogGroupName": ...}` for `AWS::Logs::LogGroup`
- unit test: test `CloudFormationImporter.import_resources()` with LogGroup conflict (mock boto3)
- unit test: test that `parse_conflicts()` correctly extracts LogGroup conflicts from error output
- integration test (manual): run `--dry-run-import` against the real stack to verify plan output

---

## Task 2: bouncer-sprint8-002 — fix: deploy 失敗訊息截斷，自動存關鍵錯誤行到 DynamoDB

### User Story
As a **DevOps operator** viewing deploy history,
I want failed deploy records to include the key error lines (not just a generic "FAILED" status),
So that I can diagnose failures without digging through CodeBuild logs.

### Background
When a deploy fails, `src/deployer.py:388` stores only `{'status': 'FAILED', 'error_message': str(e)}`.
The `error_message` is the Python exception string, which is often vague (e.g., "啟動部署失敗: ...").

For CodeBuild-triggered deploys, the actual failure details are in the Step Functions execution output
or CodeBuild build logs. When `get_deploy_status()` (line 420) detects a FAILED status from SFN,
it updates the record but does **not** extract or store the build error details.

The deploy failure message in Telegram notifications is also truncated — the full CodeBuild output
is too long for a Telegram message, and no intelligent extraction is done.

### Acceptance Scenarios

**Scenario 1: Store error lines on deploy failure**
- Given: A CodeBuild deploy fails with a SAM/CFN error
- When: `get_deploy_status()` detects `sfn_status == 'FAILED'`
- Then: It fetches the SFN execution history to extract the error cause
- And: Stores up to 5 key error lines in `DynamoDB.error_lines` (list attribute)
- And: The `error_message` field contains a human-readable summary

**Scenario 2: Error line extraction from CFN events**
- Given: The deploy fails due to a CloudFormation resource creation error
- When: Error lines are extracted
- Then: Lines matching "CREATE_FAILED", "UPDATE_FAILED", "ROLLBACK", or "already exists" are prioritized
- And: Duplicate/redundant lines are deduplicated

**Scenario 3: Truncation safety for DynamoDB**
- Given: The full error output is > 400KB
- When: Error lines are extracted and stored
- Then: Each line is truncated to 500 characters max
- And: Total `error_lines` list size stays under 5 entries

**Scenario 4: Telegram notification includes error summary**
- Given: A deploy fails and error lines are stored
- When: The Telegram failure notification is sent
- Then: It includes the top 3 error lines (not the entire log)
- And: Total message length stays within Telegram's 4096 char limit

**Scenario 5: deploy_history shows error details**
- Given: A failed deploy record exists with `error_lines`
- When: `bouncer_deploy_history` is called
- Then: Each history entry includes `error_lines` when present

### Implementation Notes
- **File:** `src/deployer.py`
- **Functions:** `get_deploy_status()`, `update_deploy_record()`
- **New helper:** `_extract_error_lines(sfn_output: str) -> list[str]` — regex-based extraction of key error patterns
- **Patterns to match:**
  - `CREATE_FAILED` / `UPDATE_FAILED` / `DELETE_FAILED`
  - `ROLLBACK_IN_PROGRESS` / `ROLLBACK_COMPLETE`
  - `already exists`
  - `ResourceStatusReason`
  - Python traceback last lines
- **DynamoDB schema:** Add `error_lines: List[str]` to deploy history records
- **Telegram:** Modify deploy failure notification to include extracted lines

### Tests Required
- unit test: `tests/test_deployer.py` — test `_extract_error_lines()` with various CodeBuild/CFN error outputs
- unit test: test that `update_deploy_record()` correctly stores `error_lines`
- unit test: test truncation behavior (line > 500 chars, > 5 lines)
- unit test: test Telegram notification message length stays within limit

---

## Task 3: bouncer-sprint8-003 — fix: REST endpoint `handle_clawdbot_request` 缺少 Unicode 正規化

### User Story
As a **security engineer**,
I want the REST API endpoint `handle_clawdbot_request` to normalize Unicode in incoming commands,
So that attackers cannot bypass blocked-command detection using Unicode lookalike characters.

### Background
The MCP path (`mcp_execute.py:195`) already calls `_normalize_command()` which handles:
1. Zero-width/invisible character removal (`\u200b`, `\ufeff`, etc.)
2. Unicode space → ASCII space
3. Whitespace collapsing

However, the REST endpoint `handle_clawdbot_request()` in `src/app.py:411` does **not** call
`_normalize_command()`. It simply does `command = body.get('command', '').strip()` (line ~433).
This means REST API callers can inject Unicode-disguised commands that bypass `get_block_reason()`.

Example attack: `aws iam delete\u200b-user` would pass the block check because the invisible
zero-width space makes the string not match the blocked pattern `delete-user`.

### Acceptance Scenarios

**Scenario 1: Unicode normalization applied to REST commands**
- Given: A REST API request with `command = "aws iam delete\u200b-user --user-name test"`
- When: `handle_clawdbot_request()` processes it
- Then: The command is normalized to `"aws iam delete-user --user-name test"`
- And: The block check correctly identifies it as a dangerous command

**Scenario 2: Unicode spaces normalized**
- Given: A REST request with `command = "aws\u00a0s3\u2003ls"` (non-breaking space + em space)
- When: The command is normalized
- Then: It becomes `"aws s3 ls"` (standard ASCII spaces)

**Scenario 3: Normal commands unaffected**
- Given: A REST request with a standard ASCII command
- When: Normalization runs
- Then: The command is unchanged (no regression)

**Scenario 4: NFKC normalization for lookalike characters**
- Given: A command using fullwidth Latin characters like `ａｗｓ ｓ３ ｌｓ`
- When: Normalization runs
- Then: Characters are normalized to their ASCII equivalents via `unicodedata.normalize('NFKC', ...)`

### Implementation Notes
- **File:** `src/app.py`
- **Function:** `handle_clawdbot_request()` (~line 433)
- **Change:** Import `_normalize_command` from `mcp_execute` and apply it to the `command` variable before any block/risk checks
- **Optional enhancement:** Add NFKC normalization step to `_normalize_command()` itself (benefits both REST and MCP paths):
  ```python
  import unicodedata
  cmd = unicodedata.normalize('NFKC', cmd)
  ```
- **Security note:** This is a SEC-003 compliance issue — Unicode normalization must happen before any policy check

### Tests Required
- unit test: `tests/test_app.py` — test `handle_clawdbot_request` with Unicode-disguised commands
- unit test: `tests/test_mcp_execute.py` — test `_normalize_command` with NFKC normalization (fullwidth chars)
- unit test: test that invisible chars in REST commands are stripped before block check
- security test: verify that known Unicode bypass patterns are caught after normalization

---

## Task 4: bouncer-sprint8-004 — fix: `bouncer_deploy_history` CLI `--args` 無法使用

### User Story
As a **CLI user** using `mcporter call bouncer.bouncer_deploy_history --args '{"project":"bouncer"}'`,
I want the `--args` parameter to correctly pass the JSON payload to the Lambda MCP handler,
So that I can query deploy history from the command line.

### Background
The `bouncer_deploy_history` tool is defined in the Lambda HTTP server (`src/tool_schema.py:261`)
with parameters `project` (required, string) and `limit` (optional, integer, default 10).

The issue: when using `mcporter call bouncer.bouncer_deploy_history --args '{"project":"bouncer"}'`,
the `--args` JSON payload may not be correctly deserialized or passed through to the Lambda handler.
The mcporter CLI parses `--args` as a JSON string, but the HTTP transport may double-encode it or
fail to merge it with the MCP `tools/call` request body.

This needs investigation: the bug could be in:
1. **mcporter CLI** — `--args` parsing/serialization
2. **Bouncer HTTP API** — request body parsing in `handle_mcp_request()`
3. **Tool schema mismatch** — the schema expects `project` but CLI sends differently

### Acceptance Scenarios

**Scenario 1: CLI with --args JSON works**
- Given: `mcporter call bouncer.bouncer_deploy_history --args '{"project":"bouncer","limit":5}'`
- When: The command is executed
- Then: The Lambda receives `arguments: {"project": "bouncer", "limit": 5}`
- And: Returns the deploy history for the "bouncer" project

**Scenario 2: CLI with key=value syntax works**
- Given: `mcporter call bouncer.bouncer_deploy_history project=bouncer limit=5`
- When: The command is executed
- Then: Same result as Scenario 1

**Scenario 3: Missing required parameter**
- Given: `mcporter call bouncer.bouncer_deploy_history --args '{}'`
- When: The command is executed
- Then: Returns error `"Missing required parameter: project"`

**Scenario 4: Integer coercion for limit**
- Given: `--args '{"project":"bouncer","limit":"5"}'` (limit as string)
- When: The handler processes it
- Then: `int(arguments.get('limit', 10))` correctly coerces to 5

### Implementation Notes
- **Investigation needed:** First reproduce the exact error by testing the HTTP API directly:
  ```bash
  curl -X POST https://n8s3f1mus6.execute-api.us-east-1.amazonaws.com/prod/mcp \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"bouncer_deploy_history","arguments":{"project":"bouncer"}}}'
  ```
- **File (if Lambda-side):** `src/deployer.py` — `mcp_tool_deploy_history()`
- **File (if mcporter-side):** mcporter config for bouncer server (HTTP transport)
- **File (if schema-side):** `src/tool_schema.py` — ensure schema matches handler expectations
- **Possible fix:** The `limit` parameter schema says `type: "integer"` but JSON from CLI might send string; add `int()` coercion (already present at `deployer.py:610`)

### Tests Required
- integration test: curl the Lambda HTTP endpoint directly with `bouncer_deploy_history` payload
- unit test: `tests/test_deployer.py` — test `mcp_tool_deploy_history()` with various argument formats
- CLI test: test `mcporter call` with `--args` and `key=value` syntax
- edge case: test with missing project, string limit, negative limit

---

## Task 5: bouncer-sprint8-005 — fix: EarlyValidation 錯誤時明確 CFN import 提示（sam_deploy.py）

### User Story
As a **DevOps operator** reading deploy failure messages,
I want CFN "already exists" errors to include a clear remediation message with import instructions,
So that I know exactly what to do without searching documentation.

### Background
When `sam_deploy.py` encounters an "already exists" error but cannot parse the resource details
(line 433), it prints a generic error:
```
ERROR: Deploy failed with 'already exists' error but could not parse resource details for import.
Manual intervention required.
```

For CFN **EarlyValidation** failures (where CFN rejects the changeset before even creating resources),
the error message format is different from the standard "already exists" pattern, so the regex
`_ALREADY_EXISTS_RE` may not match. The user sees a cryptic failure with no actionable guidance.

### Acceptance Scenarios

**Scenario 1: EarlyValidation with clear import hint**
- Given: A deploy fails with a CFN EarlyValidation "resource already exists" error
- When: `sam_deploy.py` processes the failure
- Then: The error message includes:
  - The resource logical ID and type (if parseable)
  - The exact `aws cloudformation import` command to run
  - A link to the AWS documentation for CFN import

**Scenario 2: Unparseable "already exists" with fallback hint**
- Given: The "already exists" text is detected but resource details can't be parsed
- When: Error is reported
- Then: The message suggests: "Run `sam_deploy.py --dry-run-import` to diagnose, or check the stack events in CloudFormation console"

**Scenario 3: Non-"already exists" failure unchanged**
- Given: A deploy fails due to an unrelated error (e.g., IAM permission denied)
- When: `sam_deploy.py` processes it
- Then: The error handling is unchanged (no false import hints)

**Scenario 4: Multiple resources already exist**
- Given: 3 resources trigger "already exists" but only 2 can be parsed
- When: Import is attempted
- Then: The 2 parseable resources are imported, and the 3rd gets the fallback hint

### Implementation Notes
- **File:** `deployer/scripts/sam_deploy.py`
- **Location:** Lines 430-440 (the `if not conflicts:` block)
- **Enhancement 1:** Improve the error message with actionable steps:
  ```python
  print(
      "ERROR: Deploy failed with 'already exists' error but could not "
      "parse resource details.\n"
      "Suggested remediation:\n"
      f"  1. Check stack events: aws cloudformation describe-stack-events --stack-name {stack}\n"
      f"  2. Run: python sam_deploy.py --dry-run-import\n"
      f"  3. Manual import: aws cloudformation create-change-set --change-set-type IMPORT ...\n"
      "  4. Docs: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/resource-import.html",
      file=sys.stderr,
  )
  ```
- **Enhancement 2:** Add EarlyValidation-specific regex pattern to catch the different error format
- **Enhancement 3:** When import succeeds, print the import command that was used (for operator education)

### Tests Required
- unit test: `tests/test_sam_deploy.py` — test error messaging when parse fails
- unit test: test EarlyValidation error pattern matching
- unit test: test that non-"already exists" failures don't produce import hints
- unit test: test multi-resource partial parse scenario

---

## Task 6: bouncer-sprint8-006 — feat: `bouncer_upload_batch` 上傳後自動驗證 S3 結果

### User Story
As an **MCP client** uploading files via `bouncer_upload_batch`,
I want the system to automatically verify that uploaded files actually exist in S3 after upload,
So that silent upload failures (e.g., large file truncation) are detected immediately.

### Background
`bouncer_upload_batch` (`src/mcp_upload.py:501`) uploads files to the staging bucket
`bouncer-uploads-{account_id}`. The upload uses `s3.put_object()` (line ~273 for single,
similar for batch). However, there's a known issue where large base64 payloads (>500KB)
can silently fail within Lambda — the upload function returns success, but the file doesn't
actually land in S3 or is truncated.

Currently, after upload, the function returns the `s3_uri` without verifying the object exists
and has the expected size. The caller has to manually run `aws s3 ls` to verify.

### Acceptance Scenarios

**Scenario 1: Successful upload with verification**
- Given: A batch of 3 files (each < 100KB)
- When: `bouncer_upload_batch` completes
- Then: Each file's S3 existence is verified via `s3.head_object()`
- And: The response includes `verified: true` and `s3_size` for each file
- And: Sizes match the original content sizes

**Scenario 2: Failed verification detected**
- Given: A file upload silently fails (object not in S3)
- When: Post-upload verification runs `head_object()`
- Then: The file is marked `verified: false` in the response
- And: An `error` field explains "S3 object not found after upload"
- And: The overall batch status indicates partial failure

**Scenario 3: Size mismatch detected**
- Given: A large file is uploaded but S3 only has a truncated version
- When: `head_object()` returns a different `ContentLength` than expected
- Then: The file is marked `verified: false` with error "Size mismatch: expected X, got Y"

**Scenario 4: Verification failure doesn't block response**
- Given: The `head_object()` call itself fails (e.g., permission error)
- When: Verification is attempted
- Then: The file is marked `verification_error: "..."` but the upload result is still returned
- And: The overall response is not an error (verification is best-effort)

**Scenario 5: Trust-approved uploads also verified**
- Given: Upload is auto-approved via trust session
- When: Upload completes
- Then: Verification still runs (trust doesn't skip verification)

### Implementation Notes
- **File:** `src/mcp_upload.py`
- **Function:** Add `_verify_upload(s3_client, bucket, key, expected_size) -> dict` helper
- **Integration point:** After each `s3.put_object()` succeeds, call `s3.head_object()` and compare `ContentLength`
- **Response format enhancement:** Each file result gets additional fields:
  ```json
  {
    "filename": "index.html",
    "s3_uri": "s3://bouncer-uploads-.../2026-03-01/uuid/index.html",
    "status": "uploaded",
    "verified": true,
    "s3_size": 12345,
    "expected_size": 12345
  }
  ```
- **Performance:** `head_object()` is fast (~50ms per call), acceptable for up to 50 files
- **Error handling:** Wrap verification in try/except; never let verification failure mask upload success

### Tests Required
- unit test: `tests/test_mcp_upload.py` — test `_verify_upload()` with matching sizes
- unit test: test `_verify_upload()` with size mismatch
- unit test: test `_verify_upload()` when `head_object()` raises NoSuchKey
- unit test: test batch upload response includes verification fields
- unit test: test that verification failure doesn't prevent response
- integration test (manual): upload a batch and check all files have `verified: true`

---

## Task 7: bouncer-sprint8-007 — feat: trust session 過期後通知受影響的 pending 請求

### User Story
As a **Telegram approver** (Steven),
I want to be notified when a trust session expires and there are pending requests that were expecting auto-approval,
So that I know to manually review those requests before they time out.

### Background
Trust sessions (`src/trust.py`) allow temporary auto-approval of commands from a specific source.
When a trust session expires (checked in `should_trust_approve()` at line ~370), pending requests
that arrive after expiry will fall through to manual approval. However, the approver gets no
notification that the trust session has ended.

The existing infrastructure for expiry handling:
- `src/notifications.py` has `post_notification_setup()` which schedules EventBridge one-time triggers
- `src/scheduler_service.py` creates EventBridge Scheduler schedules for cleanup
- `src/app.py:87` has `handle_cleanup_expired()` for request expiry cleanup
- Trust sessions have `expires_at` (Unix timestamp) and TTL in DynamoDB

The gap: there's no EventBridge schedule created when a trust session starts, and no handler
for trust session expiry notifications.

### Acceptance Scenarios

**Scenario 1: Trust expiry notification sent**
- Given: A trust session is active for scope "deploy" bound to source "Private Bot"
- And: There are 2 pending requests from "Private Bot" in the queue
- When: The trust session expires
- Then: A Telegram notification is sent:
  "🔒 Trust session expired for scope 'deploy' (Private Bot). 2 pending requests need manual review."
- And: The notification includes inline buttons to view/approve each pending request

**Scenario 2: Trust expiry with no pending requests**
- Given: A trust session expires
- And: There are no pending requests from the bound source
- When: The expiry handler runs
- Then: A brief notification is sent: "🔒 Trust session expired for scope 'deploy' (Private Bot). No pending requests."
- And: No inline buttons are shown

**Scenario 3: Trust session revoked early (no expiry notification)**
- Given: A trust session is revoked via `bouncer_trust_revoke`
- When: The revocation completes
- Then: The scheduled expiry notification is cancelled (if exists)
- And: A revocation confirmation is sent instead

**Scenario 4: EventBridge schedule creation on trust session start**
- Given: `create_trust_session()` is called
- When: The trust session is created successfully
- Then: An EventBridge Scheduler one-time schedule is created for `expires_at`
- And: The schedule targets the Lambda with `action: "trust_expired"` payload

### Implementation Notes
- **Files:** `src/trust.py`, `src/notifications.py`, `src/app.py`, `src/scheduler_service.py`
- **New handler in `app.py`:** `handle_trust_expired(event)` — triggered by EventBridge when trust session expires
  - Query DynamoDB for pending requests matching the trust session's `bound_source` and `trust_scope`
  - Build Telegram notification with request details
  - Send via `send_telegram_message()`
- **Modify `create_trust_session()` in `trust.py`:**
  - After creating the DynamoDB record, schedule EventBridge expiry trigger (similar to `post_notification_setup()`)
  - Store the schedule name in the trust session record for later cancellation
- **Modify `revoke_trust_session()` in `trust.py`:**
  - Cancel the EventBridge schedule on revocation
- **Lambda routing in `app.py`:**
  - Add condition: `if event.get('action') == 'trust_expired': return handle_trust_expired(event)`
- **Query for pending requests:**
  ```python
  # Scan for pending requests with matching source
  result = table.scan(
      FilterExpression='#s = :pending AND #src = :source',
      ExpressionAttributeNames={'#s': 'status', '#src': 'source'},
      ExpressionAttributeValues={':pending': 'pending', ':source': bound_source}
  )
  ```
  (Consider GSI on `status` for efficiency if table grows large)

### Tests Required
- unit test: `tests/test_trust.py` — test that `create_trust_session()` calls scheduler to create expiry schedule
- unit test: test that `revoke_trust_session()` cancels the scheduled notification
- unit test: `tests/test_app.py` — test `handle_trust_expired()` with pending requests
- unit test: test `handle_trust_expired()` with no pending requests
- unit test: test notification message format and content
- unit test: test EventBridge event routing in lambda_handler
