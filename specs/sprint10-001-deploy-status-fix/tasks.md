# Sprint 10-001: Tasks — deploy_status phase 不準確

> Generated: 2026-03-03

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | deployer.py (1 file) |
| D2 Cross-module | 0 | 無跨模組 — 改動限於 deployer.py 內部 |
| D3 Testing | 2 | 補測試（新行為 + 改 assertion） |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無新 AWS service（SFN 已在用） |
| **Total TCS** | **3** | ✅ 不需拆分 |

## Task List

```
[001-T1] [P0] [US-2] get_deploy_status() — record 不存在時回傳 {status: pending} 而非 {error}
[001-T2] [P0] [US-1] mcp_tool_deploy_status() — 移除 error dict 導致 isError=True 的邏輯
[001-T3] [P1] [US-1] get_deploy_status() — RUNNING 回傳加入 elapsed_seconds
[001-T4] [P1] [US-1] get_deploy_status() — SUCCESS/FAILED 回傳加入 duration_seconds
[001-T5] [P1] 測試：record 不存在 → status=pending + isError=false
[001-T6] [P1] 測試：RUNNING → 含 elapsed_seconds
[001-T7] [P1] 測試：SUCCESS → 含 duration_seconds
[001-T8] [P2] 確認現有 deploy 測試不 break（regression check）
```
