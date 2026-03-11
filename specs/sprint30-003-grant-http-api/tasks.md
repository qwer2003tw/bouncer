# sprint30-003: Tasks

## Task Breakdown

### Task 1: `mcp_tool_grant_execute` 實作 + schema + app.py wiring

**TCS: Medium**（cross-module: mcp_execute.py + tool_schema.py + app.py）

#### Sub-tasks

| # | Sub-task | File | 說明 |
|---|----------|------|------|
| 1.1 | 新增 tool schema | `src/tool_schema.py` | 在 `bouncer_revoke_grant` 之後加 `bouncer_grant_execute` entry |
| 1.2 | 實作 `mcp_tool_grant_execute` | `src/mcp_execute.py` | 完整 fail-fast pipeline（見 plan.md Step 2） |
| 1.3 | app.py import + dispatch | `src/app.py` | import 新 function + 加入 `TOOL_HANDLERS` dict |
| 1.4 | 單元測試 | `tests/test_grant_execute_tool.py` | 16+ 測試案例，覆蓋所有 Acceptance Scenarios |

#### Acceptance Criteria

- [ ] `bouncer_grant_execute` 出現在 `tools/list` response
- [ ] Happy path: active grant + valid command → 成功執行並回傳結果
- [ ] 所有 14 個錯誤 status 有對應測試覆蓋
- [ ] Compliance check 不被跳過（即使 grant 已核准）
- [ ] `source` 不匹配回傳 `grant_not_found`（不洩漏 grant 存在性）
- [ ] Audit log 寫入 DynamoDB（decision_type='grant_approved'）
- [ ] Telegram 通知發送（`send_grant_execute_notification`）
- [ ] Paged output 正確處理
- [ ] 命令執行失敗（exit_code != 0）仍標記已使用，仍記錄 audit
- [ ] `pytest tests/test_grant_execute_tool.py` 全部通過
- [ ] `pytest tests/test_grant.py` 既有測試不受影響（regression）

#### Implementation Notes

1. **`mcp_tool_grant_execute` 的 pre-check 區分錯誤原因：**
   - `try_use_grant_command` 只回 bool，無法區分「已使用」vs「SEC-009 上限」
   - 解法：呼叫前先讀 `grant['used_commands']` + `is_dangerous(cmd)` 做 pre-check
   - `try_use_grant_command` 仍做為最終原子性確認（防並發）
   - Pre-check 錯誤 → 回傳具體 status；atomic check 失敗 → 回傳 generic `command_already_used`

2. **response 的 `commands_remaining` 計算：**
   - `allow_repeat=False`：`len(granted_commands) - len(used_commands) - 1`（-1 因為剛用了一個）
   - `allow_repeat=True`：回傳 `null`（無意義，命令可重複）

3. **account 解析邏輯與 `mcp_tool_request_grant` 一致：**
   - 無 account → DEFAULT_ACCOUNT_ID
   - 有 account → validate_account_id + get_account + check enabled
   - 然後與 grant['account_id'] 比對

4. **Unicode normalize：** 對 command 呼叫 `_normalize_command`（SEC-003）再做 grant 比對

---

## TCS Summary

| Task | TCS | Modules | 估計工時 |
|------|-----|---------|---------|
| Task 1 | Medium | mcp_execute + tool_schema + app.py + tests | 2-3 hr |

**Total: 1 task, TCS Medium**

理由：
- 邏輯複雜度中等（核心是組合已存在的函數，但 fail-fast error handling 有 14 個分支）
- Cross-module 修改（3 個 src 檔案 + 1 個新測試檔）
- 安全敏感（驗證鏈的順序和完整性是關鍵）
- 不需要 DynamoDB schema 變更（利用現有 grant session item）
- 不需要新的 dependency
