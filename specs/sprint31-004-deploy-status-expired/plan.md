# Implementation Plan: deploy_status 區分 expired vs pending

## Technical Context
- 影響檔案：
  - `src/mcp_admin.py` — `mcp_tool_status` (主要修改)
- 影響測試：
  - `tests/test_regression_deploy_status_expired.py` (已存在 — 已涵蓋 deployer 路徑，需新增 admin 路徑)
  - 新增：`tests/test_sprint31_004_status_expired.py`
- 技術風險：
  - 低。純粹 read-only 邏輯變更
  - Decimal → int 轉換：DDB 回傳 Decimal，`int(item.get('ttl', 0))` 應可正確處理

## Constitution Check
- 安全影響：無。不改變授權邏輯，只改變 status 回報
- 成本影響：無額外 DDB 或 API 費用
- 架構影響：低。Pattern 已在 deployer.py 存在，只是 copy 邏輯到 mcp_admin.py

## Implementation Phases

### Phase 1: Add expiry check in mcp_tool_status (mcp_admin.py)
在 `return mcp_result(...)` 之前加入：

```python
# TTL expiry check: if still pending_approval but TTL has passed → expired
if item.get('status') == 'pending_approval':
    ttl = int(item.get('ttl', 0))
    if ttl and int(time.time()) > ttl:
        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'expired',
                    'request_id': request_id,
                    'message': '請求已過期，未在時限內批准',
                    'hint': 'Re-issue the command to create a new request.',
                })
            }],
            'isError': False,
        })
```

### Phase 2: Add `import time` if not present (mcp_admin.py)
- Check top of file for `import time` — add if missing

### Phase 3: Tests
- 新增 `tests/test_sprint31_004_status_expired.py`:
  - test_bouncer_status_expired_when_ttl_passed
  - test_bouncer_status_pending_when_ttl_not_passed
  - test_bouncer_status_approved_ttl_ignored
  - test_bouncer_status_no_ttl_remains_pending
  - test_bouncer_status_ttl_zero_remains_pending
