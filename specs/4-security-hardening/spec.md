# Sprint 4: Security Hardening

## Overview

Sprint 4 目標：修復 3 個安全漏洞 + 2 個運維缺口。

| ID | 類型 | 摘要 |
|----|------|------|
| bouncer-sec-006 | Security | os.environ credential race condition |
| bouncer-sec-007 | Security | presigned URL 無審批通知（無可見性） |
| bouncer-sec-008 | Security | Grant pattern ReDoS |
| bouncer-ops-001 | Ops | Duration Alarm 閾值 600000ms → 50000ms |
| bouncer-ops-003 | Ops | SNS Topic 無 Subscription（告警送不出） |

---

## User Stories

### Story 1: Credential Isolation (bouncer-sec-006)

**As a** Bouncer admin,
**I want** cross-account commands to use isolated credentials,
**So that** concurrent requests on a warm Lambda cannot use each other's AWS credentials.

#### Background

`execute_command()` (src/commands.py L382-430) 在執行跨帳號命令時，會將 STS assume role 取得的臨時 credentials 寫入 `os.environ`。Lambda 是 single-process model，但 Python runtime 可能在同一 invocation 內有 async/concurrent handler path；且未來若啟用 Lambda SnapStart 或 provisioned concurrency warm pool，os.environ 是 process-level shared state，會造成 credential 互相覆蓋。

#### Acceptance Scenarios

**Scenario 1: Concurrent cross-account requests**
**Given** two concurrent requests targeting different AWS accounts
**When** both execute simultaneously on a warm Lambda
**Then** each request uses only its own account's credentials

**Scenario 2: Specific account isolation**
**Given** request A targets account 992382394211 (Dev)
**When** request B targets account 841882238387 (1st) concurrently
**Then** request A's commands never execute with account B's credentials
**And** request B's commands never execute with account A's credentials

**Scenario 3: Default account unaffected**
**Given** a request targeting the default account (190825685292)
**When** it executes without assume role
**Then** it uses the Lambda execution role directly
**And** no os.environ modification occurs

**Scenario 4: Assume role failure rollback**
**Given** a request that requires assume role
**When** STS assume role fails (e.g., role not found, permission denied)
**Then** the original environment is not modified
**And** a clear error message is returned: `❌ Assume role 失敗: {reason}`

#### Edge Cases

- 單帳號請求（Default）不動 os.environ，不受影響
- STS assume role 失敗時 os.environ 保持原狀
- 超高併發（>10 concurrent）時不 deadlock
- awscli `create_clidriver()` 能正確使用傳入的 session credentials

---

### Story 2: Presigned URL Visibility (bouncer-sec-007)

**As a** Bouncer admin,
**I want** every presigned URL generation to trigger a Telegram notification,
**So that** I have visibility into who is generating presigned URLs, for what files, and when.

#### Background

`mcp_tool_request_presigned()` 和 `mcp_tool_request_presigned_batch()` (src/mcp_presigned.py L315, L574) 目前無需人工審批即可直接生成 presigned URL。雖有 rate limit (5 req/60s per source)，但生成時完全無通知，admin 無法知道何時有 URL 被產生。

#### Acceptance Scenarios

**Scenario 1: Single presigned URL notification**
**Given** a client requests a presigned URL via `bouncer_request_presigned`
**When** the presigned URL is successfully generated
**Then** a Telegram notification is sent with format:
```
📎 Presigned URL 已生成
source: {source}
file: {filename}
expires: {expires_in}s
account: {account_id}
```
**And** the notification does NOT contain the presigned URL itself (防洩漏)

**Scenario 2: Batch presigned URL notification**
**Given** a client requests batch presigned URLs via `bouncer_request_presigned_batch`
**When** the presigned URLs are successfully generated
**Then** a single Telegram notification is sent summarizing the batch:
```
📎 Presigned URL Batch 已生成
source: {source}
files: {count} 個
expires: {expires_in}s
account: {account_id}
```

**Scenario 3: Failed request — no notification**
**Given** a presigned URL request fails (rate limit, validation error)
**When** no URL is generated
**Then** no Telegram notification is sent

**Scenario 4: Silent notification**
**Given** a presigned URL is generated
**When** the Telegram notification is sent
**Then** it uses silent mode (`send_telegram_message_silent`) to avoid disturbing admin

#### Edge Cases

- Rate limit 觸發時不發通知（因為 URL 未生成）
- Batch 中部分檔案失敗、部分成功時，通知只列成功數量
- 通知發送本身失敗不影響 presigned URL 的回傳（fire-and-forget）
- 通知中絕不包含 presigned URL 本身

---

### Story 3: Grant Pattern Safety (bouncer-sec-008)

**As a** Bouncer admin,
**I want** grant patterns to be validated against ReDoS attacks,
**So that** a malicious or poorly-crafted pattern cannot cause Lambda timeout via catastrophic backtracking.

#### Background

`compile_pattern()` (src/grant.py L84-122) 將 grant pattern 編譯為 regex。目前的 `_glob_to_regex()` (L125-148) 將 `*` 轉為 `\S*`、`**` 轉為 `.*`。如果使用者提交含有大量 wildcard 或特殊排列的 pattern，可能產生 catastrophic backtracking，例如：
- `*` 重複多次 → 多個 `\S*` 串聯 → `\S*\S*\S*...` 在不匹配時指數回溯
- 超長 pattern 產生超大 regex

#### Acceptance Scenarios

**Scenario 1: Normal pattern — accepted**
**Given** a grant pattern `aws s3 cp s3://bucket/{uuid}/*.html s3://target/*.html`
**When** `compile_pattern()` is called
**Then** the pattern compiles successfully
**And** matching works correctly

**Scenario 2: Excessive wildcards — rejected**
**Given** a grant pattern containing more than 5 `*` wildcards
**When** `compile_pattern()` is called
**Then** a `ValueError` is raised with message: `Pattern 含有過多 wildcard（上限 5 個）`

**Scenario 3: Consecutive double-star — rejected**
**Given** a grant pattern containing `****` or `** **`
**When** `compile_pattern()` is called
**Then** a `ValueError` is raised with message: `Pattern 含有不合法的連續 wildcard`

**Scenario 4: Excessively long pattern — rejected**
**Given** a grant pattern longer than 200 characters
**When** `compile_pattern()` is called
**Then** a `ValueError` is raised with message: `Pattern 長度超過上限（200 字元）`

**Scenario 5: Regex compilation failure — graceful error**
**Given** a pattern that somehow produces invalid regex
**When** `re.compile()` raises `re.error`
**Then** a clear `ValueError` is raised with message: `Pattern 編譯失敗: {re.error message}`

**Scenario 6: Performance under attack pattern**
**Given** a pattern with 5 wildcards (the maximum allowed)
**When** matched against a 1000-character non-matching string
**Then** `match_pattern()` completes within 100ms

#### Edge Cases

- 空 pattern → 視為 exact match（空字串）
- Pattern 只含 placeholder 無 wildcard → 不受 wildcard 限制
- `*` 出現在 placeholder `{name}` 內部 → 不算 wildcard
- 既有 grant 的 pattern 若超過新限制 → `match_pattern()` 在 runtime 失敗時 catch + log

---

### Story 4: Alarm Correctness (bouncer-ops-001)

**As a** Bouncer admin,
**I want** the Lambda Duration alarm threshold to be 50000ms (50 seconds),
**So that** I am alerted when Lambda execution approaches the 60-second timeout, not only at 600 seconds (which is impossible given the 60s timeout).

#### Background

`template.yaml` L453 設定 `LambdaDurationAlarm` 的 `Threshold: 600000`（600 秒 = 10 分鐘）。但 Lambda timeout 是 60 秒 (L43)，所以 600000ms 的閾值永遠不會被觸發。正確值應為 50000ms（60 秒 timeout 的 ~83%），留 10 秒緩衝。

#### Acceptance Scenarios

**Scenario 1: Alarm triggers on slow execution**
**Given** the Lambda Duration alarm threshold is set to 50000ms
**When** a Lambda invocation takes 55000ms (p99)
**Then** the CloudWatch alarm transitions to ALARM state
**And** notification is sent via SNS

**Scenario 2: Normal execution — no alarm**
**Given** the Lambda Duration alarm threshold is set to 50000ms
**When** Lambda invocations are all under 50000ms (p99)
**Then** the alarm stays in OK state

#### Validation

- `template.yaml` 中 `LambdaDurationAlarm.Properties.Threshold` = `50000`
- 部署後 CloudWatch console 確認 alarm 設定正確

---

### Story 5: Alert Delivery (bouncer-ops-003)

**As a** Bouncer admin,
**I want** the SNS AlarmNotificationTopic to have at least one subscription,
**So that** CloudWatch alarms actually deliver notifications instead of firing into the void.

#### Background

`template.yaml` L413-415 建立了 `AlarmNotificationTopic` SNS Topic，所有 CloudWatch Alarms 都發送到此 topic。但目前沒有任何 Subscription，所以告警觸發時完全不會有人收到通知。

#### Acceptance Scenarios

**Scenario 1: Email subscription exists**
**Given** the `ALARM_EMAIL` parameter is provided during deployment
**When** the stack is created/updated
**Then** an SNS email subscription is created for `AlarmNotificationTopic`
**And** the subscriber receives a confirmation email

**Scenario 2: No email parameter — no subscription created**
**Given** the `ALARM_EMAIL` parameter is empty or not provided
**When** the stack is created/updated
**Then** no subscription resource is created
**And** no deployment error occurs

**Scenario 3: Alarm delivery end-to-end**
**Given** an email subscription is confirmed
**When** any CloudWatch alarm transitions to ALARM state
**Then** the subscriber receives an email notification with alarm details

#### Edge Cases

- `ALARM_EMAIL` 為空字串 → 使用 `AWS::CloudFormation::Condition` 跳過 Subscription 建立
- 部署更新時 email 從有到空 → Subscription 被刪除
- 多個 email → 未來可擴展為 comma-separated，但 Sprint 4 只支援單一 email
