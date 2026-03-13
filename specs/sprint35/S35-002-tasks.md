# S35-002: Fix TestOrphanApprovalCleanup - Tasks

## Task Breakdown

### Task 1: 診斷當前測試失敗 (2 hours)
- [ ] 執行失敗的測試，記錄具體失敗訊息
- [ ] 建立 `test_notification_behavior.py` 驗證 `send_approval_request` 行為
- [ ] 執行診斷測試，填寫診斷結果表格
- [ ] 確定需要修改測試 mock 還是需要新增 cleanup 邏輯

### Task 2: 檢查 mcp_execute.py Cleanup 邏輯 (1 hour)
- [ ] 搜尋 `_submit_for_approval` 函數
- [ ] 檢查是否已有 `if not notified.ok:` cleanup 邏輯
- [ ] 如果沒有，規劃新增位置

### Task 3: 新增 Cleanup 邏輯（如果需要）(2 hours)
- [ ] 在 `_submit_for_approval` 加入 `if not notified.ok:` 分支
- [ ] 實作 `table.delete_item()` cleanup
- [ ] 加入 exception handling（catch ClientError）
- [ ] 加入 logger warning 和 error
- [ ] 回傳 `mcp_error(...)` 而非 `mcp_result(...)`

### Task 4: 修改測試 Mock (1 hour)
- [ ] 修改 `test_execute_telegram_failure_ddb_no_record_returns_error` mock
- [ ] 修改 `test_execute_telegram_exception_ddb_no_record_returns_error` mock（如果需要）
- [ ] 確保 mock 與 `send_approval_request` 行為一致

### Task 5: 執行測試驗證 (1 hour)
- [ ] 執行 `TestOrphanApprovalCleanup` 測試
- [ ] 執行完整測試套件
- [ ] 檢查 coverage
- [ ] 確保無 regression

### Task 6: Code Review & Merge (1 hour)
- [ ] 建立 PR
- [ ] 撰寫 PR description
- [ ] PR Checklist 檢查
- [ ] Code review
- [ ] Merge to main

### Task 7: Post-Merge 驗證 (30 min)
- [ ] 確認 CI pipeline 通過
- [ ] 清理臨時測試檔案（`test_notification_behavior.py`）

## Total Estimated Time: 8.5 hours

## TCS Calculation

**D1 Files (檔案數量):** 1 (test_callbacks_main.py，可能 + mcp_execute.py) = **1/5**

**D2 Cross-module (跨模組):** 跨 tests + mcp_execute + notifications = **2/4**

**D3 Testing (測試需求):** 修復 existing tests = **1/4**

**D4 Infra (基礎建設):** 無 = **0/4**

**D5 External (外部 API):** 無 = **0/4**

**Total TCS:** 1 + 2 + 1 + 0 + 0 = **4** (Simple)

## Dependencies

- **Blocked by:** None（獨立 task）
- **Blocks:** None

## Risk Assessment

- **Low Risk:** 只修復測試，如果新增 cleanup 邏輯也是 defensive（不影響正常流程）
- **Mitigation:** 完整測試套件驗證確保無 regression
- **Rollback Plan:** Git revert（測試檔案或 mcp_execute.py）

## Acceptance Criteria

- [ ] `test_execute_telegram_failure_ddb_no_record_returns_error` 通過
- [ ] `test_execute_telegram_exception_ddb_no_record_returns_error` 通過
- [ ] 完整測試套件通過，無 regression
- [ ] Code coverage 不下降
- [ ] [如果新增 cleanup 邏輯] logger 訊息正確輸出
- [ ] PR merged
