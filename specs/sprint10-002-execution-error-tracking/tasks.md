# Sprint 10-002: Tasks — execution error tracking 不寫 DDB

> Generated: 2026-03-03

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | commands.py, mcp_execute.py (2 files) |
| D2 Cross-module | 2 | commands → mcp_execute（import 已存在，新增 1 個 function） |
| D3 Testing | 2 | 補測試（新 helper + 3 個路徑） |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無新 AWS service |
| **Total TCS** | **7** | ✅ 不需拆分 |

## Task List

```
[002-T1] [P0] 新增 extract_exit_code() helper 到 commands.py + __all__
[002-T2] [P0] _check_auto_approve() — is_failed 改用 _is_failed_output()，exit_code 改用 extract_exit_code()
[002-T3] [P0] _check_trust_session() — 同上
[002-T4] [P0] _check_grant_session() — 同上
[002-T5] [P1] 測試：extract_exit_code() 各 pattern 單元測試
[002-T6] [P1] 測試：auto_approve 路徑 — AWS CLI 失敗 → record_execution_error() 被呼叫 + 正確 exit_code
[002-T7] [P1] 測試：trust 路徑 — 同上
[002-T8] [P1] 測試：grant 路徑 — 同上
[002-T9] [P2] Regression：成功命令不觸發 record_execution_error()
```
