# Sprint 9-001: Tasks — Execution Error 記錄到 DDB

> Generated: 2026-03-02

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | utils.py, mcp_execute.py, callbacks.py (3 files) |
| D2 Cross-module | 2 | utils → mcp_execute/callbacks（import 已存在） |
| D3 Testing | 2 | 補測試（新 helper + 修改路徑） |
| D4 Infrastructure | 0 | 無 template.yaml 變更（DDB schemaless） |
| D5 External | 0 | 無新 AWS service |
| **Total TCS** | **7** | ✅ 不需拆分 |

## Task List

```
[001-T1] [P1] [US-1] 新增 _extract_exit_code() helper 到 utils.py
[001-T2] [P1] [US-1] log_decision() 新增 exit_code + error_output optional params
[001-T3] [P1] [US-2] _check_auto_approve() 路徑：失敗時傳入 exit_code + error_output，MCP response 加 exit_code
[001-T4] [P1] [US-2] _check_trust_session() 路徑：同上
[001-T5] [P1] [US-2] _check_grant_session() 路徑：同上
[001-T6] [P2] [US-1] callbacks.py approve 路徑：執行後更新 DDB item with exit_code + error_output
[001-T7] [P2] [US-3] 測試：_extract_exit_code() 單元測試（各種 output pattern）
[001-T8] [P2] [US-3] 測試：log_decision() 帶 exit_code 時 DDB item 正確寫入
[001-T9] [P2] [US-3] Integration test：模擬失敗命令全路徑
```
