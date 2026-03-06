# Sprint 10-001: bug: deploy_status phase 不準確

> GitHub Issue: #47
> Priority: P0
> Generated: 2026-03-03

---

## Bug Name

deploy_status Phase Inaccuracy — `bouncer_deploy_status` 回傳的 status/phase 不準確，agent 被迫 fallback 到 `aws stepfunctions describe-execution`，每次 poll 產生自動執行通知洗版。

## Root Cause Analysis

1. **DDB record 不存在的 timing gap**：`start_deploy()` 在 `callbacks.py` approve 之後才呼叫 `create_deploy_record()` 寫入 PENDING → `start_execution()` → 更新 RUNNING。Agent 在批准瞬間立刻 poll `bouncer_deploy_status`，DDB 可能尚未寫入 → 回傳 `{'error': '部署記錄不存在'}` → agent 誤判。

2. **status 只在 poll 時同步**：`get_deploy_status()` 只有在被呼叫時（`status == 'RUNNING'` + 有 `execution_arn`）才去查 Step Functions 並同步 DDB。這本身沒問題，但 record 不存在時回傳 error 會讓 agent 放棄 poll。

3. **無 phase 欄位**：DDB record 從未有 `phase`（如 BUILD / DEPLOY / CHANGESET）。Issue 提到的「phase 永遠 INITIALIZING」是 agent 從缺失欄位推斷出的預設值。

## User Stories

**US-1: Agent 可靠的 Deploy Polling**
As an **AI agent**,
I want `bouncer_deploy_status` to return accurate `status` immediately after approval,
So that I don't need to fallback to `aws stepfunctions describe-execution` and generate spam notifications.

**US-2: Deploy Status 容錯**
As an **AI agent**,
I want `bouncer_deploy_status` to return `{status: 'pending'}` when the record doesn't exist yet,
So that I know to retry instead of treating it as an error.

## Acceptance Scenarios

### Scenario 1: Record 不存在 → 回傳 pending（而非 error）
- **Given**: Deploy 剛被批准，DDB record 尚未寫入
- **When**: Agent 呼叫 `bouncer_deploy_status` with deploy_id
- **Then**: 回傳 `{"status": "pending", "deploy_id": "<id>", "message": "Deploy record not found yet, please retry"}` 而非 `{"error": "..."}`
- **And**: `isError` = false（不觸發 agent 的 error handling）

### Scenario 2: Record 存在、SFN 完成 → status 正確同步
- **Given**: Deploy record 在 DDB status = RUNNING，SFN 已 SUCCEEDED
- **When**: Agent 呼叫 `bouncer_deploy_status`
- **Then**: 回傳 `{"status": "SUCCESS", "finished_at": ..., "duration_seconds": ...}`
- **And**: DDB 同步更新為 SUCCESS + 釋放鎖

### Scenario 3: Record 存在、SFN 仍在跑 → status = RUNNING
- **Given**: Deploy record 在 DDB status = RUNNING，SFN 仍在 RUNNING
- **When**: Agent 呼叫 `bouncer_deploy_status`
- **Then**: 回傳 `{"status": "RUNNING", "started_at": ..., "elapsed_seconds": ...}`

### Scenario 4: Deploy 失敗 → error_lines 包含在回傳
- **Given**: SFN 執行 FAILED
- **When**: Agent 呼叫 `bouncer_deploy_status`
- **Then**: 回傳 `{"status": "FAILED", "error_lines": [...], "finished_at": ...}`

## Edge Cases

1. **DDB record 存在但無 execution_arn**：status = PENDING → 回傳 PENDING（尚未啟動 SFN）
2. **SFN describe_execution 拋異常**：catch + log，回傳現有 DDB record（不覆蓋）
3. **Lock 已釋放但 status 仍為 RUNNING**：`get_deploy_status` 的 SFN 同步邏輯應自動修正
4. **deploy_id 格式錯誤**：回傳 parameter error（現有行為不變）

## Requirements

- **R1**: `get_deploy_status()` 當 record 不存在時回傳 `{"status": "pending"}` 而非 `{"error": ...}`
- **R2**: `mcp_tool_deploy_status()` 不再把 `status == pending` 視為 isError
- **R3**: RUNNING 狀態回傳加入 `elapsed_seconds`（`now - started_at`）方便 agent 判斷超時
- **R4**: SUCCESS/FAILED 狀態回傳加入 `duration_seconds`（`finished_at - started_at`）
- **R5**: 向後兼容 — 現有 agent 檢查 `status == 'RUNNING'` 的邏輯不受影響

## Interface Contract

### MCP Response 變更

Record 不存在時（新行為）：
```json
{
  "status": "pending",
  "deploy_id": "deploy-xxxxx",
  "message": "Deploy record not found yet, please retry"
}
```

RUNNING 時（新增 elapsed_seconds）：
```json
{
  "status": "RUNNING",
  "deploy_id": "deploy-xxxxx",
  "started_at": 1709452800,
  "elapsed_seconds": 45
}
```

SUCCESS 時（新增 duration_seconds）：
```json
{
  "status": "SUCCESS",
  "deploy_id": "deploy-xxxxx",
  "started_at": 1709452800,
  "finished_at": 1709452950,
  "duration_seconds": 150
}
```
