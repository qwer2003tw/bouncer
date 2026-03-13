# S35-001c: Notifier Lambda handle_analyze - Plan

## Phase 1: 備份與準備

1. **備份現有檔案**
   ```bash
   cp deployer/notifier/app.py deployer/notifier/app.py.backup-s35-001c
   ```

2. **建立 feature branch**
   ```bash
   git checkout -b s35-001c-notifier-handle-analyze
   ```

## Phase 2: 複製 changeset_analyzer.py

**步驟：**

1. 複製檔案：
   ```bash
   cp src/changeset_analyzer.py deployer/notifier/changeset_analyzer.py
   ```

2. 驗證複製正確：
   ```bash
   diff src/changeset_analyzer.py deployer/notifier/changeset_analyzer.py
   ```

3. 檢查 import dependencies：
   ```bash
   grep "^import\|^from" deployer/notifier/changeset_analyzer.py
   ```

   **預期 dependencies:**
   - `time`, `uuid`, `boto3`, `dataclasses`, `typing` (標準庫)
   - `aws_lambda_powertools` (已在 notifier Lambda 環境中)
   - `botocore.exceptions` (boto3 內建)

## Phase 3: 新增 Helper 函數

**檔案：** `deployer/notifier/app.py`

### 3.1 新增 _send_task_failure()

在 `lambda_handler` 之後加入：

```python
def _send_task_failure(task_token: str, error_msg: str):
    """Send Step Functions task failure (helper)."""
    import boto3
    try:
        sfn = boto3.client('stepfunctions')
        sfn.send_task_failure(
            taskToken=task_token,
            error='ChangesetAnalysisFailed',
            cause=error_msg[:256],  # AWS limit
        )
        print(f"[SFN] Sent task_failure: {error_msg}")
    except Exception as e:
        print(f"[SFN] Failed to send task_failure: {e}")
```

### 3.2 新增 _send_infra_change_notification()

```python
def _send_infra_change_notification(
    deploy_id: str,
    project_id: str,
    change_count: int,
    changeset_name: str,
):
    """Send Telegram notification for infra changes (requires manual approval)."""
    text = (
        f"⚠️ *基礎架構變更檢測*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🆔 *Deploy ID：* `{deploy_id}`\n"
        f"🔧 *變更數量：* {change_count} 個資源\n"
        f"📋 *Changeset：* `{changeset_name}`\n\n"
        f"⚠️ 此 deploy 包含 **非純 code 變更**，需要人工審批。\n"
        f"請在 5 分鐘內審批，否則 deploy 將自動取消。"
    )

    try:
        send_telegram_message(text)
    except Exception as e:
        print(f"[Telegram] Failed to send notification: {e}")
```

## Phase 4: 新增 handle_analyze() 函數

**檔案：** `deployer/notifier/app.py`

**步驟：**

1. 在 `lambda_handler` 之後（或 helper functions 之後）加入 `handle_analyze()` 函數

2. 實作邏輯：
   - 參數驗證（`deploy_id`, `project_id`, `template_s3_url`, `stack_name`）
   - Import `changeset_analyzer`（使用 try-except 處理 ImportError）
   - 建立 CloudFormation client
   - 呼叫 `create_dry_run_changeset()`
   - 呼叫 `analyze_changeset()`
   - 呼叫 `cleanup_changeset()`（best-effort）
   - 組裝 output（`is_code_only`, `change_count`, `changeset_name`）
   - 如果 `not is_code_only`，呼叫 `_send_infra_change_notification()`
   - Exception handling（呼叫 `_send_task_failure` 如果有 `task_token`）

3. 加入 docstring 和註解

## Phase 5: 修改 lambda_handler()

**檔案：** `deployer/notifier/app.py` (Lines 25-41)

**步驟：**

1. 在 `lambda_handler` 的 if-elif chain 中加入：
   ```python
   elif action == 'analyze':
       return handle_analyze(event)
   ```

2. 驗證所有 action 都有對應的 handler

## Phase 6: 更新 IAM 權限

**檔案：** `deployer/template.yaml` (Lines 360-402)

### 6.1 新增 CloudFormation 權限

在 NotifierLambdaRole 的 Policies 中加入：

```yaml
- Sid: CloudFormation
  Effect: Allow
  Action:
    - cloudformation:CreateChangeSet
    - cloudformation:DescribeChangeSet
    - cloudformation:DeleteChangeSet
    - cloudformation:DescribeStacks
  Resource: "*"
```

### 6.2 新增 S3 權限

```yaml
- Sid: S3TemplateRead
  Effect: Allow
  Action:
    - s3:GetObject
  Resource:
    - !Sub "${ArtifactsBucket.Arn}/*"
```

### 6.3 驗證已存在的 Step Functions 權限

確認 S35-001a 已加入：

```yaml
- Sid: StepFunctionsCallback
  Effect: Allow
  Action:
    - states:SendTaskSuccess
    - states:SendTaskFailure
    - states:SendTaskHeartbeat
  Resource: !Ref DeployStateMachine
```

### 6.4 YAML 驗證

```bash
cfn-lint deployer/template.yaml --non-zero-exit-code error
```

## Phase 7: 本地測試

### 7.1 Python 語法檢查

```bash
python3 -m py_compile deployer/notifier/app.py
python3 -m py_compile deployer/notifier/changeset_analyzer.py
```

### 7.2 Import 檢查

```bash
cd deployer/notifier
python3 -c "from changeset_analyzer import create_dry_run_changeset, analyze_changeset; print('OK')"
```

## Phase 8: Unit Tests

**檔案：** `tests/test_notifier_analyze.py`

**步驟：**

1. 建立測試檔案

2. 撰寫測試：
   - `test_handle_analyze_code_only()`
   - `test_handle_analyze_infra_change()`
   - `test_handle_analyze_missing_params()`
   - `test_handle_analyze_changeset_failed()`
   - `test_handle_analyze_import_error()`

3. 執行測試：
   ```bash
   pytest tests/test_notifier_analyze.py -v
   ```

## Phase 9: 測試環境部署

### 9.1 部署 NotifierLambda

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

### 9.2 驗證 Lambda 包含 changeset_analyzer.py

```bash
aws lambda get-function \
  --function-name bouncer-deployer-notifier \
  --query 'Code.Location' \
  --output text | xargs curl -s -o /tmp/lambda.zip

unzip -l /tmp/lambda.zip | grep changeset_analyzer
```

**預期輸出：**
```
changeset_analyzer.py
```

### 9.3 手動測試 Lambda

建立測試 event：

```json
{
  "action": "analyze",
  "deploy_id": "test-s35-001c",
  "project_id": "bouncer-deployer-test",
  "template_s3_url": "https://sam-deployer-artifacts-190825685292.s3.amazonaws.com/bouncer-deployer-test/packaged-template.yaml",
  "stack_name": "bouncer-deployer-test"
}
```

執行測試：

```bash
aws lambda invoke \
  --function-name bouncer-deployer-notifier \
  --payload file://test-event.json \
  /tmp/lambda-response.json

cat /tmp/lambda-response.json
```

**預期輸出：**
```json
{
  "is_code_only": true,  // or false
  "change_count": 0,
  "changeset_name": "bouncer-dryrun-..."
}
```

## Phase 10: Integration Test（需 S35-001a + S35-001b 完成）

### 10.1 觸發 SFN Execution

```bash
aws stepfunctions start-execution \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name bouncer-deployer-test \
    --query 'Stacks[0].Outputs[?OutputKey==`DeployStateMachine`].OutputValue' \
    --output text) \
  --input '{
    "deploy_id": "test-s35-001c-full",
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

### 10.2 監控 SFN Execution

```bash
aws stepfunctions describe-execution \
  --execution-arn <execution-arn> \
  --query 'status'
```

**預期行為：**
- code-only 變更：`AnalyzeChangeset` → `CheckChangesetResult` → `SamDeploy` → `NotifySuccess`
- infra 變更：`AnalyzeChangeset` → `CheckChangesetResult` → `WaitForInfraApproval`（等待審批）

### 10.3 監控 CloudWatch Logs

```bash
aws logs tail /aws/lambda/bouncer-deployer-notifier --follow
```

**預期輸出：**
```
[analyze] Creating dry-run changeset for test-stack...
[analyze] Analyzing changeset bouncer-dryrun-...
[analyze] Result: is_code_only=true, changes=0
```

## Phase 11: Code Review & Merge

### 11.1 建立 PR

```bash
git add \
  deployer/notifier/app.py \
  deployer/notifier/changeset_analyzer.py \
  deployer/template.yaml \
  tests/test_notifier_analyze.py

git commit -m "feat(deployer): S35-001c - notifier Lambda handle_analyze"
git push origin s35-001c-notifier-handle-analyze
```

### 11.2 PR Checklist

- [ ] `py_compile` 通過
- [ ] Unit tests 通過
- [ ] cfn-lint 通過
- [ ] 測試環境 Lambda 部署成功
- [ ] 手動測試 Lambda 成功
- [ ] Integration test 成功（SFN execution）
- [ ] CloudWatch Logs 確認 changeset 分析執行
- [ ] IAM 權限驗證通過
- [ ] 無 breaking changes

## Phase 12: Production 部署

### 12.1 部署到 Production

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

### 12.2 驗證 Production Lambda

```bash
aws lambda get-function \
  --function-name bouncer-deployer-notifier \
  --query 'Code.Location' \
  --output text | xargs curl -s -o /tmp/lambda-prod.zip

unzip -l /tmp/lambda-prod.zip | grep changeset_analyzer
```

### 12.3 觸發測試 Deploy

觸發一次測試 deploy（非 critical project），驗證：
- AnalyzeChangeset state 成功執行
- code-only 變更自動進入 SamDeploy
- 或 infra 變更發送 Telegram 通知

### 12.4 監控 CloudWatch Logs

```bash
aws logs tail /aws/lambda/bouncer-deployer-notifier --follow
```

## 回滾計畫

如果 Production 失敗：

### 回滾 Lambda

```bash
# 回滾 app.py
cp deployer/notifier/app.py.backup-s35-001c deployer/notifier/app.py

# 移除 changeset_analyzer.py
rm deployer/notifier/changeset_analyzer.py

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

### 回滾 IAM 權限

```bash
# 回滾 template.yaml
git checkout HEAD~1 deployer/template.yaml

# 重新部署
sam deploy ...
```

## 注意事項

1. **changeset_analyzer.py dependencies：** 確保 `aws_lambda_powertools` 在 Lambda 環境中（已包含在 notifier Lambda）
2. **IAM 權限生效時間：** 部署後等待 1-2 分鐘再測試
3. **CloudFormation changeset cleanup：** `cleanup_changeset` 是 best-effort，失敗不影響主流程
4. **Telegram notification：** infra 變更通知只在 SFN context 中發送（不影響舊版 deploy）

## 相依性

- **Depends on:** S35-001a (SFN flow changes), S35-001b (sam_deploy.py taskToken callback)
- **Blocks:** None（功能完整）
