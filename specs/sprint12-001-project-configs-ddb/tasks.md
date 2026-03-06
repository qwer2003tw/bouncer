# Sprint 12-001: Tasks — PROJECT_CONFIGS 存 DDB

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 2 | mcp_deploy_frontend.py（主改動）、deployer.py（可能小調整） |
| D2 Cross-module | 2 | mcp_deploy_frontend ↔ deployer（get_project）、migration script |
| D3 Testing | 2 | frontend config 讀取測試 + 回歸前端部署流程 |
| D4 Infrastructure | 0 | 無 template.yaml 變更（DDB schemaless，不需改 table def） |
| D5 External | 2 | DDB seed migration（需 Bouncer grant session 寫入 DDB） |
| **Total TCS** | **8** | ✅ 不需拆分 |

## Task List

```
[001-T1] [P0] [US-1] mcp_deploy_frontend.py: 新增 _get_frontend_config(project_id) helper — 從 bouncer-projects table 讀取 frontend 欄位
[001-T2] [P0] [US-1] mcp_deploy_frontend.py: 將所有 _PROJECT_CONFIG 引用改為 _get_frontend_config()
[001-T3] [P0] [US-1] mcp_deploy_frontend.py: 移除 _PROJECT_CONFIG hardcoded dict
[001-T4] [P0] [US-2] mcp_deploy_frontend.py: project 無 frontend config → 回傳清楚的錯誤訊息（含 available projects）
[001-T5] [P1] DDB seed migration script：update_item 寫入 ztp-files 的 frontend_bucket/frontend_distribution_id/frontend_region/frontend_deploy_role_arn
[001-T6] [P1] 測試: _get_frontend_config() 正常讀取 → 回傳完整 config dict
[001-T7] [P1] 測試: _get_frontend_config() project 存在但無 frontend 欄位 → 回傳 None
[001-T8] [P1] 測試: _get_frontend_config() project 不存在 → 回傳 None
[001-T9] [P1] 測試: deploy_frontend 完整流程（mock DDB 有 frontend config）→ 成功
[001-T10] [P2] 測試: deploy_frontend project 無 frontend config → isError + 清楚訊息
```

## ⚠️ 部署順序

```
1. [PRE-DEPLOY] T5 migration script 先執行（seed DDB）
2. [DEPLOY] 部署含 T1-T4 的新 code
3. [POST-DEPLOY] 驗證前端部署功能
```

**如果 code 先部署、DDB 沒 seed → 前端部署會 break。**
