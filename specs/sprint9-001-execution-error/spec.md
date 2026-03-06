# Sprint 9-001: feat: Execution Error 記錄到 DDB

> GitHub Issue: #38
> Priority: P1
> Generated: 2026-03-02

---

## Feature Name

Execution Error Persistence — 將命令執行失敗的詳細資訊（exit code、error output）記錄到 DynamoDB requests 表。

## User Stories

**US-1: DevOps 排查失敗命令**
As a **DevOps operator**,
I want execution errors (exit code, stderr, error output) to be persisted in DynamoDB,
So that I can review failed command history without relying on CloudWatch logs alone.

**US-2: Agent 自動重試決策**
As an **AI agent**,
I want the MCP response to include structured error details (exit_code, error_type),
So that I can programmatically decide whether to retry, adjust parameters, or escalate.

**US-3: 錯誤趨勢分析**
As a **system administrator**,
I want a queryable record of execution errors with timestamps and categories,
So that I can identify recurring failure patterns across accounts.

## Acceptance Scenarios

### Scenario 1: 命令執行失敗 — error 寫入 DDB
- **Given**: 一個需要審批的命令 `aws s3 cp /nonexistent s3://bucket/` 已被批准
- **When**: `execute_command()` 返回以 `❌` 開頭的輸出，exit_code != 0
- **Then**: DDB requests 表中該 request_id 的 item 更新為：
  - `status` = `executed_error`
  - `exit_code` = 非零整數
  - `error_output` = 錯誤輸出前 2000 字元
  - `executed_at` = Unix timestamp
- **And**: MCP response 中 `status` 維持原值但額外包含 `exit_code` 欄位

### Scenario 2: 命令執行成功 — 不額外記錄 error 欄位
- **Given**: 命令 `aws s3 ls` 被自動批准執行
- **When**: `execute_command()` 返回正常輸出
- **Then**: DDB 中 `status` = `auto_approved`（現有行為不變）
- **And**: 不寫入 `exit_code` 或 `error_output` 欄位

### Scenario 3: Trust session 下執行失敗
- **Given**: 命令在信任 session 中自動批准
- **When**: 執行失敗（exit_code != 0）
- **Then**: `log_decision()` 記錄包含 `exit_code` 和 `error_output`
- **And**: MCP response 中 `status` = `trust_auto_approved` 但額外包含 `exit_code`

### Scenario 4: Grant session 下執行失敗
- **Given**: 命令在 grant session 中自動批准
- **When**: 執行失敗
- **Then**: DDB audit log 包含 `exit_code` 和 `error_output`
- **And**: Telegram 通知中結果顯示 ❌ 標記（已有行為，不變）

### Scenario 5: && 串接命令中某個子命令失敗
- **Given**: `aws s3 ls && aws ec2 describe-bad-thing` 被批准
- **When**: 第二個子命令失敗
- **Then**: 記錄整體 exit_code（最後一個失敗的 exit code）
- **And**: `error_output` 包含失敗子命令的輸出

## Edge Cases

1. **error_output 超長**：截斷到 2000 字元，加 `[truncated]` 後綴
2. **exit_code 為 None**：`_is_failed_output()` 判斷失敗但無明確 exit code → 記錄 `exit_code = -1`（unknown）
3. **DDB update 失敗**：catch exception + log error，不影響 MCP response 返回
4. **已有的 `log_decision()` 呼叫**：向後兼容，新增 `exit_code` 和 `error_output` 為 optional kwargs
5. **空 error output**：`execute_command()` 返回空字串但判定為失敗 → `error_output = "(no output)"`

## Requirements

- **R1**: `log_decision()` 新增 `exit_code: Optional[int]` 和 `error_output: Optional[str]` 參數
- **R2**: 所有 execute 路徑（auto_approve、trust、grant、human_approved callback）在命令結果為 error 時傳入這些參數
- **R3**: MCP response JSON 在 error 時多回傳 `exit_code` 欄位
- **R4**: `error_output` 最大 2000 字元
- **R5**: 成功執行不寫入 error 欄位（節省 DDB 儲存）

## Interface Contract

### DDB Schema 變更（requests 表）

新增 optional 欄位（不需要 migration，DDB schemaless）：

| 欄位 | 類型 | 說明 |
|------|------|------|
| `exit_code` | Number | 命令 exit code（0=成功時不記錄，非零=失敗，-1=未知） |
| `error_output` | String | 錯誤輸出，最大 2000 chars |
| `executed_at` | Number | 執行完成時間（Unix timestamp） |

### MCP Response 變更

成功時（不變）：
```json
{
  "status": "auto_approved",
  "command": "aws s3 ls",
  "result": "2026-01-01 my-bucket\n..."
}
```

失敗時（新增 exit_code）：
```json
{
  "status": "auto_approved",
  "command": "aws s3 cp /nonexistent s3://bucket/",
  "result": "❌ ...",
  "exit_code": 1
}
```
