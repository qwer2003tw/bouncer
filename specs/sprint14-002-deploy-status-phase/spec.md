# Sprint 14-002: deploy_status phase 永遠顯示 INITIALIZING

> GitHub Issue: #53
> Priority: P0
> TCS: 8
> Generated: 2026-03-06
> Related: Sprint 11-009（已加 progress_hint，但 phase 問題未解決）

---

## Problem Statement

`bouncer_deploy_status` 回傳的 `phase` 欄位在整個 SAM build/deploy 過程中一直顯示 `INITIALIZING`，不隨實際執行進度更新。Agent 無法判斷是「剛開始」還是「卡住了」。

Sprint 11-009 添加了 `progress_hint`（基於 elapsed time 估算的文字提示），但 `phase` 欄位本身仍是靜態的。Agent 和 SKILL.md 都已標記「不看 phase」，但 API response 仍回傳誤導性的 `phase` 值。

### 現況

```python
# deployer.py get_deploy_status() 回傳：
{
    "status": "RUNNING",
    "phase": "INITIALIZING",          # ← 永遠不變
    "progress_hint": "正在 build...",  # ← Sprint 11-009 加的估算
    ...
}
```

### 影響

1. Agent 看到 `phase: INITIALIZING` 誤判為卡住 → fallback 到 `aws stepfunctions describe-execution`
2. 每次 SFN describe → 觸發 auto-approve 通知 → 洗版
3. `progress_hint` 只是時間估算，不反映真實 SFN 狀態

## Root Cause

DynamoDB deploy record 在建立時寫入 `status=RUNNING`，但沒有 `phase` 欄位。`get_deploy_status()` 在查 SFN 時只更新 `status`（RUNNING/SUCCESS/FAILED），不寫入對應的 phase。`_get_progress_hint()` 是純本地估算，不查 SFN history。

## User Stories

**US-1: 移除 misleading phase 欄位**
As an **MCP client (Agent)**,
I want `bouncer_deploy_status` to NOT return a misleading `phase` field,
So that I only rely on `status` and `progress_hint` for deploy tracking.

**US-2: progress_hint 提供更準確的提示（可選增強）**
As an **MCP client (Agent)**,
I want `progress_hint` to optionally reflect real SFN state when available,
So that the hint is more accurate than pure time-based estimation.

## Scope

### 方案選擇

**方案 A（推薦，簡單）：移除 `phase` 欄位**
- 從 `get_deploy_status()` response 中移除 `phase` key
- Agent 只看 `status`（RUNNING/SUCCESS/FAILED）+ `progress_hint`
- 不改 SFN 查詢邏輯

**方案 B（完整，TCS 更高）：用 SFN history 填充 phase**
- 在 `get_deploy_status()` 查 SFN 時，解析 execution history events
- 根據 taskToken / activity name 判斷當前階段（BUILD/DEPLOY/COMPLETE）
- 更新 DDB `phase` 欄位
- ⚠️ TCS ≈ 12-15，需拆分，且 SFN history API 可能 throttle

**本 spec 採用方案 A + 可選的 progress_hint 增強。**

### 變更 1: 移除 phase 欄位

**檔案：** `src/deployer.py` — `get_deploy_status()` (line ~548)

在回傳 record 前，移除 `phase` key：
```python
# 移除 misleading 的 phase 欄位
record.pop('phase', None)
```

或者更精確：在 `mcp_tool_deploy_status()` 的 response 構建前移除。

### 變更 2: 增強 progress_hint（可選）

**檔案：** `src/deployer.py` — `_get_progress_hint()` (line ~534)

當 SFN execution 已被查詢時（`execution_arn` 存在），可從 SFN `describeExecution` response 的 `status` 做更準確的判斷：

```python
def _get_progress_hint(elapsed: int, sfn_status: str = None) -> str:
    if sfn_status == 'SUCCEEDED':
        return "✅ 部署完成"
    if sfn_status == 'FAILED':
        return "❌ 部署失敗"
    # 時間估算 fallback
    if elapsed < 30:
        return "正在初始化..."
    elif elapsed < 120:
        return "正在 build（SAM + Lambda layer）"
    else:
        return "正在部署 CloudFormation stack"
```

### 變更 3: 清理 deploy record 建立

**檔案：** `src/deployer.py` — `create_deploy_record()` (line ~228)

確認建立時不要寫入 `phase: INITIALIZING`。如果有其他地方寫入 phase，一併移除。

## Out of Scope

- SFN history-based phase tracking（方案 B，留給未來 sprint）
- 修改 SFN state machine 本身
- 修改 Agent SKILL.md 的「不看 phase」指導（移除 phase 後自然不用看）

## Test Plan

### Unit Tests

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_deploy_status_no_phase_field` | `get_deploy_status()` / `mcp_tool_deploy_status()` response 不含 `phase` key |
| T2 | `test_deploy_status_has_progress_hint` | RUNNING 狀態有 `progress_hint` |
| T3 | `test_progress_hint_with_sfn_status` | sfn_status 傳入時 hint 準確 |
| T4 | `test_create_deploy_record_no_phase` | 新建 record 不含 `phase` 欄位 |

### Regression

- 既有 deploy_status 測試全部通過
- Agent poll deploy_status 不再看到 phase=INITIALIZING

## Acceptance Criteria

- [ ] `bouncer_deploy_status` response 不包含 `phase` key
- [ ] `progress_hint` 在 RUNNING 狀態正常回傳
- [ ] 新建的 deploy record 不寫入 `phase`
- [ ] 所有既有測試通過
- [ ] Agent SKILL.md 的「不看 phase」註記可以移除（因為欄位已不存在）
