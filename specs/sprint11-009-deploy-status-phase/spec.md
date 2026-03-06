# Sprint 11-009: deploy_status phase fix + SFN inconsistency

> GitHub Issues: #53 + #56
> Priority: P1
> TCS: 6
> Generated: 2026-03-04

---

## Problem Statement

### Issue #53: deploy_status phase 永遠 INITIALIZING

`get_deploy_status()` (`deployer.py:534`) 回傳 DDB record，但 record 中從未有 `phase` 欄位。Agent/使用者看到的 `phase` 是 agent 側的 fallback 預設值（`INITIALIZING`），因為 DDB 沒有這個欄位。

**Sprint 10 fix** 已解決 record-not-found 的問題（改為回傳 `{status: 'pending'}`），並新增了 `elapsed_seconds` / `duration_seconds`。但 phase 問題未修。

### Issue #56: SFN 狀態同步不一致

`get_deploy_status()` 在被 poll 時才同步 SFN 狀態到 DDB。如果：
1. SFN 已完成但沒人 poll → DDB 永遠 `RUNNING`
2. Lock 永遠不釋放（因為 lock release 只在 poll 同步時觸發）
3. 下次 deploy 被 lock 擋住

這是 "lazy sync" 設計的根本問題：依賴 client-side poll 觸發伺服器端狀態更新。

### Current State

- `get_deploy_status()` (`deployer.py:534-612`): Lazy sync — only queries SFN when polled AND `status == 'RUNNING'`.
- DDB record 無 `phase` 欄位。
- Lock release 只在 `get_deploy_status()` 的 SFN sync 邏輯中（`deployer.py:592`）。
- 另一個 release path: `cancel_deploy()` 和 `start_deploy()` failure。

## Root Cause

1. **Phase**: Step Functions state machine 有多個 states（git clone → SAM build → SAM deploy → changeset），但 deploy record 只存 `status`（PENDING/RUNNING/SUCCESS/FAILED），沒有 granular phase。
2. **SFN sync**: Lazy sync 設計 — 沒有 SFN callback/EventBridge rule 來主動更新 DDB。

## User Stories

**US-1: Meaningful Phase**
As an **AI agent polling deploy status**,
I want `bouncer_deploy_status` to return a meaningful `phase` field,
So that I can report progress to the user instead of saying "INITIALIZING".

**US-2: Reliable Terminal State Detection**
As an **AI agent**,
I want deploys to reliably reach terminal state even without polling,
So that locks are released and the next deploy is not blocked.

## Acceptance Criteria

### Phase Fix (#53)
1. `get_deploy_status()` 回傳 `phase` 欄位：至少 `PENDING` / `RUNNING` / `COMPLETED` / `FAILED`。
2. 若 SFN execution 可提供更細 phase（從 SFN history events），加入 `BUILDING` / `DEPLOYING` 等。
3. Phase 寫入 DDB record，後續 poll 不需重複查 SFN history。

### SFN Inconsistency (#56)
4. 增加 SFN completion callback 或 EventBridge rule → 主動更新 DDB + release lock。
   **或**: 增加 TTL-based safety net — `get_deploy_status()` 若 `RUNNING` 超過 30 分鐘 → 查 SFN 強制同步。
5. Lock stale detection: deploy 超過預設超時（30min）→ 自動 release lock + 標記 TIMED_OUT。

## Out of Scope

- Step Functions state machine 結構變更（保持現有 states）。
- 移除 lazy sync 改為純 push（風險太高，保持 hybrid）。
