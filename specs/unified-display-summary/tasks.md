# Tasks: Unified DynamoDB Item Display Summary

**Input**: Design documents from `specs/unified-display-summary/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create helper function and move shared utilities

- [x] T001 Create `generate_display_summary(action, **kwargs)` function in `src/utils.py` that generates display_summary string for all action types
- [x] T002 [P] Move `_format_size_human()` from `src/mcp_upload.py` to `src/utils.py` as `format_size_human()` and update import in `src/mcp_upload.py`

**Checkpoint**: Helper functions ready for use by all modules

---

## Phase 2: User Story 2 - Unified `display_summary` field on all items (Priority: P2) 🎯

**Goal**: Every DynamoDB item created by Bouncer includes a `display_summary` field

**Independent Test**: Query any DynamoDB item → item has `display_summary` field with non-empty string

**Note**: Implementing US2 first because US1 (display) depends on having the field populated.

### Implementation for User Story 2

- [x] T003 [P] [US2] Add `display_summary` to execute item creation in `src/mcp_execute.py` — `_submit_for_approval()` function
- [x] T004 [P] [US2] Add `display_summary` to execute item creation in REST path in `src/app.py` — `handle_clawdbot_request()` function
- [x] T005 [P] [US2] Add `display_summary` to single upload item creation in `src/mcp_upload.py` — `_submit_upload_for_approval()` function
- [x] T006 [P] [US2] Add `display_summary` to batch upload item creation in `src/mcp_upload.py` — `mcp_tool_upload_batch()` function
- [x] T007 [P] [US2] Add `display_summary` to add_account item creation in `src/mcp_admin.py` — `mcp_tool_add_account()` function
- [x] T008 [P] [US2] Add `display_summary` to remove_account item creation in `src/mcp_admin.py` — `mcp_tool_remove_account()` function
- [x] T009 [P] [US2] Add `display_summary` to deploy item creation in `src/deployer.py` — `mcp_tool_deploy()` function

**Checkpoint**: All new items written to DynamoDB have `display_summary` field

---

## Phase 3: User Story 1 - Consistent "Already Processed" display (Priority: P1)

**Goal**: "Already processed" callback messages show meaningful description for all request types

**Independent Test**: Click "Approve" on an already-approved `upload_batch` request → message shows `upload_batch (9 個檔案)` instead of empty backticks

### Implementation for User Story 1

- [x] T010 [US1] Update "already processed" path in `src/app.py` — `handle_telegram_webhook()` function to read `display_summary` first, then fall back to existing action-type detection logic

**Checkpoint**: Already-processed messages display correctly for all request types

---

## Phase 4: Tests

**Purpose**: Add tests covering display_summary generation and display

- [x] T011 [P] Add tests for `generate_display_summary()` helper function in `tests/test_bouncer.py` — cover all 6 action types
- [x] T012 [P] Add tests for "already processed" display path with `display_summary` field in `tests/test_bouncer.py`
- [x] T013 [P] Add test for legacy items without `display_summary` (backward compatibility) in `tests/test_bouncer.py`
- [x] T014 Add test for edge cases: missing file_count, missing project_id in `tests/test_bouncer.py`

**Checkpoint**: All new tests pass, all 694 existing tests still pass

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: Final verification

- [x] T015 Run full test suite and verify all tests pass
- [x] T016 Git commit all changes on `feat/unified-display-summary` branch

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1** (Setup): No dependencies — start immediately
- **Phase 2** (US2): Depends on Phase 1 (needs helper function)
- **Phase 3** (US1): Depends on Phase 2 (needs display_summary field to exist)
- **Phase 4** (Tests): Can partially run after Phase 1, fully after Phase 3
- **Phase 5** (Polish): Depends on all phases

### Within Phases

- T001 must complete before T003-T009 (they use the helper)
- T002 can run in parallel with T001
- T003-T009 are all independent (different files)
- T010 should come after T003-T009
- T011-T014 can mostly run in parallel

### Parallel Opportunities

- T001 and T002 are independent
- T003-T009 are all independent (different files, no dependencies on each other)
- T011-T013 are independent

## Implementation Strategy

### MVP First

1. Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5
2. Each phase validates before moving on

### Key Files Modified

| File | Tasks |
|------|-------|
| `src/utils.py` | T001, T002 |
| `src/mcp_execute.py` | T003 |
| `src/app.py` | T004, T010 |
| `src/mcp_upload.py` | T002, T005, T006 |
| `src/mcp_admin.py` | T007, T008 |
| `src/deployer.py` | T009 |
| `tests/test_bouncer.py` | T011, T012, T013, T014 |
