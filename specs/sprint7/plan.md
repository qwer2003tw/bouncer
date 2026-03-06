# Bouncer Sprint 7 — Implementation Plan

> Generated: 2026-03-01

---

## Technical Context

### Codebase Summary
- **30 source files** in `src/`, totaling **13,848 LOC**
- Largest files: `mcp_execute.py` (1025), `mcp_upload.py` (971), `callbacks.py` (945)
- DynamoDB tables: main requests table, accounts table, deployer tables (projects/history/locks), command-history, shadow-approvals
- 2 existing GSIs: `source-created-index`, `status-created-index`
- Template: `template.yaml` (SAM/CloudFormation)
- Deployer: `deployer/scripts/sam_deploy.py`

### Key Architectural Patterns
- **Lazy DynamoDB init:** `db.py` uses `_LazyTable` proxy for main/accounts tables; other modules have their own lazy init (the problem #008 addresses)
- **Pipeline architecture:** `mcp_execute.py` has a multi-stage pipeline (parse → compliance → blocked → grant → auto-approve → rate-limit → trust → approval)
- **Command execution:** `commands.py` uses `awscli.clidriver` in-process (no subprocess) for AWS CLI commands
- **Paging:** `paging.py` splits long output into DynamoDB-stored pages, retrieved via `bouncer_get_page`

---

## Dependency Graph

```
                    ┌─────────────────────────┐
                    │  #009 Memory 512MB       │ ← Independent (1-line)
                    └─────────────────────────┘
                    ┌─────────────────────────┐
                    │  #007 Dedup functions     │ ← Independent
                    └─────────────────────────┘
                    ┌─────────────────────────┐
                    │  #008 db.py centralize    │ ← Independent (but #003 benefits from same db pattern)
                    └─────────────────────────┘
                    ┌─────────────────────────┐
                    │  #006 Trust source bind   │ ← Independent
                    └─────────────────────────┘
                    ┌─────────────────────────┐
                    │  #004 CW Logs truncation  │ ← Independent
                    └─────────────────────────┘
                    ┌─────────────────────────┐
                    │  #005 sam_deploy import   │ ← Independent (deployer subdir)
                    └─────────────────────────┘

  ┌──────────────────┐    ┌──────────────────────┐
  │ #001 && fix       │    │ #003 GSI Query        │
  │ (commands.py)     │    │ (mcp_history.py +     │
  │                   │    │  telegram_commands.py +│
  └──────────────────┘    │  template.yaml)       │
                          └──────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │ #002 EventBridge Scheduler   │
                    │ (notifications.py + app.py + │
                    │  template.yaml)              │
                    └─────────────────────────────┘
```

### Dependency Rules
1. **#002 and #003** both modify `template.yaml` → should be done SEQUENTIALLY or carefully merged
2. **#008** centralizes DynamoDB init → ideally done BEFORE #003 (cleaner imports), but not strictly required
3. **#001, #004, #005, #006, #007, #009** are fully independent
4. **#005** only touches `deployer/scripts/sam_deploy.py` — completely isolated

---

## Implementation Phases

### Phase 1: Quick Wins (Parallel)
All independent, low-risk changes that can be done simultaneously.

| Task | Complexity | Agent |
|------|-----------|-------|
| #009 Memory 512MB | Low | Agent A |
| #007 Dedup functions | Low | Agent A (same agent, quick) |
| #001 && chain fix | Medium | Agent B |
| #006 Trust source bind | Medium | Agent C |
| #004 CW Logs fix | Medium | Agent D |
| #005 sam_deploy import | Medium | Agent E |

### Phase 2: Refactoring (After Phase 1)
Centralization work that benefits from Phase 1 being stable.

| Task | Complexity | Agent |
|------|-----------|-------|
| #008 db.py centralize | Medium | Agent F |

### Phase 3: Infrastructure Changes (Sequential)
Changes that touch `template.yaml` and require careful merging.

| Task | Complexity | Agent |
|------|-----------|-------|
| #003 GSI Query | Medium | Agent G |
| #002 EventBridge Scheduler | High | Agent G (same agent, sequential — both touch template.yaml) |

---

## Complexity Estimates

| Task ID | Title | Complexity | Rationale |
|---------|-------|-----------|-----------|
| S7-001 | && chain fix | **Medium** | Custom parser modification; need careful quote handling; multiple test scenarios |
| S7-002 | EventBridge Scheduler | **High** | New AWS service integration; IAM policy changes; template.yaml modification; new event handler |
| S7-003 | GSI Query | **Medium** | Multiple scan locations to convert; may need GSI projection changes in template.yaml |
| S7-004 | CW Logs truncation | **Medium** | Need to understand full paging flow; potential DynamoDB size limits |
| S7-005 | sam_deploy import | **Medium** | CFN import API is complex; error parsing from sam deploy output |
| S7-006 | Trust source bind | **Medium** | Security-sensitive; backward compatibility needed; multiple functions to update |
| S7-007 | Dedup functions | **Low** | Pure refactoring; move + import change |
| S7-008 | db.py centralize | **Medium** | Touches many files; test injection patterns need preservation |
| S7-009 | Memory 512MB | **Low** | One-line change in template.yaml |

---

## Risk Assessment

| Task | Risk | Mitigation |
|------|------|-----------|
| #001 | Regex/parser bugs with edge cases | Comprehensive test suite for quote handling |
| #002 | EventBridge Scheduler IAM permissions | Test in dev account first; rollback plan |
| #003 | GSI projection missing fields | Verify projection covers all needed attributes |
| #005 | Automated CFN import may corrupt resources | Only attempt for specific "already exists" errors; add dry-run mode |
| #006 | Breaking existing trust sessions | Backward compatible: skip check if `bound_source` is empty |
| #008 | Breaking test mocking patterns | Preserve `_LazyTable` proxy pattern; update test fixtures |

---

## Multi-Agent Assignment

### Recommended: 5 parallel agents

- **Agent A:** #007 + #009 (quick wins, same files don't overlap)
- **Agent B:** #001 (commands.py focused)
- **Agent C:** #006 (trust.py focused)
- **Agent D:** #004 (paging.py + constants.py focused)
- **Agent E:** #005 (deployer/ focused — completely isolated subdirectory)

### After Phase 1 completes:
- **Agent F:** #008 (db.py refactor — benefits from stable codebase)

### After Phase 2:
- **Agent G:** #003 then #002 (both touch template.yaml; sequential)

---
