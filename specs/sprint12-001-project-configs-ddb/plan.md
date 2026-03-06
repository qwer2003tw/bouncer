# Sprint 12-001: Plan — PROJECT_CONFIGS 存 DDB

> Generated: 2026-03-05

---

## Technical Context

### 現狀分析

1. **`_PROJECT_CONFIG`** (`mcp_deploy_frontend.py:33-40`):
   - Dict，目前只有 `ztp-files` 一個專案
   - 欄位：`frontend_bucket`, `distribution_id`, `region`, `deploy_role_arn`

2. **`bouncer-projects` table** (`deployer.py:46-135`):
   - DDB table，由 `deployer.py` 管理
   - 用 `get_project(project_id)` / `list_projects()` CRUD
   - Record 欄位：`project_id`, `name`, `git_repo`, `stack_name`, `default_branch`, `sam_template_path`, `sam_params`, `enabled`, `target_role_arn`, `secrets_id`
   - 沒有 frontend 相關欄位

3. **前端部署流程** (`mcp_deploy_frontend.py:190+`):
   - `project_config = _PROJECT_CONFIG.get(project)` → 用 hardcoded dict
   - 使用 `project_config["frontend_bucket"]`, `project_config["distribution_id"]` 等

### Design

#### Step 1: 擴展 `bouncer-projects` table schema（無 infra 變更）

DDB 是 schemaless，直接在 project record 加新欄位：

```python
# 在 ztp-files project record 中新增：
{
    "project_id": "ztp-files",
    "name": "ZTP Files",
    # ... 現有欄位 ...
    "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
    "frontend_distribution_id": "E176PW0SA5JF29",
    "frontend_region": "us-east-1",
    "frontend_deploy_role_arn": "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role",
}
```

前綴 `frontend_` 避免與後端 deploy 欄位衝突（如 `target_role_arn`）。

#### Step 2: `mcp_deploy_frontend.py` 改用 DDB

```python
# Before
project_config = _PROJECT_CONFIG.get(project)

# After
from deployer import get_project
project_record = get_project(project)
if not project_record:
    return error("Project not found")

frontend_config = {
    "frontend_bucket": project_record.get("frontend_bucket"),
    "distribution_id": project_record.get("frontend_distribution_id"),
    "region": project_record.get("frontend_region", "us-east-1"),
    "deploy_role_arn": project_record.get("frontend_deploy_role_arn"),
}

if not frontend_config["frontend_bucket"]:
    return error("Project has no frontend deploy config")
```

#### Step 3: 移除 hardcoded `_PROJECT_CONFIG`

刪除 `_PROJECT_CONFIG` dict 和相關 comment。

#### Step 4: Seed migration

一次性 script 或手動 DDB put_item，把 ztp-files 的 frontend config 寫入 `bouncer-projects` table。
這需要透過 Bouncer grant session 執行（寫 DDB 需要 IAM）。

**Seed data:**
```json
{
    "project_id": "ztp-files",
    "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
    "frontend_distribution_id": "E176PW0SA5JF29",
    "frontend_region": "us-east-1",
    "frontend_deploy_role_arn": "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role"
}
```

⚠️ 注意：需用 `update_item` 而非 `put_item`，因為 ztp-files record 已存在（有後端 deploy config）。

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| DDB record 缺少 frontend 欄位 | 確定（migration 前） | 高 | 先 seed data 再部署新 code |
| `get_project()` import 造成 circular dep | 低 | 中 | `mcp_deploy_frontend.py` 已有其他 deployer import |
| DDB read latency 影響前端部署 | 極低 | 低 | `get_project()` 已在其他路徑大量使用 |

## Migration Strategy

**順序很重要**：
1. 先 seed DDB data（透過 grant session）
2. 再部署新 code（讀 DDB 取代 hardcoded）
3. 驗證前端部署正常

如果順序反了 → 前端部署會因為找不到 frontend config 而失敗。

## Testing Strategy

- 單元測試：`get_frontend_config()` 從 DDB 取值 → 正確
- 單元測試：project 存在但無 frontend config → 適當錯誤
- 單元測試：project 不存在 → 回傳 available projects
- 回歸：完整前端部署流程測試（mock DDB）
