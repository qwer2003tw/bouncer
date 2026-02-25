# Feature Specification: Unified DynamoDB Item Display Summary

**Feature Branch**: `feat/unified-display-summary`  
**Created**: 2026-02-25  
**Status**: Draft

## Background

Bouncer stores all approval requests (execute, upload, upload_batch, add_account, remove_account, deploy) in a single DynamoDB table. Currently, only `bouncer_execute` items have a `command` field. Other request types use different fields (`files`, `key`, `project_id`, etc.).

The "already processed" Telegram message (`⚠️ 此請求已處理過` path in `app.py`) assumes `command` exists, resulting in an empty command display for upload/deploy/account requests.

## User Scenarios & Testing

### User Story 1 - Consistent "Already Processed" display (Priority: P1)

When a user clicks an already-processed Bouncer Telegram message button, they see a meaningful description of what the request was — regardless of request type.

**Why this priority**: This is the bug that triggered this spec. Upload batch requests showed empty command field.

**Independent Test**: Click "Approve" on an already-approved `upload_batch` request → message updates with `upload_batch (9 個檔案)` instead of empty backticks.

**Acceptance Scenarios**:

1. **Given** an already-approved `bouncer_execute` item, **When** callback fires, **Then** display shows the `command` text
2. **Given** an already-approved `upload_batch` item, **When** callback fires, **Then** display shows `upload_batch (N 個檔案)`
3. **Given** an already-approved `upload` item, **When** callback fires, **Then** display shows `upload: filename.js`
4. **Given** an already-approved `add_account` item, **When** callback fires, **Then** display shows `add_account: account_name`
5. **Given** an already-approved `remove_account` item, **When** callback fires, **Then** display shows `remove_account: account_name`
6. **Given** an already-approved `deploy` item, **When** callback fires, **Then** display shows `deploy: project_id`

---

### User Story 2 - Unified `display_summary` field on all items (Priority: P2)

Each item written to DynamoDB includes a `display_summary` field set at creation time, containing a human-readable description of the request. All display logic reads from this single field.

**Why this priority**: Prevents future regressions when new request types are added — they'll always have a `display_summary`.

**Independent Test**: Query any DynamoDB item (execute/upload/deploy) → item has `display_summary` field with non-empty string.

**Acceptance Scenarios**:

1. **Given** a new `bouncer_execute` request is created, **Then** item has `display_summary: "aws ec2 describe-instances --region us-east-1"`
2. **Given** a new `upload_batch` request is created, **Then** item has `display_summary: "upload_batch (9 個檔案, 245 KB)"`
3. **Given** a new `upload` request is created, **Then** item has `display_summary: "upload: index.html (12 KB)"`
4. **Given** a new `add_account` request is created, **Then** item has `display_summary: "add_account: Dev (992382394211)"`
5. **Given** a new `remove_account` request is created, **Then** item has `display_summary: "remove_account: Dev (992382394211)"`
6. **Given** a new `deploy` request is created, **Then** item has `display_summary: "deploy: bouncer"`

---

### Edge Cases

- What if `display_summary` is missing (legacy items created before this change)? → Fallback to current action-type detection logic (backward compatible)
- What if `file_count` is 0 or missing on upload_batch? → Show `upload_batch (unknown files)`
- What if `project_id` is missing on deploy? → Show `deploy: (unknown project)`

## Requirements

### Functional Requirements

- **FR-001**: All `put_item` calls MUST include a `display_summary` string field
- **FR-002**: `display_summary` MUST be set at item creation time (in `mcp_upload.py`, `mcp_admin.py`, `mcp_execute.py`, `deployer.py`)
- **FR-003**: The "already processed" callback path in `app.py` MUST read `display_summary` first, then fall back to action-type detection
- **FR-004**: `display_summary` format MUST be concise (≤100 chars) and human-readable
- **FR-005**: Existing items without `display_summary` MUST still display correctly (no crash, graceful fallback)

### Key Entities

- **ApprovalRequest item**: DynamoDB item with new optional `display_summary: str` field
- **display_summary format per type**:
  - execute: first 100 chars of `command`
  - upload: `"upload: {filename} ({size_human})"`
  - upload_batch: `"upload_batch ({file_count} 個檔案, {total_size_human})"`
  - add_account: `"add_account: {account_name} ({account_id})"`
  - remove_account: `"remove_account: {account_name} ({account_id})"`
  - deploy: `"deploy: {project_id}"`

### Files to Modify

| File | Change |
|------|--------|
| `src/mcp_execute.py` | Add `display_summary: command[:100]` to item creation |
| `src/mcp_upload.py` | Add `display_summary` to both single upload and batch upload item creation |
| `src/mcp_admin.py` | Add `display_summary` to add_account and remove_account item creation |
| `src/deployer.py` | Add `display_summary: f"deploy: {project_id}"` to deploy item creation |
| `src/app.py` | Read `display_summary` first in already-processed path, fallback to current logic |

## Success Criteria

### Measurable Outcomes

- **SC-001**: All 5 request types have `display_summary` field in DynamoDB items after creation
- **SC-002**: "Already processed" message never shows empty command field
- **SC-003**: All 686 existing tests continue to pass
- **SC-004**: New tests cover each request type's `display_summary` format
- **SC-005**: No crash when displaying legacy items without `display_summary`
