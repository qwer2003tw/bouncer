# S35-001b: sam_deploy.py TaskToken Callback - Plan

## Phase 1: 備份與準備

1. **備份現有 sam_deploy.py**
   ```bash
   cp deployer/scripts/sam_deploy.py deployer/scripts/sam_deploy.py.backup-s35-001b
   ```

2. **建立 feature branch**
   ```bash
   git checkout -b s35-001b-sam-deploy-tasktoken
   ```

## Phase 2: 新增命令列參數

**檔案：** `deployer/scripts/sam_deploy.py` (Lines 603-716)

**步驟：**

1. 在 `main()` 函數開頭加入參數解析：
   ```python
   package_only = "--package-only" in argv
   deploy_only = "--deploy-only" in argv
   ```

2. 新增互斥檢查：
   ```python
   if package_only and deploy_only:
       print("ERROR: --package-only and --deploy-only are mutually exclusive", file=sys.stderr)
       sys.exit(1)
   ```

3. 新增環境變數讀取：
   ```python
   sfn_task_token = os.environ.get("SFN_TASK_TOKEN", "").strip()
   ```

## Phase 3: 新增 TaskToken Callback 函數

**檔案：** `deployer/scripts/sam_deploy.py` (在 `update_template_s3_url` 之後，約 Line 478)

**步驟：**

1. 新增 `send_sfn_task_token_callback()` 函數：
   - 參數：`task_token`, `template_s3_url`, `artifacts_bucket`, `project_id`
   - 建立 boto3 Step Functions client
   - 組裝 output JSON（包含 `template_s3_url` 等）
   - 呼叫 `sfn.send_task_success(taskToken=..., output=...)`
   - Exception handling（non-fatal，只 print warning）

2. 加入 docstring，說明：
   - 何時被調用（package 完成後）
   - 為何是 non-fatal（SFN timeout 但 CodeBuild 繼續）
   - 回傳值格式

## Phase 4: 修改 main() 函數流程

**檔案：** `deployer/scripts/sam_deploy.py` (Lines 603-716)

**步驟：**

1. 在原有的 `_run_sam_package()` 調用後，加入條件判斷：
   ```python
   if not deploy_only:
       _run_sam_package(artifacts_bucket, project_id)
       update_template_s3_url(project_id, artifacts_bucket)

       if sfn_task_token:
           template_s3_url = f"https://{artifacts_bucket}.s3.amazonaws.com/{project_id}/packaged-template.yaml"
           send_sfn_task_token_callback(
               task_token=sfn_task_token,
               template_s3_url=template_s3_url,
               artifacts_bucket=artifacts_bucket,
               project_id=project_id,
           )
   ```

2. 在 package 後加入 early exit：
   ```python
   if package_only:
       print("[package-only] Exiting after package phase")
       sys.exit(0)
   ```

3. 保留原有的 deploy 邏輯（`_build_sam_cmd` → `_run_deploy`）

4. 確保 import conflict 解決邏輯不受影響

## Phase 5: 修改 CodeBuild IAM Role

**檔案：** `deployer/template.yaml` (Lines 270-356)

**步驟：**

1. 在 `CodeBuildRole` 的 `Policies` 中新增 statement：
   ```yaml
   - Sid: StepFunctionsCallback
     Effect: Allow
     Action:
       - states:SendTaskSuccess
       - states:SendTaskFailure
     Resource: !Ref DeployStateMachine
   ```

2. 驗證 YAML 語法：
   ```bash
   cfn-lint deployer/template.yaml --non-zero-exit-code error
   ```

## Phase 6: 本地測試

### 6.1 Python 語法檢查

```bash
python3 -m py_compile deployer/scripts/sam_deploy.py
```

### 6.2 Import 檢查

```bash
python3 -c "from deployer.scripts import sam_deploy; print(sam_deploy.__file__)"
```

## Phase 7: Unit Tests

**檔案：** `tests/test_sam_deploy_tasktoken.py`

**步驟：**

1. 建立測試檔案
2. 撰寫測試：
   - `test_send_sfn_task_token_callback_success()`
   - `test_send_sfn_task_token_callback_no_token()`
   - `test_send_sfn_task_token_callback_exception()`
   - `test_main_package_only_exits_early()`
   - `test_main_deploy_only_skips_package()`

3. 執行測試：
   ```bash
   pytest tests/test_sam_deploy_tasktoken.py -v
   ```

## Phase 8: 上傳到 S3

### 8.1 更新 Makefile（如需要）

**檔案：** `deployer/Makefile`

確認 `upload-deploy-script` target 存在：

```makefile
upload-deploy-script:
	aws s3 cp scripts/sam_deploy.py s3://$(ARTIFACTS_BUCKET)/deployer-scripts/sam_deploy.py
```

### 8.2 上傳到測試環境

```bash
export ARTIFACTS_BUCKET=sam-deployer-artifacts-190825685292  # 測試環境 bucket
make -C deployer upload-deploy-script
```

## Phase 9: 測試環境驗證

### 9.1 部署 IAM 權限變更

```bash
sam deploy \
  --template-file deployer/template.yaml \
  --stack-name bouncer-deployer-test \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    TelegramBotToken=$TELEGRAM_BOT_TOKEN \
    TelegramChatId=$TELEGRAM_CHAT_ID \
    CreateCFNRole=false
```

### 9.2 觸發 SFN Execution

```bash
aws stepfunctions start-execution \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name bouncer-deployer-test \
    --query 'Stacks[0].Outputs[?OutputKey==`DeployStateMachine`].OutputValue' \
    --output text) \
  --input '{
    "deploy_id": "test-s35-001b",
    "project_id": "bouncer-deployer-test",
    "git_repo": "your-org/your-repo",
    "branch": "main",
    "stack_name": "test-stack",
    "sam_template_path": ".",
    "sam_params": "{}",
    "github_pat_secret": "sam-deployer/github-pat",
    "target_role_arn": ""
  }'
```

### 9.3 監控 CodeBuild Logs

```bash
aws logs tail /aws/codebuild/sam-deployer --follow
```

**預期輸出：**
```
[package] sam package → s3://...
[package] Packaged template written to /tmp/packaged-template.yaml
[package] Uploaded packaged template YAML to s3://...
[DDB] Updated template_s3_url for bouncer-deployer-test: ...
[SFN] TaskToken callback sent: https://...
[package-only] Exiting after package phase
```

### 9.4 驗證 SFN State Transition

```bash
# 取得 execution ARN（從上面的 start-execution 輸出）
aws stepfunctions describe-execution \
  --execution-arn <execution-arn> \
  --query 'status'
```

**預期狀態：** `RUNNING`（進入 WaitForPackage state，然後進入 AnalyzeChangeset）

## Phase 10: Integration Test

### 10.1 完整流程測試

觸發一次完整 deploy（需要 S35-001c 完成後）：

1. StartBuild → WaitForPackage（taskToken callback）
2. AnalyzeChangeset → CheckChangesetResult
3. SamDeploy → NotifySuccess

### 10.2 驗證 DDB 更新

```bash
aws dynamodb get-item \
  --table-name bouncer-projects \
  --key '{"project_id": {"S": "bouncer-deployer-test"}}' \
  --query 'Item.template_s3_url.S'
```

**預期輸出：** `https://sam-deployer-artifacts-190825685292.s3.amazonaws.com/bouncer-deployer-test/packaged-template.yaml`

## Phase 11: Code Review & Merge

### 11.1 建立 PR

```bash
git add deployer/scripts/sam_deploy.py deployer/template.yaml tests/test_sam_deploy_tasktoken.py
git commit -m "feat(deployer): S35-001b - sam_deploy.py taskToken callback"
git push origin s35-001b-sam-deploy-tasktoken
```

### 11.2 PR Checklist

- [ ] `py_compile` 通過
- [ ] Unit tests 通過
- [ ] cfn-lint 通過
- [ ] 測試環境 SFN execution 成功收到 taskToken callback
- [ ] CodeBuild logs 確認 callback 送出
- [ ] IAM 權限驗證通過
- [ ] 無 breaking changes（向後相容）

## Phase 12: Production 部署

### 12.1 上傳 sam_deploy.py 到 Production S3

```bash
export ARTIFACTS_BUCKET=sam-deployer-artifacts-190825685292  # Production bucket
make -C deployer upload-deploy-script
```

### 12.2 部署 IAM 權限變更

```bash
sam deploy \
  --template-file deployer/template.yaml \
  --stack-name bouncer-deployer \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    TelegramBotToken=$TELEGRAM_BOT_TOKEN \
    TelegramChatId=$TELEGRAM_CHAT_ID \
    CreateCFNRole=false
```

### 12.3 觸發測試 Deploy

觸發一次測試 deploy（非 critical project），驗證：
- CodeBuild 成功下載新版 `sam_deploy.py`
- taskToken callback 成功送出
- SFN WaitForPackage state 收到 callback

### 12.4 監控 CloudWatch Logs

```bash
aws logs tail /aws/codebuild/sam-deployer --follow
```

## 回滾計畫

如果 Production 失敗：

### 回滾 sam_deploy.py

```bash
# 回滾到舊版
cp deployer/scripts/sam_deploy.py.backup-s35-001b deployer/scripts/sam_deploy.py

# 重新上傳到 S3
make -C deployer upload-deploy-script
```

### 回滾 IAM 權限

```bash
# 回滾 template.yaml
git checkout HEAD~1 deployer/template.yaml

# 重新部署
sam deploy \
  --template-file deployer/template.yaml \
  --stack-name bouncer-deployer \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    TelegramBotToken=$TELEGRAM_BOT_TOKEN \
    TelegramChatId=$TELEGRAM_CHAT_ID \
    CreateCFNRole=false
```

## 注意事項

1. **taskToken callback 是 non-fatal：** 失敗不中斷 CodeBuild，但 SFN 會 timeout
2. **向後相容：** 無 `SFN_TASK_TOKEN` 環境變數時，行為與舊版相同
3. **S3 upload 順序：** 必須先 package，再 send taskToken callback（確保 template 已上傳）
4. **IAM 權限生效時間：** 部署後等待 1-2 分鐘再測試

## 相依性

- **Depends on:** S35-001a (SFN flow changes)
- **Blocks:** S35-001c (notifier/app.py handle_analyze)
