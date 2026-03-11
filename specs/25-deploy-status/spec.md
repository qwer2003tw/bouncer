# Deploy Status: Expired vs Pending — Sprint 25

## Overview
Fix `bouncer_deploy_status` to return distinct status values: `"expired"` when TTL has passed, `"pending"` when approval is still active, and `"not_found"` when the record doesn't exist. Currently, expired and not-yet-started deploys return the same status, causing confusion.

## User Stories
1. As an agent user, I want `bouncer_deploy_status` to tell me if my deploy request expired so that I know to re-issue `bouncer_deploy` instead of waiting forever.
2. As a developer, I want to distinguish between "request expired" and "record not found" so that I can provide accurate retry guidance.
3. As a system admin, I want backward compatibility so that existing polling logic (e.g., in Clawdbot) doesn't break when this change deploys.

## Acceptance Scenarios

### Scenario 1: Deploy Request Pending (Not Expired)
**Given** a deploy request exists in DynamoDB with `status="pending_approval"` and `ttl=<future_timestamp>`
**When** `bouncer_deploy_status` is called with that `deploy_id`
**Then** the response contains `{"status": "pending", "message": "Deploy request is awaiting approval", "expires_in": 180}`
**And** the response does **not** set `isError: true`

### Scenario 2: Deploy Request Expired
**Given** a deploy request exists in DynamoDB with `status="pending_approval"` and `ttl=<past_timestamp>`
**When** `bouncer_deploy_status` is called with that `deploy_id`
**Then** the response contains `{"status": "expired", "message": "Deploy request expired without approval", "hint": "Re-issue bouncer_deploy to create a new request"}`
**And** the response does **not** set `isError: true`
**And** DynamoDB TTL will eventually auto-delete this record

### Scenario 3: Deploy Record Not Found
**Given** no DynamoDB record exists for the given `deploy_id`
**When** `bouncer_deploy_status` is called
**Then** the response contains `{"status": "not_found", "message": "Deploy record not found. The deploy may not have started yet, or the record has been cleaned up."}`
**And** the response does **not** set `isError: true`

### Scenario 4: Deploy Running or Completed
**Given** a deploy record exists with `status="RUNNING"` or `status="SUCCESS"` or `status="FAILED"`
**When** `bouncer_deploy_status` is called
**Then** the existing behavior is unchanged
**And** timing fields (`elapsed_seconds`, `duration_seconds`, `progress_hint`) are included

## Interface Contract

### Current Behavior (Bug)
- `get_deploy_status()` returns `{"status": "not_found"}` when record doesn't exist (line 680)
- `mcp_tool_deploy_status()` checks `if record.get('status') == 'pending'` but actual status is `'pending_approval'` (line 969), so the expired check never runs
- Result: expired and not-found both return `"not_found"`

### Fixed Behavior
**Case 1: Record Not Found**
```json
{
  "status": "not_found",
  "deploy_id": "deploy-abc123",
  "message": "Deploy record not found. The deploy may not have started yet, or the record has been cleaned up.",
  "hint": "If the deploy was just approved, retry in a few seconds. If the request expired, re-issue bouncer_deploy."
}
```

**Case 2: Record Exists, TTL Not Expired**
```json
{
  "status": "pending",
  "deploy_id": "deploy-abc123",
  "message": "Deploy request is awaiting approval",
  "expires_in": 180,
  "expires_at": 1710028800
}
```

**Case 3: Record Exists, TTL Expired**
```json
{
  "status": "expired",
  "deploy_id": "deploy-abc123",
  "message": "Deploy request expired without approval",
  "hint": "Re-issue bouncer_deploy to create a new request.",
  "expired_at": 1710025200
}
```

## Implementation Notes

### Files to Modify
- `src/deployer.py`:
  - **Line 969**: Change `if record.get('status') == 'pending':` to `if record.get('status') == 'pending_approval':`
  - **Line 680**: `get_deploy_status()` already returns `"not_found"` correctly, no change needed
  - **Line 973-977**: Update expired response to include `expired_at` timestamp
  - Ensure `"pending"` response includes `expires_in` (seconds until TTL) and `expires_at` (unix timestamp)

### New Files
- None

### Example Fix (deployer.py, line 966-978)
```python
# Before:
if record.get('status') == 'pending':  # BUG: status is 'pending_approval', not 'pending'
    ttl = int(record.get('ttl', 0))
    if ttl and int(time.time()) > ttl:
        record = {
            'status': 'expired',
            'deploy_id': deploy_id,
            'message': '部署請求已過期，未在時限內批准',
            'hint': 'Re-issue bouncer_deploy to create a new deploy request.',
        }

# After:
if record.get('status') == 'pending_approval':  # FIX: use correct status
    ttl = int(record.get('ttl', 0))
    current_time = int(time.time())
    if ttl and current_time > ttl:
        # Expired
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'expired',
                'deploy_id': deploy_id,
                'message': 'Deploy request expired without approval',
                'hint': 'Re-issue bouncer_deploy to create a new request.',
                'expired_at': ttl,
            }, ensure_ascii=False)}],
            'isError': False,
        })
    else:
        # Still pending
        expires_in = max(0, ttl - current_time)
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending',
                'deploy_id': deploy_id,
                'message': 'Deploy request is awaiting approval',
                'expires_in': expires_in,
                'expires_at': ttl,
            }, ensure_ascii=False)}],
            'isError': False,
        })
```

### DynamoDB Changes
- None (schema unchanged)

### Security Considerations
- **No new attack surface**: This is a read-only status query fix
- **Backward compatibility**: Existing clients may check `status != "not_found"` as success indicator; ensure they don't treat `"expired"` as success
- **Recommendation**: Document in CHANGELOG that `"expired"` is a new status value (distinct from `"not_found"`)

### Testing Strategy
- **Unit test 1**: Mock DDB record with `status="pending_approval"` and `ttl=<past>`, assert response is `"expired"`
- **Unit test 2**: Mock DDB record with `status="pending_approval"` and `ttl=<future>`, assert response is `"pending"` with correct `expires_in`
- **Unit test 3**: Mock DDB `get_item` returning `None`, assert response is `"not_found"`
- **Integration test**: Create a real deploy request, wait for TTL to expire, verify status transitions from `"pending"` → `"expired"`

## TCS Score
| D1 Files | D2 Cross-module | D3 Testing | D4 Infrastructure | D5 External | Total |
|----------|-----------------|------------|-------------------|-------------|-------|
| 1        | 0               | 2          | 0                 | 0           | 3     |

**TCS = 3 (Simple)**

**Breakdown:**
- D1 (Files): 1 — Only `deployer.py` changes, ~30 lines modified
- D2 (Cross-module): 0 — No cross-module dependencies (self-contained fix)
- D3 (Testing): 2 — Need 3 unit tests + 1 integration test to verify TTL expiry
- D4 (Infrastructure): 0 — No DDB schema or IAM changes
- D5 (External): 0 — No external service changes
