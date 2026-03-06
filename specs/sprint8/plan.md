# Bouncer Sprint 8 — Execution Plan

> Generated: 2026-03-01

---

## Dependency Graph

```
S8-001 (LambdaLogGroup CFN import)
  └─→ S8-005 (EarlyValidation hint)    ← S8-005 depends on S8-001's template changes
       └─→ S8-002 (Deploy error lines)  ← S8-002 benefits from improved error patterns in S8-005

S8-003 (REST Unicode normalization)     ← Independent
S8-004 (deploy_history CLI --args)      ← Independent
S8-006 (upload_batch S3 verification)   ← Independent
S8-007 (trust expiry notification)      ← Independent
```

## Execution Order

### Phase 1: Sequential (Must be done in order)

| Order | Task | Reason |
|-------|------|--------|
| 1st | **S8-001** | Template changes are prerequisite for S8-005; must land first |
| 2nd | **S8-005** | Builds on S8-001's import logic; improves error messages used by S8-002 |
| 3rd | **S8-002** | Uses error patterns established in S8-005; stores them in DynamoDB |

### Phase 2: Parallel (Independent, can run simultaneously)

| Task | Why Independent |
|------|----------------|
| **S8-003** | Touches `src/app.py` (REST handler) + `src/mcp_execute.py` — no overlap with deployer |
| **S8-004** | CLI/API transport investigation — separate from all other changes |
| **S8-006** | `src/mcp_upload.py` only — no deployer or trust changes |
| **S8-007** | `src/trust.py` + `src/notifications.py` + `src/app.py` (new handler) — trust subsystem only |

> **Note on S8-007 and S8-003:** Both touch `src/app.py` but in different sections (S8-003: `handle_clawdbot_request`, S8-007: new `handle_trust_expired` + routing). Merge conflict risk is minimal but should be coordinated.

## Phase Diagram

```
Time ──────────────────────────────────────────────────────────────►

Phase 1 (Sequential):
  [S8-001] ──► [S8-005] ──► [S8-002]

Phase 2 (Parallel, can start after S8-001 or immediately):
  [S8-003] ─────────────────
  [S8-004] ─────────────────
  [S8-006] ─────────────────
  [S8-007] ─────────────────

                              ▼
                        Integration & Test
```

## Complexity Assessment

| Task | ID | Complexity | Rationale |
|------|----|------------|-----------|
| LambdaLogGroup CFN import | S8-001 | **High** | Requires SAM-transformed template handling, CFN import API, template.yaml modification, DeletionPolicy management. Touches deployment infrastructure. |
| Deploy error lines to DDB | S8-002 | **Medium** | New helper function for error extraction, DynamoDB schema addition, Telegram notification changes. Well-scoped but cross-cutting. |
| REST Unicode normalization | S8-003 | **Low** | Import existing `_normalize_command()`, add one function call. Optionally add NFKC step. Minimal code change. |
| deploy_history CLI --args | S8-004 | **Medium** | Investigation-heavy: need to reproduce, identify root cause (mcporter CLI, HTTP transport, or Lambda), then fix. Fix itself may be small. |
| EarlyValidation import hint | S8-005 | **Medium** | New regex pattern for EarlyValidation errors, improved error messages, CLI command generation. Moderate scope within `sam_deploy.py`. |
| upload_batch S3 verification | S8-006 | **Medium** | New `_verify_upload()` helper, response format changes, head_object integration. Well-defined scope in `mcp_upload.py`. |
| trust expiry notification | S8-007 | **High** | New EventBridge schedule on trust creation, new Lambda handler, DynamoDB query for pending requests, Telegram notification, schedule cancellation on revocation. Touches 4+ files. |

## Estimated Effort

| Task | Story Points | Dev Time |
|------|-------------|----------|
| S8-001 | 5 | 3-4 hours |
| S8-002 | 3 | 2-3 hours |
| S8-003 | 1 | 30-60 min |
| S8-004 | 3 | 1-2 hours (mostly investigation) |
| S8-005 | 3 | 1-2 hours |
| S8-006 | 3 | 1-2 hours |
| S8-007 | 5 | 3-4 hours |
| **Total** | **23** | **~12-17 hours** |

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| S8-001 import breaks production stack | High | Use `--dry-run-import` first; test on dev stack; DeletionPolicy=Retain |
| S8-002 DynamoDB write increases latency | Low | error_lines write is async/best-effort; cap at 5 entries |
| S8-003 breaks existing command parsing | Medium | Thorough testing with existing command patterns; NFKC is additive |
| S8-004 root cause unclear | Medium | Start with HTTP API curl test to isolate; may need mcporter upstream fix |
| S8-007 EventBridge schedule costs | Low | One-time schedules, auto-deleted after trigger; minimal cost |

## Recommended Agent Assignment (Multi-Agent Dev)

| Agent | Tasks | Rationale |
|-------|-------|-----------|
| Agent A (Deployer) | S8-001, S8-005, S8-002 | Sequential chain; same file domain (`deployer/`, `template.yaml`) |
| Agent B (Security) | S8-003 | Small, isolated SEC-003 fix |
| Agent C (CLI/API) | S8-004 | Investigation task, needs HTTP testing |
| Agent D (Upload) | S8-006 | Isolated to `mcp_upload.py` |
| Agent E (Trust) | S8-007 | Touches trust subsystem + notifications + app routing |
