# Data Model: Unified Display Summary

## Modified Entity: ApprovalRequest (DynamoDB Item)

### New Field

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `display_summary` | String | Optional (new items: always set; legacy items: absent) | Human-readable summary of the request, ≤100 chars |

### Existing Fields Referenced

| Field | Type | Used By |
|-------|------|---------|
| `command` | String | execute items |
| `action` | String | Type discriminator (upload, upload_batch, add_account, remove_account, deploy) |
| `file_count` | Number | upload_batch items |
| `total_size` | Number | upload_batch items |
| `key` | String | upload items (S3 key containing filename) |
| `account_id` | String | add/remove_account items |
| `account_name` | String | add/remove_account and other items |
| `project_id` | String | deploy items |
| `status` | String | All items (pending_approval, approved, denied, etc.) |

### Format Rules

- execute: first 100 characters of `command`
- upload: `"upload: {filename} ({size_human})"`
- upload_batch: `"upload_batch ({file_count} 個檔案, {total_size_human})"`
- add_account: `"add_account: {account_name} ({account_id})"`
- remove_account: `"remove_account: {account_name} ({account_id})"`
- deploy: `"deploy: {project_id}"`

### State Transitions

No state transition changes. `display_summary` is immutable after creation.
