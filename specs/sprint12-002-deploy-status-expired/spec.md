# Sprint 12-002: deploy_status 區分 expired vs pending

> GitHub Issue: #69
> Priority: P1
> TCS: 4
> Generated: 2026-03-05

---

## Problem Statement

`bouncer_deploy_status` 在 deploy record 找不到時回傳 `{status: 'pending', message: 'Deploy record not found yet, please retry'}`（`deployer.py:553`）。

問題：Agent 如果用一個**過期**（approval expired 或 TTL 已過）的 `deploy_id` 來查，也會看到 `status: 'pending'`，無法區分是：
1. Deploy 剛建立、record 還沒寫入（正常 race condition） → 應該 retry
2. Deploy approval 已過期、record 被 TTL 清理 → 不應該 retry，應重新發起

同時，`mcp_tool_deploy` 建立的 DDB approval record（`deployer.py:720`）有 `ttl` 欄位（300+60 秒），但 `get_deploy_status()` 不檢查這個 approval record 的 TTL，只看 deploy history table 的 record。

### 現有行為

| 情境 | 現在回傳 | 理想回傳 |
|------|---------|---------|
| deploy_id 剛建立，history record 還沒寫入 | `pending` | `pending`（不變） |
| deploy_id 的 approval 已過期（TTL） | `pending` | `expired` |
| deploy_id 的 history record 被 DDB TTL 清理 | `pending` | `expired` 或 `not_found` |

## Root Cause

`get_deploy_status()` 只查 deploy history table（`deployer_history_table`）。當 record 不存在時，統一回傳 `pending`，不區分「還沒建立」vs「已過期被清理」。

## User Stories

**US-1: Distinguish expired from pending**
As an **AI agent polling deploy status**,
I want `bouncer_deploy_status` to distinguish between "pending" (just created) and "expired" (TTL passed),
So that I can decide whether to retry or report expiry to the user.

## Scope

- `deployer.py`: `get_deploy_status()` — record not found 時，嘗試查 main request table 的 approval record
- 如果 approval record 存在且 `status` 為 `timeout` / `pending` + TTL 過期 → 回傳 `expired`
- 如果都不存在 → 回傳 `not_found`（而非 `pending`）

## Out of Scope

- 不改 deploy record 的 TTL 策略
- 不改 approval record 結構
- 不改 `mcp_tool_deploy_status` 的回傳格式（只改 `get_deploy_status()` 回傳值）
