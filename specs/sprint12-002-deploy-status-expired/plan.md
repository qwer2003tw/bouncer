# Sprint 12-002: Plan — deploy_status 區分 expired vs pending

> Generated: 2026-03-05

---

## Technical Context

### 現狀分析

1. **`get_deploy_status(deploy_id)`** (`deployer.py:548-650`):
   - 查 `deployer_history_table` by `deploy_id`
   - Record not found → `{status: 'pending', deploy_id, message: 'Deploy record not found yet, please retry'}`
   - 沒有交叉查詢 main request table

2. **Deploy approval flow**:
   - `mcp_tool_deploy()` 建立 approval record 在 main `bouncer-requests` table，key=`request_id`，帶 `ttl`
   - Approval 通過後 → `start_deploy()` → 建 deploy record 在 `deployer_history_table`，key=`deploy_id`
   - Deploy ID 格式：`deploy-{uuid[:12]}`
   - Request ID 格式：`deploy:{project_id}-{uuid}`

3. **問題**：`deploy_id` 和 `request_id` 是不同的 key，目前沒有直接從 `deploy_id` 反查 `request_id` 的機制。

### Design

#### Approach: 基於 deploy_id 時間戳推斷

`deploy_id` 格式是 `deploy-{hex12}`，不包含建立時間。但可以從 deploy history table 的查詢結果來判斷：

**方案 A（推薦）：Record-not-found 改為 `not_found`，增加 TTL 感知**

當 `get_deploy_record(deploy_id)` 回傳 None：
1. 回傳 `{status: 'not_found'}` 取代 `{status: 'pending'}`
2. Agent 端判斷：
   - 剛發 deploy request → 短暫 retry（deploy 可能還沒開始）
   - 等了很久仍是 `not_found` → approval 可能過期

**方案 B：查 approval record 交叉驗證**

需要在 deploy record 中存 `request_id`（目前沒有），或建 GSI。

**選擇方案 A**，因為：
- 改動最小（只改一個回傳值）
- 語意更正確（`pending` 暗示「正在等待」，但 record 根本不存在不是 pending）
- Agent 端已有 timeout 邏輯

#### 額外改進：Deploy record 帶 `request_id`

在 `start_deploy()` 建 deploy record 時加入 `request_id` 欄位（如果 caller 提供）。這為未來交叉查詢打基礎，但本 sprint 不做反查。

#### API 回傳值變更

```python
# Before
if not record:
    return {
        'status': 'pending',
        'deploy_id': deploy_id,
        'message': 'Deploy record not found yet, please retry',
    }

# After
if not record:
    return {
        'status': 'not_found',
        'deploy_id': deploy_id,
        'message': 'Deploy record not found. If recently requested, retry shortly. If request expired, re-submit.',
    }
```

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| Agent 依賴 `status == 'pending'` 判斷 | 中 | 中 | Agent 應改為判斷 non-terminal states |
| 短暫 race condition 誤報 `not_found` | 低 | 低 | Agent retry 邏輯已處理 |

## Breaking Change 評估

`status: 'pending'` → `status: 'not_found'` 是語意變更。目前消費者只有 OpenClaw agent（`bouncer_deploy_status`），且 AGENTS.md 已記錄「poll `bouncer_deploy_status`」。影響可控。

## Testing Strategy

- 單元測試：record not found → status == 'not_found'
- 單元測試：record exists → 原有行為不變
- 回歸：確認 `mcp_tool_deploy_status` isError 邏輯不受影響
