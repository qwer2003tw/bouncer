# Sprint 18-004: PROJECT_CONFIGS 完全遷移至 DynamoDB

> Priority: P1
> TCS: 7
> Generated: 2026-03-08
> Related: Sprint 12-001 (project-configs-ddb 初始實作)

---

## Problem Statement

`mcp_deploy_frontend.py` 目前有 DynamoDB-first + hardcoded fallback 的 project config 查詢機制：

```python
# L34-42: 硬編碼 fallback
_PROJECT_CONFIG = {
    "ztp-files": {
        "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
        "distribution_id": "E176PW0SA5JF29",
        "region": "us-east-1",
        "deploy_role_arn": "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role",
    }
}
```

問題：
1. **硬編碼的 config 永遠過時**：新增專案需要改 code + deploy，而非只寫 DDB record
2. **Fallback 掩蓋問題**：DDB lookup 失敗時 silently fallback，不會暴露 config 錯誤
3. **`deployer.py` 也有類似模式**：`PROJECTS_TABLE` 環境變數和 `bouncer-projects` table 已在用，但 frontend config 的 DDB schema 和 deployer 的 schema 分離
4. **deploy_role_arn 中有 typo**：hardcoded 值 `arn:aws:iam::190825685292` 有雙冒號（`iam::`），雖然 AWS 接受但不規範

### 現狀

- `bouncer-projects` DDB table 已存在（`template.yaml:355`）
- `_get_frontend_config()` 已能從 DDB 讀取（L71-107）
- `_get_project_config()` 實現 DDB → hardcoded fallback 邏輯（L115-143）
- `deployer.py` 透過 `db.py` 的 `deployer_projects_table` 存取同一個 table

## Scope

### 變更 1: 新增 CLI / script 寫入 DDB project config

**檔案：** `scripts/seed_project_config.py`（新建）

提供一個 seed script，將 project config 寫入 `bouncer-projects` table：

```python
"""Seed bouncer-projects DynamoDB table with frontend project configs.

Usage:
    python scripts/seed_project_config.py --project ztp-files --dry-run
    python scripts/seed_project_config.py --project ztp-files --apply
"""
```

Schema（與 `_get_frontend_config()` 的 field mapping 一致）：

```json
{
    "project_id": "ztp-files",
    "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
    "frontend_distribution_id": "E176PW0SA5JF29",
    "frontend_region": "us-east-1",
    "frontend_deploy_role_arn": "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role",
    "updated_at": "2026-03-08T17:00:00Z",
    "updated_by": "seed_project_config.py"
}
```

### 變更 2: 將 hardcoded fallback 降級為 warning-only

**檔案：** `src/mcp_deploy_frontend.py`

修改 `_get_project_config()`：
1. DDB lookup 成功 → 直接回傳（不變）
2. DDB lookup 失敗 → **log warning + 回傳 hardcoded fallback**（不變，但加 CloudWatch metric）
3. DDB lookup 成功但 record 不存在 → **log error，不 fallback**（行為改變）

```python
def _get_project_config(project_id: str) -> Optional[dict]:
    ddb_config = _get_frontend_config(project_id)
    if ddb_config is not None:
        return ddb_config

    # DDB returned None = record not found (not an error)
    # Check hardcoded fallback but log a deprecation warning
    hardcoded = _PROJECT_CONFIG.get(project_id)
    if hardcoded:
        logger.warning(
            "[deploy-frontend] DEPRECATION: Using hardcoded config for '%s'. "
            "Please add to DynamoDB bouncer-projects table.",
            project_id,
        )
        return hardcoded

    # No config anywhere
    return None
```

⚠️ **不在此 sprint 移除 `_PROJECT_CONFIG`**——需先確認 DDB 已有所有 project 的 record。

### 變更 3: DDB 欄位驗證

**檔案：** `src/mcp_deploy_frontend.py`

在 `_get_frontend_config()` 中加入欄位完整性檢查：

```python
required_fields = ['frontend_bucket', 'frontend_distribution_id']
optional_fields = ['frontend_region', 'frontend_deploy_role_arn']

if not frontend_bucket or not distribution_id:
    logger.warning(
        "[deploy-frontend] DDB record for '%s' missing required fields: %s",
        project_id,
        [f for f in required_fields if not item.get(f)],
    )
    return None
```

### 變更 4: 新增 MCP tool `bouncer_project_config`

**檔案：** `src/mcp_deploy_frontend.py` 或新檔案

新增一個唯讀 MCP tool，讓 agent 查詢目前的 project config（用於 debug）：

```python
def handle_project_config(params: dict) -> dict:
    """查詢 project 的 frontend config（DDB + fallback）"""
    project_id = params.get('project')
    config = _get_project_config(project_id)
    if config:
        return {'ok': True, 'project': project_id, 'config': config, 'source': 'ddb_or_fallback'}
    return {'ok': False, 'error': f'Project {project_id} not found'}
```

### 變更 5: 單元測試

**檔案：** `tests/test_project_configs.py`（新建）

| # | 測試 | 預期 |
|---|------|------|
| T1 | DDB 有 record → `_get_project_config` | 回傳 DDB config |
| T2 | DDB 無 record + hardcoded 有 | 回傳 hardcoded + log DEPRECATION warning |
| T3 | DDB 異常（connection error） | fallback to hardcoded + log warning |
| T4 | DDB 有 record 但缺 `frontend_bucket` | 回傳 None + log warning |
| T5 | 完全未知 project | 回傳 None |
| T6 | `seed_project_config.py --dry-run` | 不寫 DDB，顯示將要寫的內容 |
| T7 | `handle_project_config` MCP tool | 回傳 config dict |

## Out of Scope

- 移除 `_PROJECT_CONFIG` hardcoded dict（需先確認 DDB seed 完成）
- `deployer.py` 的 project config 遷移（deployer 有自己的 schema）
- 多帳號 project config（目前只有 2nd account）

## Security Considerations

- `seed_project_config.py` 需要 DDB write 權限（透過 Bouncer grant 或 deploy role）
- MCP tool `bouncer_project_config` 為唯讀，不需額外權限
- hardcoded `deploy_role_arn` 含 account ID，seed script 不應 log 完整 ARN

## Acceptance Criteria

- [ ] `scripts/seed_project_config.py` 可以 seed/update DDB project config
- [ ] `_get_project_config()` DDB-not-found 時 log DEPRECATION warning
- [ ] `_get_frontend_config()` 驗證必填欄位
- [ ] 新增 `bouncer_project_config` MCP tool（唯讀）
- [ ] 新增 ≥ 6 個單元測試
- [ ] 既有測試全部通過
- [ ] Coverage ≥ 75%
