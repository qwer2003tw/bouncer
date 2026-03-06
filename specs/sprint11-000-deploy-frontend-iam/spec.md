# Sprint 11-000: deploy_frontend assume per-project deploy_role_arn

> GitHub Issue: #67
> Priority: P0
> TCS: 11
> Generated: 2026-03-04

---

## Problem Statement

`handle_deploy_frontend_callback()` (Phase B, `callbacks.py:929`) calls `execute_command(cmd)` **without** `assume_role_arn`. This means all S3 copy and CloudFront invalidation operations execute under the Lambda execution role, not under the project-specific deploy role.

For cross-account or least-privilege deployments, each project should define its own `deploy_role_arn` in `_PROJECT_CONFIG` (or DDB), and Phase B should assume that role when performing S3 copy + CF invalidation.

### Current State

1. **`_PROJECT_CONFIG`** (`mcp_deploy_frontend.py`): Only has `frontend_bucket`, `distribution_id`, `region`. No `deploy_role_arn`.
2. **Phase B** (`callbacks.py:929,967`): `execute_command(cmd)` — no `assume_role_arn` parameter.
3. **Other handlers** (e.g. `callbacks.py:230,1114`): Already use `execute_command(command, assume_role)` pattern correctly.

### Impact

- **Security**: Lambda role has broader permissions than needed; should use scoped per-project role.
- **Multi-account**: Cannot deploy frontends to buckets in different AWS accounts.
- **Consistency**: Other command execution paths (upload, deploy) already support `assume_role_arn`.

## Root Cause

Phase B (deploy_frontend callback) was implemented in Sprint 9 as MVP. The `execute_command()` calls were hardcoded without role assumption, unlike the general command execution path that passes `assume_role_arn` from the DDB item.

## User Stories

**US-1: Per-Project Deploy Role**
As a **platform operator**,
I want each project's frontend deployment to assume a project-specific `deploy_role_arn`,
So that S3 copy + CF invalidation run with least-privilege, scoped IAM permissions.

**US-2: Cross-Account Frontend Deploy**
As a **platform operator**,
I want to configure `deploy_role_arn` per project so frontend deployments can target buckets in different AWS accounts.

**US-3: Backward Compatibility**
As an **existing user**,
I want projects without `deploy_role_arn` to continue working (using Lambda execution role),
So that existing deployments are not broken.

## Acceptance Criteria

1. `_PROJECT_CONFIG` supports optional `deploy_role_arn` field.
2. Phase A (`mcp_deploy_frontend.py`) stores `deploy_role_arn` in the DDB pending record.
3. Phase B (`callbacks.py`) reads `deploy_role_arn` from DDB item and passes it to `execute_command()`.
4. If `deploy_role_arn` is `None`/absent, falls back to Lambda execution role (no regression).
5. Both S3 copy commands and CloudFront invalidation command use the project's `deploy_role_arn`.
6. Tests cover: with role, without role (backward compat), role assumption failure handling.

## Out of Scope

- Moving `_PROJECT_CONFIG` to DDB (future enhancement).
- IAM role creation / CloudFormation changes for the deploy role itself.
- Changes to SAM deploy (`deployer.py`) — that already has its own role handling.
