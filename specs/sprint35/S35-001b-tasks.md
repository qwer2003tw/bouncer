# S35-001b: sam_deploy.py TaskToken Callback - Tasks

## Task Breakdown

### Task 1: 備份與準備 (30 min)
- [ ] 備份 `deployer/scripts/sam_deploy.py`
- [ ] 建立 feature branch `s35-001b-sam-deploy-tasktoken`

### Task 2: 新增命令列參數 (1 hour)
- [ ] 在 `main()` 加入 `--package-only` 和 `--deploy-only` 參數解析
- [ ] 新增互斥檢查
- [ ] 新增 `SFN_TASK_TOKEN` 環境變數讀取
- [ ] 驗證參數邏輯正確

### Task 3: 新增 TaskToken Callback 函數 (2 hours)
- [ ] 撰寫 `send_sfn_task_token_callback()` 函數
- [ ] 實作 boto3 Step Functions client 邏輯
- [ ] 組裝 output JSON
- [ ] 實作 exception handling（non-fatal）
- [ ] 加入 docstring 和註解

### Task 4: 修改 main() 函數流程 (2 hours)
- [ ] 在 `_run_sam_package()` 後加入條件判斷（`if not deploy_only`）
- [ ] 加入 taskToken callback 調用（`if sfn_task_token`）
- [ ] 加入 `package_only` early exit
- [ ] 驗證原有 deploy 邏輯不受影響
- [ ] 驗證 import conflict 邏輯不受影響

### Task 5: 修改 CodeBuild IAM Role (1 hour)
- [ ] 在 `deployer/template.yaml` 加入 Step Functions callback 權限
- [ ] 執行 `cfn-lint` 驗證 YAML 語法
- [ ] 執行 `sam validate`

### Task 6: 本地測試 (1 hour)
- [ ] 執行 `python3 -m py_compile deployer/scripts/sam_deploy.py`
- [ ] 驗證 import 正常

### Task 7: Unit Tests (3 hours)
- [ ] 建立 `tests/test_sam_deploy_tasktoken.py`
- [ ] 撰寫 `test_send_sfn_task_token_callback_success()`
- [ ] 撰寫 `test_send_sfn_task_token_callback_no_token()`
- [ ] 撰寫 `test_send_sfn_task_token_callback_exception()`
- [ ] 撰寫 `test_main_package_only_exits_early()`
- [ ] 撰寫 `test_main_deploy_only_skips_package()`
- [ ] 執行測試，確保全部通過

### Task 8: 上傳到 S3 (30 min)
- [ ] 驗證 `deployer/Makefile` 的 `upload-deploy-script` target
- [ ] 上傳到測試環境 S3

### Task 9: 測試環境驗證 (3 hours)
- [ ] 部署 IAM 權限變更到測試環境
- [ ] 觸發 SFN execution
- [ ] 監控 CodeBuild logs，驗證 taskToken callback 送出
- [ ] 驗證 SFN WaitForPackage state 收到 callback
- [ ] 驗證 DDB `bouncer-projects` table 更新

### Task 10: Integration Test (2 hours)
- [ ] 測試 `--package-only` flag
- [ ] 測試 `--deploy-only` flag
- [ ] 測試無 `SFN_TASK_TOKEN` 環境變數（向後相容）
- [ ] 測試 taskToken callback 失敗場景

### Task 11: Code Review & Merge (1 hour)
- [ ] 建立 PR
- [ ] PR Checklist 檢查
- [ ] Code review
- [ ] Merge to main

### Task 12: Production 部署 (2 hours)
- [ ] 上傳 `sam_deploy.py` 到 Production S3
- [ ] 部署 IAM 權限變更到 Production
- [ ] 觸發測試 deploy，驗證流程正常
- [ ] 監控 CloudWatch Logs

## Total Estimated Time: 18 hours

## TCS Calculation

**D1 Files (檔案數量):** 1 (sam_deploy.py) = **1/5**

**D2 Cross-module (跨模組):** 跨 CodeBuild + Step Functions = **3/4**

**D3 Testing (測試需求):** 新增 unit test = **2/4**

**D4 Infra (基礎建設):** IAM 權限變更 = **2/4**

**D5 External (外部 API):** Step Functions send_task_success API = **2/4**

**Total TCS:** 1 + 3 + 2 + 2 + 2 = **10** (Medium)

## Dependencies

- **Blocked by:** S35-001a (SFN flow changes)
- **Blocks:** S35-001c (notifier/app.py handle_analyze)

## Risk Assessment

- **Medium Risk:** taskToken callback 失敗會導致 SFN timeout（但 CodeBuild 繼續）
- **Mitigation:** Exception handling 設計為 non-fatal，失敗只 print warning
- **Rollback Plan:** 回滾 S3 上的 `sam_deploy.py` + 回滾 IAM 權限

## Acceptance Criteria

- [ ] `sam_deploy.py` 支援 `--package-only` 和 `--deploy-only` flag
- [ ] `send_sfn_task_token_callback()` 函數正確實作
- [ ] CodeBuildRole 有 Step Functions callback 權限
- [ ] Unit tests 全部通過
- [ ] 測試環境 SFN execution 成功收到 taskToken callback
- [ ] 向後相容（無 `SFN_TASK_TOKEN` 時正常運作）
- [ ] Production 部署成功
