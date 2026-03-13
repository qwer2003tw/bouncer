# S35-001a: SFN Flow Changes - Tasks

## Task Breakdown

### Task 1: 備份與準備 (1 hour)
- [ ] 備份 `deployer/template.yaml` → `deployer/template.yaml.backup-s35-001a`
- [ ] 建立 feature branch `s35-001a-sfn-flow-changes`
- [ ] 驗證 branch 正常運作

### Task 2: 修改 Step Functions Definition (4 hours)
- [ ] 在 `StartBuild` state 加入 `SFN_TASK_TOKEN` 環境變數
- [ ] 修改 `StartBuild` 的 `Next` 和 `Resource`
- [ ] 新增 `WaitForPackage` state
- [ ] 新增 `AnalyzeChangeset` state
- [ ] 新增 `CheckChangesetResult` state (Choice)
- [ ] 新增 `WaitForInfraApproval` state
- [ ] 新增 `SamDeploy` state
- [ ] 調整 `NotifySuccess` 和 `NotifyFailure` 的 input paths
- [ ] 驗證所有 `Catch` blocks 指向 `NotifyFailure`

### Task 3: 更新 IAM 權限 (2 hours)
- [ ] NotifierLambdaRole 加入 `states:SendTaskSuccess`, `states:SendTaskFailure`, `states:SendTaskHeartbeat`
- [ ] 驗證 StepFunctionsRole 已有 `lambda:InvokeFunction` 權限
- [ ] 驗證 CodeBuildRole 已有 DynamoDB 權限

### Task 4: 修改 CodeBuild BuildSpec (1 hour)
- [ ] 在 `build` phase 加入環境變數檢查邏輯
- [ ] 保留 assume-role 邏輯
- [ ] 調整 `sam_deploy.py` 調用方式（準備支援 `--package-only` 和 `--deploy-only`）

### Task 5: 本地驗證 (1 hour)
- [ ] 執行 `cfn-lint deployer/template.yaml`
- [ ] 執行 `sam build && sam validate`
- [ ] 檢查 YAML 語法正確

### Task 6: 測試環境部署 (2 hours)
- [ ] 部署到測試環境 `bouncer-deployer-test`
- [ ] 驗證 SFN definition（執行 `describe-state-machine`）
- [ ] 手動觸發 SFN execution，驗證新 states 存在
- [ ] 驗證 IAM 權限生效

### Task 7: Integration Test (2 hours)
- [ ] 建立 `tests/test_s35_001a_sfn_flow.py`
- [ ] 撰寫 `test_sfn_definition_has_new_states()`
- [ ] 撰寫 `test_sfn_iam_permissions()`
- [ ] 執行測試，確保通過

### Task 8: 文檔更新 (1 hour)
- [ ] 更新 `deployer/README.md`，加入新 SFN flow 說明
- [ ] 更新 `CHANGELOG.md`

### Task 9: Code Review & Merge (1 hour)
- [ ] 建立 PR
- [ ] PR Checklist 檢查
- [ ] Code review
- [ ] Merge to main

### Task 10: Production 部署 (2 hours)
- [ ] 部署到 Production `bouncer-deployer`
- [ ] 監控 CloudWatch Logs
- [ ] 驗證 Production SFN definition
- [ ] 觸發測試 deploy，驗證新 states 正常運作

## Total Estimated Time: 17 hours

## TCS Calculation

**D1 Files (檔案數量):** 1 (template.yaml) = **2/5**

**D2 Cross-module (跨模組):** 跨 SFN + NotifierLambda + CodeBuild = **4/4**

**D3 Testing (測試需求):** 新增 integration test = **2/4**

**D4 Infra (基礎建設):** CloudFormation 架構變更 = **4/4**

**D5 External (外部 API):** Step Functions API = **1/4**

**Total TCS:** 2 + 4 + 2 + 4 + 1 = **13** (Medium-High)

## Dependencies

- **Blocks:** S35-001b, S35-001c（在 S35-001a 完成前，sam_deploy.py 和 notifier/app.py 無法實作）
- **Blocked by:** None

## Risk Assessment

- **High Risk:** SFN definition 錯誤可能導致所有 deploy 失敗
- **Mitigation:** 測試環境先部署，驗證通過後再 Production
- **Rollback Plan:** 保留 template.yaml.backup，隨時可回滾

## Acceptance Criteria

- [ ] `deployer/template.yaml` 包含 5 個新 states
- [ ] NotifierLambdaRole 有 Step Functions callback 權限
- [ ] 測試環境 SFN execution 可正常啟動並進入 WaitForPackage state
- [ ] Integration tests 全部通過
- [ ] `deployer/README.md` 已更新
- [ ] Production 部署成功，無 breaking changes
