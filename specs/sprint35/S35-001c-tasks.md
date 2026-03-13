# S35-001c: Notifier Lambda handle_analyze - Tasks

## Task Breakdown

### Task 1: 備份與準備 (30 min)
- [ ] 備份 `deployer/notifier/app.py`
- [ ] 建立 feature branch `s35-001c-notifier-handle-analyze`

### Task 2: 複製 changeset_analyzer.py (30 min)
- [ ] 複製 `src/changeset_analyzer.py` → `deployer/notifier/changeset_analyzer.py`
- [ ] 驗證複製正確（`diff` 檢查）
- [ ] 檢查 import dependencies

### Task 3: 新增 Helper 函數 (1.5 hours)
- [ ] 新增 `_send_task_failure()` 函數
- [ ] 新增 `_send_infra_change_notification()` 函數
- [ ] 加入 docstring 和註解

### Task 4: 新增 handle_analyze() 函數 (3 hours)
- [ ] 實作參數驗證
- [ ] 實作 import `changeset_analyzer` 邏輯（try-except）
- [ ] 實作 CloudFormation client 建立
- [ ] 實作 `create_dry_run_changeset()` 呼叫
- [ ] 實作 `analyze_changeset()` 呼叫
- [ ] 實作 `cleanup_changeset()` 呼叫
- [ ] 實作 output 組裝
- [ ] 實作 infra 變更通知邏輯
- [ ] 實作 exception handling
- [ ] 加入 docstring 和註解

### Task 5: 修改 lambda_handler() (30 min)
- [ ] 在 if-elif chain 加入 `action == 'analyze'` 分支
- [ ] 驗證所有 action 都有對應 handler

### Task 6: 更新 IAM 權限 (1.5 hours)
- [ ] 在 NotifierLambdaRole 加入 CloudFormation 權限
- [ ] 在 NotifierLambdaRole 加入 S3 權限
- [ ] 驗證已存在的 Step Functions 權限
- [ ] 執行 `cfn-lint` 驗證 YAML 語法

### Task 7: 本地測試 (1 hour)
- [ ] 執行 `python3 -m py_compile` 檢查語法
- [ ] 驗證 import `changeset_analyzer` 正常

### Task 8: Unit Tests (3 hours)
- [ ] 建立 `tests/test_notifier_analyze.py`
- [ ] 撰寫 `test_handle_analyze_code_only()`
- [ ] 撰寫 `test_handle_analyze_infra_change()`
- [ ] 撰寫 `test_handle_analyze_missing_params()`
- [ ] 撰寫 `test_handle_analyze_changeset_failed()`
- [ ] 撰寫 `test_handle_analyze_import_error()`
- [ ] 執行測試，確保全部通過

### Task 9: 測試環境部署 (2 hours)
- [ ] 部署 NotifierLambda 到測試環境
- [ ] 驗證 Lambda 包含 `changeset_analyzer.py`
- [ ] 手動測試 Lambda（執行 `lambda invoke`）
- [ ] 驗證 IAM 權限生效

### Task 10: Integration Test (3 hours)
- [ ] 觸發 SFN execution（需 S35-001a + S35-001b 完成）
- [ ] 測試 code-only 變更流程
- [ ] 測試 infra 變更流程（驗證 Telegram 通知）
- [ ] 監控 CloudWatch Logs
- [ ] 驗證 SFN state transitions

### Task 11: Code Review & Merge (1 hour)
- [ ] 建立 PR
- [ ] PR Checklist 檢查
- [ ] Code review
- [ ] Merge to main

### Task 12: Production 部署 (2 hours)
- [ ] 部署到 Production
- [ ] 驗證 Production Lambda 包含 `changeset_analyzer.py`
- [ ] 觸發測試 deploy，驗證流程正常
- [ ] 監控 CloudWatch Logs

## Total Estimated Time: 19 hours

## TCS Calculation

**D1 Files (檔案數量):** 2 (notifier/app.py, changeset_analyzer.py copy) = **2/5**

**D2 Cross-module (跨模組):** 跨 notifier Lambda + changeset_analyzer + Step Functions = **3/4**

**D3 Testing (測試需求):** 新增 unit test = **3/4**

**D4 Infra (基礎建設):** IAM 權限變更 = **2/4**

**D5 External (外部 API):** CloudFormation changeset API + Step Functions API = **2/4**

**Total TCS:** 2 + 3 + 3 + 2 + 2 = **12** (Medium-High)

## Dependencies

- **Blocked by:** S35-001a (SFN flow changes), S35-001b (sam_deploy.py taskToken callback)
- **Blocks:** None（功能完整）

## Risk Assessment

- **Medium Risk:** changeset 分析失敗會導致 SFN AnalyzeChangeset state 失敗
- **Mitigation:** Exception handling 設計為呼叫 `send_task_failure`，SFN 進入 NotifyFailure
- **Rollback Plan:** 回滾 Lambda code + 回滾 IAM 權限

## Acceptance Criteria

- [ ] `deployer/notifier/app.py` 包含 `handle_analyze()` 函數
- [ ] `deployer/notifier/changeset_analyzer.py` 存在並可正常 import
- [ ] NotifierLambdaRole 有 CloudFormation changeset 權限
- [ ] Unit tests 全部通過
- [ ] 測試環境 SFN execution：
  - [ ] code-only 變更 → 自動進入 SamDeploy
  - [ ] infra 變更 → 發送 Telegram 通知 + 進入 WaitForInfraApproval
- [ ] Production 部署成功
- [ ] End-to-end test：觸發 deploy，驗證 auto-approve 流程正常
