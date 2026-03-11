# sprint30-003: bouncer_grant_execute HTTP API

## Summary

新增獨立的 `bouncer_grant_execute` MCP tool（HTTP endpoint），讓 Agent 可以透過 grant session 直接執行已授權的命令，不需走 `bouncer_execute` + `grant_id` 參數的間接路徑。

**決策：** 獨立 tool endpoint，不是複用 `bouncer_execute`。

## User Stories

### US-1: Agent 在 Grant Session 內執行命令

> 身為 Agent，當我有一個已核准的 grant session 時，我想透過 `bouncer_grant_execute` 直接執行已授權的命令，讓流程更直覺且明確。

**驗收條件：**
- 呼叫 `bouncer_grant_execute` 傳入 `grant_id` + `command` + `source`，命令成功執行並回傳結果
- response 包含 `status: "grant_executed"`、`result`、`request_id`、`grant_id`、`commands_remaining`

### US-2: Grant Session 安全邊界防護

> 身為系統管理者，我要確保 grant_execute 不能被濫用——過期 grant、錯誤 source、不在清單的命令都必須被擋下。

**驗收條件：** 見 Acceptance Scenarios 安全邊界案例。

---

## Interface Contract

### Tool Definition

```
Tool Name: bouncer_grant_execute
```

### Input Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `grant_id` | string | ✅ | Grant Session ID |
| `command` | string | ✅ | AWS CLI 命令（必須精確匹配 grant 中已授權的一條命令） |
| `source` | string | ✅ | 請求來源標識（必須與 grant 建立時的 source 一致） |
| `account` | string | ❌ | 目標 AWS 帳號 ID（不填則使用預設帳號，必須與 grant 的 account_id 一致） |
| `reason` | string | ❌ | 執行原因（用於 audit log，預設 "Grant execute"） |

### Output (成功)

```json
{
  "status": "grant_executed",
  "request_id": "req_abc123",
  "command": "aws s3 ls",
  "account": "190825685292",
  "account_name": "2nd",
  "result": "2026-03-01 bucket-a\n2026-03-01 bucket-b",
  "grant_id": "grant_abc123",
  "remaining": "2/5 命令, 25:13",
  "commands_remaining": 4
}
```

如果命令失敗（exit_code != 0）：
```json
{
  "status": "grant_executed",
  "request_id": "req_abc123",
  "exit_code": 1,
  "result": "An error occurred ...",
  "grant_id": "grant_abc123",
  "remaining": "2/5 命令, 25:13",
  "commands_remaining": 4
}
```

如果輸出過長（paged）：
```json
{
  "status": "grant_executed",
  "paged": true,
  "page": 1,
  "total_pages": 3,
  "output_length": 45000,
  "next_page": "req_abc123:2"
}
```

### Output (失敗)

所有失敗 response 都設 `isError: true`。

| status | 觸發條件 | message 範例 |
|--------|----------|-------------|
| `grant_not_found` | `grant_id` 不存在或 source 不匹配 | "Grant not found or source mismatch" |
| `grant_expired` | grant TTL 已過期 | "Grant session expired" |
| `grant_not_active` | grant status ≠ `active` | "Grant session is not active (status: pending_approval)" |
| `command_not_in_grant` | 命令不在授權清單中 | "Command not in granted commands list" |
| `command_already_used` | `allow_repeat=False` 且命令已執行過 | "Command already used (allow_repeat=false)" |
| `command_repeat_limit` | `allow_repeat=True` + dangerous command 已達 3 次（SEC-009） | "Dangerous command repeat limit reached (3/3)" |
| `total_executions_exceeded` | 總執行次數超過上限 | "Total execution limit reached (50/50)" |
| `compliance_violation` | 命令不通過 compliance check | "Compliance violation: {rule_id} - {rule_name}" |
| `account_mismatch` | 指定的 account 與 grant 記錄的不一致 | "Account mismatch: grant is for 190825685292, got 992382394211" |
| `account_invalid` | account ID 格式錯誤或帳號不存在 | "帳號 {account_id} 未配置" |

---

## Security Design

### 驗證鏈（依序執行，任一失敗即返回錯誤）

```
1. X-Approval-Secret header 驗證 ← 由 app.py handle_mcp_request 統一處理
2. 參數必填驗證（grant_id, command, source）
3. grant_id 存在檢查 → get_grant_session(grant_id)
4. source 匹配 → grant['source'] == request.source
5. status 檢查 → grant['status'] == 'active'
6. TTL 檢查 → time.time() < grant['expires_at']
7. account 匹配 → grant['account_id'] == resolved_account_id
8. compliance_checker → check_compliance(command)
9. 命令在授權清單 → is_command_in_grant(normalized_cmd, grant)
10. 總執行次數 → grant['total_executions'] < grant['max_total_executions']
11. 原子性使用標記 → try_use_grant_command(grant_id, normalized_cmd, allow_repeat)
    - allow_repeat=False: 命令只能用一次
    - allow_repeat=True + dangerous: 最多 3 次 (SEC-009)
12. 執行命令 → execute_command(command, assume_role)
13. Audit log → log_decision(decision_type='grant_approved')
14. 通知 → send_grant_execute_notification(...)
```

### 不可繞過的檢查

1. **compliance_checker**：即使 grant 已核准，命令仍必須通過 compliance check（攔截 runtime 新增的禁止規則）
2. **帳號驗證**：account 必須存在且 enabled
3. **原子性**：`try_use_grant_command` 使用 DynamoDB conditional update 防並發

### 與 `bouncer_execute` + `grant_id` 的差異

| 項目 | `bouncer_execute` + `grant_id` | `bouncer_grant_execute` |
|------|------|------|
| grant 不命中 | Fallthrough 到下一層（auto_approve, trust, approval） | 直接回傳明確錯誤 |
| 錯誤訊息 | 無（fallthrough = 靜默走其他路徑） | 明確 status + message |
| trust_scope | 必填 | 不需要（grant session 不存 trust_scope） |
| 用途 | 通用命令執行 | 專門用於 grant session |

### trust_scope 設計決策

**現狀：** Grant session（DynamoDB item）**不存儲** `trust_scope`。Grant 的身份驗證用 `source` + `account_id`。

**決策：** `bouncer_grant_execute` 不要求 `trust_scope` 參數。原因：
1. Grant session 已有 `source` 做身份隔離（與 `trust_scope` 作用等價）
2. Grant 是一次性/短期的審批模型，不需 trust session 的長期範圍匹配
3. 強制加 `trust_scope` 但 grant 沒存 → 要嘛改 `create_grant_request`（breaking change），要嘛驗證不了（假檢查）
4. 如果未來需要，可加為 optional field 到 grant schema

---

## Acceptance Scenarios

### Happy Path

#### S1: 成功執行 grant 內的命令
```
GIVEN 有一個 active grant session (allow_repeat=False)，包含 ["aws s3 ls", "aws sts get-caller-identity"]
WHEN Agent 呼叫 bouncer_grant_execute(grant_id=X, command="aws s3 ls", source="Private Bot")
THEN 回傳 status="grant_executed", result=<S3 輸出>, commands_remaining=1
AND DynamoDB audit log 記錄 decision_type="grant_approved"
AND Telegram 收到靜默通知
```

#### S2: allow_repeat=True 重複執行
```
GIVEN 有一個 active grant session (allow_repeat=True)
AND "aws s3 ls" 已執行 1 次
WHEN Agent 再次呼叫 bouncer_grant_execute(command="aws s3 ls")
THEN 回傳 status="grant_executed"，第二次成功
```

#### S3: Paged output（大輸出）
```
GIVEN 命令輸出 > 50KB
WHEN 執行成功
THEN 回傳 paged=true, page=1, total_pages=N, next_page="{req_id}:2"
AND Agent 可用 bouncer_get_page 取得後續頁
```

### Security Boundary Cases

#### S4: Grant 不存在
```
GIVEN grant_id="grant_nonexistent"
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 status="grant_not_found", isError=true
```

#### S5: Source 不匹配
```
GIVEN grant 的 source="Private Bot"
WHEN 以 source="Public Bot" 呼叫
THEN 回傳 status="grant_not_found", isError=true（不洩漏 grant 存在性）
```

#### S6: Grant 已過期
```
GIVEN grant 的 expires_at < now
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 status="grant_expired", isError=true
```

#### S7: Grant 狀態非 active（pending/revoked）
```
GIVEN grant 的 status="pending_approval"
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 status="grant_not_active", isError=true, message 含實際 status
```

#### S8: 命令不在授權清單
```
GIVEN grant 只授權了 ["aws s3 ls"]
WHEN 呼叫 command="aws ec2 describe-instances"
THEN 回傳 status="command_not_in_grant", isError=true
```

#### S9: 命令已使用（allow_repeat=False）
```
GIVEN allow_repeat=False，"aws s3 ls" 已執行
WHEN 再次呼叫 command="aws s3 ls"
THEN 回傳 status="command_already_used", isError=true
```

#### S10: Dangerous 命令重複達上限（SEC-009）
```
GIVEN allow_repeat=True，dangerous command "aws s3 rm ..." 已執行 3 次
WHEN 第 4 次呼叫
THEN 回傳 status="command_repeat_limit", isError=true
```

#### S11: 總執行次數超限
```
GIVEN total_executions=50, max_total_executions=50
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 status="total_executions_exceeded", isError=true
```

#### S12: Compliance violation
```
GIVEN command="aws iam create-user ..." 被 compliance_checker 攔截
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 status="compliance_violation", isError=true
AND 含 rule_id, rule_name, remediation
```

#### S13: Account 不匹配
```
GIVEN grant 的 account_id="190825685292"
WHEN 呼叫 account="992382394211"
THEN 回傳 status="account_mismatch", isError=true
```

#### S14: Account 不存在或已停用
```
GIVEN account="999999999999"（未配置）
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 status="account_invalid", isError=true
```

#### S15: 缺少必填參數
```
GIVEN 缺少 grant_id 或 command 或 source
WHEN 呼叫 bouncer_grant_execute
THEN 回傳 MCP error code -32602, "Missing required parameter: {param}"
```

#### S16: 並發安全（原子性）
```
GIVEN allow_repeat=False，兩個 Agent 同時呼叫同一 command
WHEN DynamoDB conditional update
THEN 只有一個成功，另一個回傳 status="command_already_used"
```

#### S17: 命令失敗（exit_code != 0）
```
GIVEN grant 內命令合法
WHEN 命令執行失敗（例如 aws s3 ls s3://nonexistent）
THEN 回傳 status="grant_executed"（不是 error），result 含錯誤訊息，exit_code=1
AND 命令仍標記為已使用
```

---

## Design Decision Log

### trust_scope 驗證（2026-03-11）
**決定：方案 A — 不要求 trust_scope 參數**

理由：
- Grant session DynamoDB schema 沒有存 trust_scope
- grant_id 是 32 位 hex，實際洩漏風險低
- 避免假驗證（存了但只做字串比對，沒有實質 session binding）

未來升級路徑（Sprint 31+）：
- 若需要更強的綁定，改 bouncer_request_grant 建立時存 trust_scope → bouncer_grant_execute 驗證
- 這是 breaking change，需要 major version bump
