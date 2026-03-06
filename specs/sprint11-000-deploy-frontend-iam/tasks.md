# Sprint 11-000: Tasks — deploy_frontend assume per-project deploy_role_arn

> Generated: 2026-03-04

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 2 | mcp_deploy_frontend.py + callbacks.py |
| D2 Cross-module | 3 | Phase A→DDB→Phase B data flow; execute_command integration |
| D3 Testing | 4 | Phase A tests + Phase B tests (role, no-role, failure) |
| D4 Infrastructure | 0 | 無 template.yaml 變更（role 已存在或由使用者建立） |
| D5 External | 2 | STS AssumeRole interaction via execute_command |
| **Total TCS** | **11** | ✅ 不需拆分 |

## Task List

```
[000-T1] [P0] [US-1] _PROJECT_CONFIG 加入 deploy_role_arn 欄位（mcp_deploy_frontend.py）
[000-T2] [P0] [US-1] Phase A: mcp_tool_deploy_frontend() 將 deploy_role_arn 寫入 DDB pending record
[000-T3] [P0] [US-1] Phase B: handle_deploy_frontend_callback() 從 item 讀 deploy_role_arn
[000-T4] [P0] [US-1] Phase B: S3 copy execute_command(cmd, deploy_role_arn)
[000-T5] [P0] [US-1] Phase B: CF invalidation execute_command(cf_cmd, deploy_role_arn)
[000-T6] [P0] [US-3] deploy_role_arn=None 時 fallback 到 Lambda execution role（不傳 assume_role_arn）
[000-T7] [P1] [US-1] Phase A 測試: DDB record 包含 deploy_role_arn
[000-T8] [P1] [US-1] Phase B 測試: execute_command 收到正確的 deploy_role_arn
[000-T9] [P1] [US-3] Phase B 測試: deploy_role_arn 為 None 時不傳 assume_role_arn
[000-T10] [P1] [US-1] Phase B 測試: role assumption 失敗時 file 計入 failed list
[000-T11] [P2] 回歸測試: 確認現有 Phase A + Phase B 測試不 break
```
