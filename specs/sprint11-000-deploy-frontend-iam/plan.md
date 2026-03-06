# Sprint 11-000: Plan — deploy_frontend assume per-project deploy_role_arn

> Generated: 2026-03-04

---

## Technical Context

### 現狀分析

1. **`_PROJECT_CONFIG`** (`mcp_deploy_frontend.py`): Dict with `frontend_bucket`, `distribution_id`, `region` per project. No `deploy_role_arn`.

2. **Phase A** (`mcp_deploy_frontend.py`): `mcp_tool_deploy_frontend()` reads `_PROJECT_CONFIG`, stages files to S3 staging bucket, writes DDB pending record with project config fields (`frontend_bucket`, `distribution_id`, `staging_bucket`, etc.).

3. **Phase B** (`callbacks.py:853-1000`): `handle_deploy_frontend_callback()` reads DDB item, iterates files calling `execute_command(cmd)` for each S3 copy, then `execute_command(cf_cmd)` for CloudFront invalidation. Neither call passes `assume_role_arn`.

4. **`execute_command()`** (`commands.py:515`): Already supports `assume_role_arn` as 2nd parameter. Well-tested.

5. **Other callback handlers**: `callbacks.py:230` uses `execute_command(command, assume_role)` from item's `assume_role` field. `callbacks.py:1114` does the same for upload callbacks.

### Design

#### Phase A Changes (`mcp_deploy_frontend.py`)

1. Add optional `deploy_role_arn` to `_PROJECT_CONFIG`:
   ```python
   _PROJECT_CONFIG = {
       "ztp-files": {
           "frontend_bucket": "...",
           "distribution_id": "...",
           "region": "us-east-1",
           "deploy_role_arn": None,  # Optional: assume this role for S3+CF ops
       }
   }
   ```

2. In `mcp_tool_deploy_frontend()`, include `deploy_role_arn` in the DDB pending record:
   ```python
   item['deploy_role_arn'] = project_config.get('deploy_role_arn', '')
   ```

#### Phase B Changes (`callbacks.py`)

1. Read `deploy_role_arn` from DDB item:
   ```python
   deploy_role_arn = item.get('deploy_role_arn') or None
   ```

2. Pass to all `execute_command()` calls:
   ```python
   output = execute_command(cmd, deploy_role_arn)
   # ...
   cf_output = execute_command(cf_cmd, deploy_role_arn)
   ```

3. `execute_command()` already handles `None` → no role assumption (backward compat).

### Risk Assessment

- **Low risk**: `execute_command()` already supports role assumption; we're just wiring it up.
- **Backward compat**: `deploy_role_arn=None` → same behavior as today.
- **Testing**: Existing Phase B tests mock `execute_command` — need to verify `assume_role_arn` parameter is passed.

### Files Changed

| File | Change |
|------|--------|
| `src/mcp_deploy_frontend.py` | Add `deploy_role_arn` to `_PROJECT_CONFIG`; include in DDB record |
| `src/callbacks.py` | Read `deploy_role_arn` from item; pass to `execute_command()` calls |
| `tests/test_mcp_deploy_frontend_phase_a.py` | Test `deploy_role_arn` stored in DDB |
| `tests/test_mcp_deploy_frontend_phase_b.py` | Test role passed to `execute_command()`; test `None` fallback |
