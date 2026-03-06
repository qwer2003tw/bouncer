# Sprint 12-001: PROJECT_CONFIGS 存 DDB

> GitHub Issue: #68
> Priority: P1
> TCS: 8
> Generated: 2026-03-05

---

## Problem Statement

`mcp_deploy_frontend.py` 使用 hardcoded `_PROJECT_CONFIG` dict（`mcp_deploy_frontend.py:33`）來存前端部署的專案配置（frontend_bucket、distribution_id、region、deploy_role_arn）。

問題：
1. **新增專案需改 code + 部署**：每次新增前端部署專案，需要修改 `_PROJECT_CONFIG`、commit、部署。
2. **與 deployer projects table 不一致**：後端部署的 `bouncer-projects` table（`deployer.py:46`）已經是 DDB，但前端部署的配置仍是 hardcoded。
3. **無法動態管理**：沒有 MCP tool 可以新增/修改前端部署專案。

### 現狀

```python
# mcp_deploy_frontend.py:33
_PROJECT_CONFIG = {
    "ztp-files": {
        "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
        "distribution_id": "E176PW0SA5JF29",
        "region": "us-east-1",
        "deploy_role_arn": "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role",
    }
}
```

## Root Cause

Sprint 9-003 實作前端部署時，為了快速上線，先用 hardcoded config。Issue #68 是把它遷移到 DDB 的 TODO。

## User Stories

**US-1: Dynamic frontend project config**
As a **system administrator**,
I want frontend deploy project configs stored in DynamoDB,
So that I can add/modify projects without code changes and redeployment.

**US-2: Unified project management**
As a **developer**,
I want frontend configs to use the same `bouncer-projects` DDB table as backend deploys,
So that project management is consistent.

## Scope

### Option A: 擴展 `bouncer-projects` table（推薦）

在現有 `bouncer-projects` table 的 project record 中加入 frontend deploy 欄位：
- `frontend_bucket`
- `distribution_id` (CloudFront)
- `frontend_region`
- `deploy_role_arn`（已有同名概念但用途不同 — 後端是 SFN target role，前端是 S3/CF deploy role）

**好處**：不需新 DDB table、不需改 template.yaml、一個 project 統一管理前後端配置。

### Option B: 新建 `bouncer-frontend-projects` table

獨立 table。但增加 infra 複雜度，不推薦。

### 選擇：Option A

#### 實作步驟

1. `mcp_deploy_frontend.py`：移除 `_PROJECT_CONFIG` hardcoded dict
2. 新增 `get_frontend_config(project_id)` — 從 `bouncer-projects` table 讀取
3. Seed script / migration：把現有 ztp-files frontend config 寫入 DDB
4. 確保向後相容：如果 project 沒有 frontend config 欄位 → 回適當錯誤

## Out of Scope

- 不建新的 MCP admin tool（用現有 `bouncer_add_account` 的模式，未來再加 `bouncer_project_update`）
- 不改 template.yaml（`bouncer-projects` table 已存在）
- 不改前端部署流程本身（只改配置來源）
