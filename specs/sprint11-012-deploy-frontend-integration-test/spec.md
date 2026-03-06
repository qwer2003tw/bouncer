# Sprint 11-012: deploy_frontend Phase B integration test

> GitHub Issue: #59
> Priority: P1
> TCS: 4
> Generated: 2026-03-04

---

## Problem Statement

Phase B of `deploy_frontend` (`handle_deploy_frontend_callback` in `callbacks.py`) currently uses `execute_command()` to run `aws s3 cp` and `aws cloudfront create-invalidation` CLI commands. The existing tests (`test_mcp_deploy_frontend_phase_b.py`, 520 lines, 30+ test cases) mock `execute_command` at a high level.

**Missing**: Integration-level tests that verify:
1. The actual AWS CLI commands constructed are correct (bucket paths, flags, content-type, cache-control).
2. End-to-end flow from DDB item → S3 copy → CF invalidation → DDB update → Telegram notification.
3. Error propagation when individual files fail (partial deploy scenario).
4. Progress update messages sent correctly during the loop.

### Current State

- `test_mcp_deploy_frontend_phase_b.py`: 30+ unit tests mocking S3 client / `execute_command`. Good coverage of individual behaviors.
- **Gap**: No test verifies the actual CLI command strings passed to `execute_command()`.
- **Gap**: No test for the full approve flow with realistic multi-file manifest (>5 files triggering progress updates).

## User Stories

**US-1: Command Correctness**
As a **developer**,
I want tests to verify the exact AWS CLI commands generated for each file,
So that I catch regressions in S3 copy flags (content-type, cache-control, metadata-directive).

**US-2: End-to-End Approve Flow**
As a **developer**,
I want an integration test covering the full approve path,
So that DDB writes, Telegram updates, and CF invalidation are all verified together.

## Acceptance Criteria

1. Tests verify exact `execute_command()` call arguments (command string + assume_role_arn).
2. Test with 7+ files: verifies progress update at file 5 and final.
3. Test partial failure: 1 file fails → `partial_deploy` status, CF still called.
4. Test command string includes correct `--content-type`, `--cache-control`, `--metadata-directive REPLACE`.
5. Test CF invalidation command includes correct `--distribution-id` and `--paths '/*'`.
6. All tests are in `tests/test_mcp_deploy_frontend_phase_b.py` (extend existing file).

## Out of Scope

- Phase A integration tests (already well-covered).
- Actual AWS API calls (all tests remain unit/integration with mocks).
- Changes to production code (test-only sprint task).
