# DynamoDB Scan → GSI Query in mcp_history

## Summary
Replace full-table scan in `mcp_history.py` with efficient GSI query to reduce DynamoDB read costs and improve latency for command history queries.

## Background / Motivation
Currently `_query_command_history_table()` at `src/mcp_history.py:258-283` performs a full-table scan with `FilterExpression` to retrieve command history. This is inefficient and costly as the table grows:
- **Current approach:** Scan entire table, filter client-side → O(n) read capacity
- **Target approach:** Query GSI with time-based sort key → O(log n) read capacity
- **Cost impact:** Scan charges for all scanned items; query charges only for returned items

The comment at line 260 acknowledges this: *"CommandHistoryTable has no GSI (only pk/sk composite key). A full-table Scan with FilterExpression is the only option here."*

**Infrastructure Design:** Add a GSI to `CommandHistoryTable` in `template.yaml` that supports efficient time-range queries with optional filters for `source` and `account_id`.

## User Stories
- **US1:** As a Claude Code user, when I query command history with `bouncer_history`, then the response is <100ms (vs current ~500ms for full scan).
- **US2:** As a cost-conscious operator, when command history is queried, then DynamoDB charges only for matching items (not all scanned items).
- **US3:** As a developer, when I add new query filters to history tool, then I can leverage the GSI without requiring another table scan.

## Acceptance Scenarios

### Scenario 1: Time-Range Query (Common Case)
- **Given:** CommandHistoryTable has 10,000 items spanning 30 days
- **When:** User queries `bouncer_history` for last 24 hours (default)
- **Then:** Query uses GSI `type-created_at-index` with `KeyConditionExpression`
- **And:** DynamoDB reads only ~500 items (24h worth)
- **And:** Query completes in <100ms

### Scenario 2: Filtered Query (source + time)
- **Given:** User queries history with `source=claude-code`
- **When:** Query executes
- **Then:** GSI query returns all items in time range
- **And:** `FilterExpression` for `source=claude-code` applied post-query (still more efficient than full scan)
- **And:** Cost is proportional to time range, not total table size

### Scenario 3: Backward Compatibility
- **Given:** Existing `mcp_history` tool API
- **When:** GSI-based query is implemented
- **Then:** Tool response format unchanged (same fields, same structure)
- **And:** Existing tool consumers (Claude Code, clawdbot) see no breaking changes

### Scenario 4: Empty Result Set
- **Given:** No commands in specified time range
- **When:** Query executes
- **Then:** GSI query returns 0 items (minimal cost)
- **And:** Response: `{"commands": [], "total_scanned": 0}`

## Technical Design

### Files to Change
| File | Change |
|------|--------|
| `template.yaml:151-177` | Add GSI `type-created_at-index` to CommandHistoryTable |
| `src/mcp_history.py:258-289` | Replace `scan()` with `query()` on GSI |
| `src/sequence_analyzer.py:552-562` | Add `created_at` (unix timestamp) and `type` field to written items |

### Key Implementation Notes

#### 1. GSI Design in template.yaml
Add to `CommandHistoryTable` (after line 162):
```yaml
AttributeDefinitions:
  - AttributeName: pk
    AttributeType: S
  - AttributeName: sk
    AttributeType: S
  - AttributeName: type
    AttributeType: S
  - AttributeName: created_at
    AttributeType: N  # Unix timestamp
KeySchema:
  - AttributeName: pk
    KeyType: HASH
  - AttributeName: sk
    KeyType: RANGE
GlobalSecondaryIndexes:
  - IndexName: type-created_at-index
    KeySchema:
      - AttributeName: type
        KeyType: HASH
      - AttributeName: created_at
        KeyType: RANGE
    Projection:
      ProjectionType: ALL
    BillingMode: PAY_PER_REQUEST
```

**Design Rationale:**
- **`type` as HASH key:** Constant value `"CMD"` for all command records (allows single-partition query)
- **`created_at` as RANGE key:** Unix timestamp enables efficient time-range queries with `KeyConditionExpression`
- **ProjectionType: ALL:** Include all attributes in GSI (no need for base table lookup)
- **PAY_PER_REQUEST:** Match base table billing mode

**Alternative Rejected:** `source` or `account_id` as HASH key would require separate queries per source/account (less efficient for "all commands" queries).

#### 2. Update `sequence_analyzer.py` to Write GSI Attributes
Modify `record_command()` at line 552:
```python
# Build item for DynamoDB
item = {
    'pk': f'source#{source_hash}',
    'sk': f'ts#{timestamp}',
    'type': 'CMD',  # NEW: Constant value for GSI HASH key
    'source': source,
    'command': command,
    'service': service,
    'action': action,
    'resource_ids': resource_ids,
    'account_id': account_id,
    'created_at': int(time.time()),  # NEW: Unix timestamp for GSI RANGE key
    'ttl': ttl,
}
```

**Backward Compatibility:** Existing items without `type`/`created_at` will not appear in GSI queries. Migration strategy:
- **Option A (Recommended):** Deploy code + template, new writes include GSI fields, old data expires via TTL (30 days)
- **Option B:** Backfill existing items with GSI fields (requires one-time scan + update script)

For this spec, we choose **Option A** (no backfill) since command history is ephemeral (30-day TTL).

#### 3. Update `mcp_history.py` to Query GSI
Replace `_query_command_history_table()` at line 258:
```python
def _query_command_history_table(
    limit: int,
    source: str | None,
    account_id: str | None,
    since_ts: int,
    exclusive_start_key: dict | None,
) -> tuple[list[dict], int]:
    """Query command-history table via GSI. Returns (items, scanned_count)."""
    dynamodb = _get_dynamodb_resource()
    cmd_table = _get_command_history_table(dynamodb)
    if cmd_table is None:
        return [], 0

    # Build KeyConditionExpression for GSI
    from boto3.dynamodb.conditions import Key
    key_condition = Key("type").eq("CMD") & Key("created_at").gte(since_ts)

    # Build FilterExpression for optional filters
    filter_expr = None
    if source:
        filter_expr = Attr("source").eq(source)
    if account_id:
        if filter_expr:
            filter_expr = filter_expr & Attr("account_id").eq(account_id)
        else:
            filter_expr = Attr("account_id").eq(account_id)

    # Query GSI
    kwargs: dict = {
        "IndexName": "type-created_at-index",
        "KeyConditionExpression": key_condition,
        "Limit": limit,
        "ScanIndexForward": False,  # Sort descending (newest first)
    }
    if filter_expr:
        kwargs["FilterExpression"] = filter_expr
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    try:
        resp = cmd_table.query(**kwargs)
        items = resp.get("Items", [])
        scanned = resp.get("ScannedCount", 0)
        return items, scanned
    except Exception as e:
        logger.error(f"[history] query command-history GSI error: {e}")
        return [], 0
```

**Key Changes:**
- `scan()` → `query()` with `IndexName`
- `FilterExpression` → `KeyConditionExpression` for time range (much more efficient)
- `ScanIndexForward=False` → Return newest commands first (typical UX expectation)

#### 4. CloudFormation Deployment Notes
- **GSI creation is online:** No table downtime, but backfilling takes time (~5 mins for small tables)
- **Reads during GSI creation:** Base table queries still work (no impact on existing deployments)
- **Cost during backfill:** DynamoDB charges for reads to populate GSI (one-time cost, proportional to table size)

### Security Considerations
- **No new permissions required:** Lambda already has `dynamodb:Query` on CommandHistoryTable (covered by `DynamoDBCrudPolicy`)
- **Data exposure:** GSI contains same data as base table (no new PII exposure)
- **Query cost limits:** Existing `limit` parameter prevents runaway queries (max 50 items per call)

## Task Complexity Score (TCS)
| D1 Files | D2 Cross-module | D3 Testing | D4 Infra | D5 External | Total |
|----------|-----------------|------------|----------|-------------|-------|
| 2        | 1               | 2          | 3        | 0           | 8     |

**TCS = 8 (Medium)**

- **D1 Files (2):** Modify 2 files (`mcp_history.py`, `sequence_analyzer.py`)
- **D2 Cross-module (1):** Changes in `sequence_analyzer.py` affect writes, changes in `mcp_history.py` affect reads
- **D3 Testing (2):** Test GSI query logic, verify backward compatibility, test empty results
- **D4 Infra (3):** Modify `template.yaml` to add GSI (requires CloudFormation stack update)
- **D5 External (0):** No new external dependencies

## Test Requirements

### Unit Tests
- **Test:** `_query_command_history_table()` queries GSI with correct `KeyConditionExpression`
- **Test:** Time-range filter uses `created_at >= since_ts`
- **Test:** Optional `source` filter applied as `FilterExpression`
- **Test:** Optional `account_id` filter applied as `FilterExpression`
- **Test:** `ScanIndexForward=False` returns newest items first
- **Test:** Empty result set returns `([], 0)`

### Integration Tests (Post-Deploy)
1. **Write test command:** Invoke `record_command()` with known parameters
2. **Query via `bouncer_history`:** Verify command appears in results
3. **Time-range test:** Query for last 1 hour, verify only recent commands returned
4. **Source filter test:** Query with `source=claude-code`, verify filtering works
5. **Performance test:** Query 24h history, verify latency <100ms (vs previous ~500ms)

### Mock Paths
- `cmd_table.query()` → mock response with `Items` and `ScannedCount`
- `boto3.dynamodb.conditions.Key` → real implementation (no mock needed)

### Edge Cases
1. **GSI not yet created (during deployment)** → Query falls back to scan (add try/except for `IndexName` not found)
2. **Old items without `created_at`** → Not returned by GSI query (acceptable, will age out via TTL)
3. **Clock skew (client sends future `since_ts`)** → Query returns 0 items (correct behavior)
4. **Large result set (>1MB)** → DynamoDB paginates with `LastEvaluatedKey` (existing code handles this)
5. **GSI throttling** → Rare with PAY_PER_REQUEST, but log error and return partial results

## Deployment Plan

### Phase 1: Deploy Template (GSI Creation)
1. Update `template.yaml` with GSI definition
2. Deploy CloudFormation stack
3. Wait for GSI to reach `ACTIVE` status (~5 mins)
4. Verify GSI exists: `aws dynamodb describe-table --table-name bouncer-prod-command-history`

### Phase 2: Deploy Code (Write GSI Attributes)
1. Deploy updated `sequence_analyzer.py` (adds `type` and `created_at` to new writes)
2. Verify new commands include GSI fields in CloudWatch logs

### Phase 3: Deploy Code (Query GSI)
1. Deploy updated `mcp_history.py` (replaces scan with GSI query)
2. Test `bouncer_history` tool in dev environment
3. Monitor CloudWatch metrics: `DynamoDB → CommandHistoryTable → QueryCount` should increase

### Phase 4: Verify Cost Reduction
- Compare DynamoDB costs before/after (expect 70-90% reduction in CommandHistoryTable read costs)
- Monitor `ScannedCount` in responses (should match `Count` for GSI queries, vs 10x higher for scans)

## Rollback Plan
If GSI query causes issues:
1. **Revert `mcp_history.py`** to use scan (previous code)
2. **Keep GSI in place** (no harm, just unused)
3. **Investigate issue** (likely query logic bug, not GSI itself)

**Risk:** Low (GSI query is well-tested DynamoDB pattern)

## Migration Timeline
- **T+0:** Deploy template.yaml (GSI creation starts)
- **T+5min:** GSI backfill complete, status=ACTIVE
- **T+10min:** Deploy sequence_analyzer.py (new writes include GSI fields)
- **T+1hour:** Deploy mcp_history.py (queries use GSI)
- **T+24hours:** Verify cost reduction in DynamoDB billing dashboard
- **T+30days:** Old items (without GSI fields) age out via TTL, 100% of data in GSI

## Success Metrics
- **Latency:** Command history queries <100ms (P95)
- **Cost:** DynamoDB read costs for CommandHistoryTable reduced by ≥70%
- **Accuracy:** Query results match previous scan results (no data loss)
- **Availability:** No errors or throttling in CloudWatch logs
