# Sprint 12-002: Tasks — deploy_status 區分 expired vs pending

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | deployer.py |
| D2 Cross-module | 1 | mcp_tool_deploy_status 依賴 get_deploy_status 回傳值 |
| D3 Testing | 2 | not_found 測試 + isError 回歸 |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無新外部 API |
| **Total TCS** | **4** | ✅ 不需拆分 |

## Task List

```
[002-T1] [P0] [US-1] deployer.py: get_deploy_status() record not found → status='not_found'（取代 'pending'）
[002-T2] [P1] [US-1] deployer.py: mcp_tool_deploy_status() 確認 isError 邏輯包含 'not_found'（不應設 isError）
[002-T3] [P1] 測試: get_deploy_status() record not found → status == 'not_found', message 含 hint
[002-T4] [P1] 測試: get_deploy_status() record exists, status RUNNING → 原有行為不變
[002-T5] [P2] 測試: mcp_tool_deploy_status() 使用 not_found record → isError == False
```
