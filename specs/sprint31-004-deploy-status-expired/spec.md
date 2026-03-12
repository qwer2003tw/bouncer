# deploy_status 區分 expired vs pending

## Feature
`bouncer_status`（execute 請求）和 `bouncer_deploy_status`（deploy 請求）都正確區分 `expired`（TTL 已過）與 `pending_approval`（仍在等待審批），不再將過期請求回報為 pending。

## User Stories
- As a Bouncer MCP client, I want `bouncer_status` to return `status: 'expired'` when an execute request has timed out, so that I know to retry instead of continuing to poll.
- As a Bouncer MCP client, I want `bouncer_deploy_status` to return `status: 'expired'` when a deploy approval has timed out, so that I know to re-issue `bouncer_deploy`.
- As a Bouncer operator, I want consistent status semantics across both tools, so that I can build reliable automation.

## Acceptance Scenarios

### Scenario 1: bouncer_status — request expired (TTL passed)
Given an execute request with `status = 'pending_approval'` and `ttl = now - 100`
When `bouncer_status` is called with the `request_id`
Then the response returns `status: 'expired'`
And includes `message: '請求已過期，未在時限內批准'`
And `isError: false` (expired is informational)

### Scenario 2: bouncer_status — request still pending (TTL not passed)
Given an execute request with `status = 'pending_approval'` and `ttl = now + 300`
When `bouncer_status` is called with the `request_id`
Then the response returns `status: 'pending_approval'` (unchanged)

### Scenario 3: bouncer_deploy_status — deploy expired (already implemented — regression guard)
Given a deploy request with `status = 'pending_approval'` and `ttl = now - 100`
When `bouncer_deploy_status` is called
Then the response returns `status: 'expired'` (already works — regression test confirms)

### Scenario 4: bouncer_status — approved/denied request (TTL irrelevant)
Given an execute request with `status = 'approved'` and expired TTL
When `bouncer_status` is called
Then the response returns `status: 'approved'` (TTL check only applies to `pending_approval`)

### Scenario 5: Request has no TTL field
Given an execute request with `status = 'pending_approval'` and no `ttl` field
When `bouncer_status` is called
Then the response returns `status: 'pending_approval'` (no TTL → cannot determine expiry → treat as pending)

## Edge Cases
- `ttl` stored as Decimal (DDB) → must convert to int before comparison
- `ttl = 0` → treat as no TTL (do not mark expired)
- Record not found → existing `isError: True` response unchanged
- Deploy requests (`pending_approval` from `deployer.py`) already handled in `mcp_tool_deploy_status` — this issue is specifically about execute/upload requests in `mcp_admin.py:mcp_tool_status`

## Requirements

### Functional
- `mcp_admin.py:mcp_tool_status`: after fetching DDB item, check if `status == 'pending_approval'` and `ttl` is past → return synthetic `expired` response (same pattern as `deployer.py:mcp_tool_deploy_status`)
- Response format for expired: `{'status': 'expired', 'request_id': request_id, 'message': '請求已過期，未在時限內批准', 'hint': 'Re-issue the command to create a new request.'}`
- `isError: false` for expired status

### Non-functional
- No DDB writes (read-only status check)
- No performance impact
- Pattern must match `deployer.py` implementation for consistency

## Interface Contract
- `bouncer_status` response: new possible `status: 'expired'` value
- MCP clients should handle `expired` status and retry accordingly
