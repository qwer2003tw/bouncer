# Sprint 10-001: Plan — deploy_status phase 不準確

> Generated: 2026-03-03

---

## Technical Context

### 現狀分析

1. **`get_deploy_status()`**（`deployer.py:534`）：透過 `get_deploy_record()` 從 DDB 取 record → 若 `status == 'RUNNING'` 且有 `execution_arn`，呼叫 SFN `describe_execution` 同步。若 record 不存在回傳 `{'error': '部署記錄不存在'}`。

2. **`mcp_tool_deploy_status()`**（`deployer.py:702`）：檢查回傳 dict 是否有 `error` key → 設 `isError: True`。

3. **`create_deploy_record()`**（`deployer.py:228`）：寫入 DDB，初始 status = PENDING。在 `start_deploy()` 裡呼叫。

4. **`start_deploy()`**（`deployer.py:300`）：先 `create_deploy_record(PENDING)` → `start_execution()` → `update_deploy_record(RUNNING)` → return。

5. **Deploy callback 流程**（`callbacks.py`）：approve → `start_deploy()` → 回傳結果。在 `start_deploy()` 之前 DDB 不存在任何 record。

### 問題點

- `start_deploy()` 在審批 callback 內才呼叫。Agent 可能在 callback 回傳之前就 poll → record 不存在 → error → agent fallback 到 `stepfunctions describe-execution` → 觸發自動審批通知。
- 缺少 `elapsed_seconds` / `duration_seconds` 讓 agent 難以判斷超時。

### 影響範圍

- `src/deployer.py` — `get_deploy_status()` + `mcp_tool_deploy_status()`
- `tests/` — 補/改測試

## Implementation Phases

### Phase 1: 修改 get_deploy_status()（deployer.py）

1. 當 `get_deploy_record()` 回傳 None 時，改回傳：
   ```python
   return {'status': 'pending', 'deploy_id': deploy_id, 'message': 'Deploy record not found yet, please retry'}
   ```

2. 在回傳 record 前，動態計算並加入時間欄位：
   - RUNNING: `record['elapsed_seconds'] = int(time.time()) - int(record.get('started_at', 0))`
   - SUCCESS/FAILED: `record['duration_seconds'] = int(record.get('finished_at', 0)) - int(record.get('started_at', 0))`

### Phase 2: 修改 mcp_tool_deploy_status()（deployer.py）

1. 移除 `if 'error' in record` 的 isError 邏輯（因為不再回傳 error dict）
2. 統一走正常 response path，讓 `status: pending` 視為正常回傳

### Phase 3: 測試

1. Unit test: `get_deploy_status()` record 不存在 → 回傳 `{status: pending}`
2. Unit test: `get_deploy_status()` RUNNING → 包含 `elapsed_seconds`
3. Unit test: `get_deploy_status()` SUCCESS → 包含 `duration_seconds`
4. 確認現有 deploy 相關測試不 break
