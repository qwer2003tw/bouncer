# Sprint 11-009: Tasks — deploy_status phase fix + SFN inconsistency

> Generated: 2026-03-04

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | deployer.py (1 file) |
| D2 Cross-module | 1 | SFN history API 整合（已有 SFN client） |
| D3 Testing | 3 | phase 測試 + stale lock 測試 + regression |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 1 | SFN get_execution_history API（新 API call） |
| **Total TCS** | **6** | ✅ 不需拆分 |

## Task List

```
[009-T1] [P0] [US-1] _extract_phase_from_history() helper: SFN history events → phase string
[009-T2] [P0] [US-1] get_deploy_status(): RUNNING 時呼叫 SFN history 取 phase，寫入 response + DDB
[009-T3] [P0] [US-1] get_deploy_status(): terminal state 設 phase = COMPLETED / FAILED
[009-T4] [P1] [US-2] get_deploy_status(): RUNNING + elapsed > 30min → 強制 SFN sync
[009-T5] [P1] [US-2] start_deploy(): lock age > 30min → 驗證 SFN 狀態，stale 則 release
[009-T6] [P1] 測試: phase = INITIALIZING（SFN 剛啟動無 history）
[009-T7] [P1] 測試: phase = BUILDING（SFN 在 SAMBuild state）
[009-T8] [P1] 測試: phase = COMPLETED（SFN SUCCEEDED）
[009-T9] [P1] 測試: stale lock（>30min）→ SFN terminal → lock released
[009-T10] [P2] 測試: SFN history API 失敗 → phase = UNKNOWN, 不影響 status 回傳
```
