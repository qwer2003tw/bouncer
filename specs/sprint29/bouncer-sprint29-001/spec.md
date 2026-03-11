# Trust Session in mcp_deploy_frontend

## Summary
Enable trust session bypass for `mcp_deploy_frontend` tool to allow auto-approval of frontend deploys when trust conditions are met.

## Background / Motivation
Currently `mcp_deploy_frontend` at line 252 has a stubbed `_check_deploy_trust()` that always returns False, and `_execute_trusted_deploy()` at line 437 is unimplemented. The trust session infrastructure (`should_trust_approve()` in `src/trust.py:365`) and reference implementation in `mcp_execute` (`_check_trust_session()` at line 715) are already complete. This task connects the frontend deploy flow to the existing trust system, reducing manual approval friction for trusted deploy operations while maintaining strict security controls.

**Security Rationale:** Trust session bypass must be restricted to frontend-only projects with verified trust_scope and project constraints. Unlike arbitrary command execution, frontend deploys have a narrower attack surface (limited to S3/CloudFront operations via specific IAM roles), making them suitable for trust automation with proper guardrails.

## User Stories
- **US1:** As a Claude Code user, when I deploy a frontend project within an active trust session, then the deploy proceeds automatically without manual approval.
- **US2:** As a security operator, when a trust bypass occurs for frontend deploy, then I can audit the event in CloudWatch logs with full context (trust_scope, project, user).
- **US3:** As a developer, when I attempt a trusted deploy for a non-frontend project, then the request is rejected and falls back to manual approval flow.

## Acceptance Scenarios

### Scenario 1: Successful Trust Bypass
- **Given:** Active trust session with `trust_scope="bouncer-prod"` and `command_count < 20`
- **When:** User invokes `bouncer_deploy_frontend` for project `openclaw-web`
- **Then:** `_check_deploy_trust()` returns `True`
- **And:** `_execute_trusted_deploy()` executes deploy without manual approval
- **And:** Audit log records: `trust_session_bypass=true`, `trust_scope`, `project`, `account_id`

### Scenario 2: Trust Denied - Invalid Project Type
- **Given:** Active trust session
- **When:** User invokes `bouncer_deploy_frontend` for project `lambda-backend` (backend project)
- **Then:** `_check_deploy_trust()` returns `False`
- **And:** Deploy falls back to manual approval flow
- **And:** Warning log: "Trust denied: project type mismatch"

### Scenario 3: Trust Denied - Expired Session
- **Given:** Trust session expired (command_count >= 20 or session timeout)
- **When:** User invokes `bouncer_deploy_frontend` for any project
- **Then:** `_check_deploy_trust()` returns `False`
- **And:** Deploy requires manual approval

### Scenario 4: Trust Denied - Scope Mismatch
- **Given:** Active trust session with `trust_scope="bouncer-dev"`
- **When:** User invokes `bouncer_deploy_frontend` targeting production environment
- **Then:** `_check_deploy_trust()` returns `False`
- **And:** Warning log: "Trust denied: scope mismatch"

## Technical Design

### Files to Change
| File | Change |
|------|--------|
| `src/mcp_deploy_frontend.py:252` | Implement `_check_deploy_trust()` - call `should_trust_approve()` with frontend-specific validation |
| `src/mcp_deploy_frontend.py:437` | Implement `_execute_trusted_deploy()` - execute deploy flow with trust bypass flag |
| `src/trust.py` | (No change - reuse existing `should_trust_approve()`) |

### Key Implementation Notes

#### 1. `_check_deploy_trust()` Implementation
```python
def _check_deploy_trust(req_id: str, arguments: dict) -> bool:
    """Check if deploy can be trusted based on trust session state.

    Returns True if:
    - Trust session is active (should_trust_approve returns True)
    - Project is frontend-type (verified via deployer_projects_table)
    - trust_scope matches target environment
    """
    from trust import should_trust_approve

    # Basic trust check
    if not should_trust_approve(req_id, arguments):
        logger.info(f"[deploy_frontend] trust denied: session inactive, req={req_id}")
        return False

    # Validate project type (frontend only)
    project = arguments.get("project")
    if not project:
        logger.warning(f"[deploy_frontend] trust denied: no project specified")
        return False

    # Query deployer_projects_table to verify project type
    from db import deployer_projects_table
    try:
        project_item = deployer_projects_table.get_item(Key={"project": project}).get("Item")
        if not project_item:
            logger.warning(f"[deploy_frontend] trust denied: project not found, project={project}")
            return False

        project_type = project_item.get("type", "")
        if project_type != "frontend":
            logger.warning(f"[deploy_frontend] trust denied: non-frontend project, type={project_type}")
            return False
    except Exception as e:
        logger.error(f"[deploy_frontend] trust check failed: {e}")
        return False

    # Validate trust_scope matches project environment (optional, based on project metadata)
    # trust_scope is already validated in should_trust_approve()

    logger.info(f"[deploy_frontend] trust approved, req={req_id}, project={project}")
    return True
```

#### 2. `_execute_trusted_deploy()` Implementation
```python
def _execute_trusted_deploy(req_id: str, arguments: dict) -> dict:
    """Execute frontend deploy with trust bypass (no manual approval).

    Mirrors normal deploy flow but:
    - Sets trust_bypass=True in metadata
    - Logs audit trail with trust session context
    - Directly invokes Step Functions (no approval wait)
    """
    project = arguments.get("project")
    branch = arguments.get("branch", "main")
    account_id = arguments.get("account_id", DEFAULT_ACCOUNT_ID)

    # Audit log
    logger.info(
        f"[deploy_frontend] TRUST BYPASS DEPLOY: req={req_id}, project={project}, "
        f"branch={branch}, account={account_id}, trust_scope={arguments.get('trust_scope')}"
    )

    # Execute deploy (same as approved flow)
    try:
        # Start deploy workflow via Step Functions
        deploy_id = _start_deploy_workflow(
            project=project,
            branch=branch,
            account_id=account_id,
            metadata={
                "req_id": req_id,
                "trust_bypass": True,
                "trust_scope": arguments.get("trust_scope"),
            }
        )

        return {
            "status": "started",
            "deploy_id": deploy_id,
            "project": project,
            "branch": branch,
            "trust_bypass": True,
            "message": f"Deploy started with trust session bypass (deploy_id={deploy_id})"
        }
    except Exception as e:
        logger.error(f"[deploy_frontend] trusted deploy failed: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": "Trust deploy failed, please retry with manual approval"
        }
```

#### 3. Integration Points
- `src/mcp_deploy_frontend.py:main_flow()` already has the trust check callsite (line 252) - just needs implementation
- Reference implementation in `src/mcp_execute.py:715` provides the pattern
- Trust state is managed by `src/trust.py:should_trust_approve()` - no changes needed

### Security Considerations

#### 1. Project Type Whitelist
- **Control:** Only `project.type == "frontend"` allowed for trust bypass
- **Validation:** Query `deployer_projects_table` to verify project metadata
- **Rationale:** Frontend projects have limited blast radius (S3/CloudFront only)

#### 2. Trust Scope Verification
- **Control:** `should_trust_approve()` already validates trust_scope against session state
- **Enforcement:** Trust session must be explicitly created with correct scope
- **Audit:** All trust_scope values logged in approval events

#### 3. Command Count Limit
- **Control:** Trust session expires after 20 commands (enforced in `trust.py`)
- **Rate Limiting:** Prevents runaway automation
- **Manual Override:** Users can restart trust session with `/trust start`

#### 4. Audit Trail
- **Requirement:** All trust bypass events logged with:
  - `req_id`, `project`, `branch`, `account_id`
  - `trust_scope`, `trust_bypass=true` flag
  - Timestamp and outcome (success/failure)
- **Log Level:** INFO for approval, WARNING for denials, ERROR for failures
- **Retention:** CloudWatch logs retained per workspace policy (typically 30-90 days)

#### 5. IAM Role Constraints
- **Enforcement:** Frontend deploy roles (`*-frontend-deploy-role`) have least-privilege permissions
- **Boundaries:** Cannot modify Lambda, DynamoDB, or other sensitive resources
- **Trust Policy:** Deploy roles trust only the bouncer Lambda execution role

#### 6. No Silent Failures
- **Design:** If trust check fails (DB error, invalid state), fall back to manual approval
- **Error Handling:** Never auto-approve on exception - always fail-safe to manual flow

## Task Complexity Score (TCS)
| D1 Files | D2 Cross-module | D3 Testing | D4 Infra | D5 External | Total |
|----------|-----------------|------------|----------|-------------|-------|
| 2        | 2               | 2          | 0        | 0           | 6     |

**TCS = 6 (Simple)**

- **D1 Files (2):** Modify 1 file (`mcp_deploy_frontend.py`), implement 2 functions
- **D2 Cross-module (2):** Import from `trust.py`, query `deployer_projects_table` via `db.py`
- **D3 Testing (2):** Unit tests for trust checks, integration test for trust bypass flow
- **D4 Infra (0):** No template.yaml or IAM changes (trust infrastructure already exists)
- **D5 External (0):** No new external dependencies

## Test Requirements

### Unit Tests
- **Test:** `_check_deploy_trust()` returns `True` for valid frontend project with active trust session
- **Test:** `_check_deploy_trust()` returns `False` for non-frontend project
- **Test:** `_check_deploy_trust()` returns `False` for expired trust session
- **Test:** `_check_deploy_trust()` returns `False` when project not found in projects table
- **Test:** `_execute_trusted_deploy()` logs audit trail with `trust_bypass=true`

### Integration Tests
- **Test:** End-to-end deploy with trust session (mock Step Functions)
- **Test:** Deploy falls back to manual approval when trust denied
- **Test:** Trust session command count increments after deploy

### Mock Paths
- `trust.should_trust_approve` → mock to return True/False
- `deployer_projects_table.get_item` → mock project metadata
- Step Functions client → mock `start_execution()` response

### Edge Cases
1. **Project exists but has no `type` field** → Deny trust (missing metadata = unsafe)
2. **DynamoDB throttling on projects table query** → Deny trust (fail-safe)
3. **Trust session expires mid-deploy** → Deploy proceeds (trust checked at invocation time)
4. **User manually approves after trust denial** → Normal flow continues (trust is additive, not exclusive)
5. **Multiple concurrent deploys in same trust session** → Each consumes 1 command count (parallelism allowed)
