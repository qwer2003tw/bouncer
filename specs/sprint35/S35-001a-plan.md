# S35-001a: SFN Flow Changes for Post-Package Changeset Analysis - Plan

## Phase 1: 備份與準備

1. **備份現有 template.yaml**
   ```bash
   cp deployer/template.yaml deployer/template.yaml.backup-s35-001a
   ```

2. **建立 feature branch**
   ```bash
   git checkout -b s35-001a-sfn-flow-changes
   ```

## Phase 2: 修改 Step Functions Definition

### 2.1 修改 DeployStateMachine

**檔案：** `deployer/template.yaml` (Lines 604-686)

**步驟：**

1. 在 `StartBuild` state 加入 `SFN_TASK_TOKEN` 環境變數
2. 修改 `StartBuild` 的 `Next` 從 `NotifySuccess` 改為 `WaitForPackage`
3. 修改 `StartBuild` 的 `Resource` 從 `sync` 改為 non-sync (移除 `.sync`)
4. 新增 `WaitForPackage` state
5. 新增 `AnalyzeChangeset` state
6. 新增 `CheckChangesetResult` state (Choice)
7. 新增 `WaitForInfraApproval` state
8. 新增 `SamDeploy` state（類似舊 StartBuild，但 Resource 是 `sync`）
9. 調整 `NotifySuccess` 和 `NotifyFailure` 的 input paths

**注意：**
- `StartBuild` 改為 non-sync（不等待 CodeBuild 完成）
- `SamDeploy` 使用 sync（等待 CodeBuild 完成）
- 所有新 states 的 `Catch` 都指向 `NotifyFailure`

### 2.2 驗證 YAML 語法

```bash
cfn-lint deployer/template.yaml --non-zero-exit-code error
```

## Phase 3: 更新 IAM 權限

### 3.1 NotifierLambdaRole

**檔案：** `deployer/template.yaml` (Lines 360-402)

**新增 Policy Statement：**

```yaml
- Sid: StepFunctionsCallback
  Effect: Allow
  Action:
    - states:SendTaskSuccess
    - states:SendTaskFailure
    - states:SendTaskHeartbeat
  Resource: !Ref DeployStateMachine
```

### 3.2 StepFunctionsRole

**檔案：** `deployer/template.yaml` (Lines 406-461)

**確認已存在：**

```yaml
- Sid: Lambda
  Effect: Allow
  Action:
    - lambda:InvokeFunction
  Resource:
    - !GetAtt NotifierLambda.Arn
```

（已存在，無需新增）

### 3.3 CodeBuildRole

**檔案：** `deployer/template.yaml` (Lines 270-356)

**確認已存在：**

```yaml
- Sid: ProjectsTableUpdateDirect
  Effect: Allow
  Action:
    - dynamodb:UpdateItem
    - dynamodb:GetItem
  Resource: "arn:aws:dynamodb:us-east-1:190825685292:table/bouncer-projects"
```

（已存在，無需新增）

## Phase 4: 修改 CodeBuild BuildSpec

**檔案：** `deployer/template.yaml` (Lines 515-561)

**變更：**

1. 在 `build` phase 的 commands 加入環境變數邏輯：
   - 檢查 `SFN_TASK_TOKEN` 是否存在
   - 檢查 `SKIP_PACKAGE` flag
2. 保留 assume-role 邏輯（在 package 前執行）
3. 調用 `sam_deploy.py` 時傳遞環境變數

**注意：** 此階段只修改 buildspec，`sam_deploy.py` 的實作在 S35-001b。

## Phase 5: 本地驗證

### 5.1 YAML 驗證

```bash
cfn-lint deployer/template.yaml --non-zero-exit-code error
```

### 5.2 Template 打包測試

```bash
cd deployer
sam build --template-file template.yaml
sam validate
```

## Phase 6: 測試環境部署

### 6.1 部署到測試環境

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

### 6.2 驗證 SFN Definition

```bash
aws stepfunctions describe-state-machine \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name bouncer-deployer-test \
    --query 'Stacks[0].Outputs[?OutputKey==`DeployStateMachine`].OutputValue' \
    --output text) \
  --query 'definition' \
  --output text | jq .
```

檢查：
- `WaitForPackage` state 存在
- `AnalyzeChangeset` state 存在
- `CheckChangesetResult` state 存在
- `WaitForInfraApproval` state 存在
- `SamDeploy` state 存在

### 6.3 測試 SFN Execution（手動觸發）

```bash
aws stepfunctions start-execution \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name bouncer-deployer-test \
    --query 'Stacks[0].Outputs[?OutputKey==`DeployStateMachine`].OutputValue' \
    --output text) \
  --input '{
    "deploy_id": "test-s35-001a",
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

**預期行為：**
- `StartBuild` 執行成功
- `WaitForPackage` 進入等待狀態（因為 sam_deploy.py 尚未實作 callback，會 timeout）

## Phase 7: Integration Test

### 7.1 建立測試檔案

**檔案：** `tests/test_s35_001a_sfn_flow.py`

```python
import pytest
import boto3
from moto import mock_aws

@mock_aws
def test_sfn_definition_has_new_states():
    """驗證 SFN definition 包含新 states"""
    # Load template.yaml and parse SFN definition
    # Assert WaitForPackage, AnalyzeChangeset, CheckChangesetResult, WaitForInfraApproval, SamDeploy exist
    pass

def test_sfn_iam_permissions():
    """驗證 IAM 權限正確配置"""
    # Load template.yaml
    # Assert NotifierLambdaRole has states:SendTaskSuccess permission
    pass
```

### 7.2 執行測試

```bash
pytest tests/test_s35_001a_sfn_flow.py -v
```

## Phase 8: 文檔更新

### 8.1 更新 deployer/README.md

加入新 SFN flow 說明：

```markdown
## Deployment Flow (Sprint 35)

1. NotifyStart: 發送部署開始通知
2. StartBuild: CodeBuild 執行 sam build + sam package
3. WaitForPackage: 等待 CodeBuild 回呼 taskToken
4. AnalyzeChangeset: Lambda 分析 changeset
5. CheckChangesetResult: 判斷是否為 code-only
   - code-only: 跳到 SamDeploy
   - infra change: 進入 WaitForInfraApproval
6. WaitForInfraApproval: 等待人工審批
7. SamDeploy: CodeBuild 執行 sam deploy
8. NotifySuccess/NotifyFailure: 發送最終通知
```

## Phase 9: Code Review & Merge

### 9.1 建立 PR

```bash
git add deployer/template.yaml tests/test_s35_001a_sfn_flow.py deployer/README.md
git commit -m "feat(deployer): S35-001a - SFN flow changes for post-package changeset analysis"
git push origin s35-001a-sfn-flow-changes
```

### 9.2 PR Checklist

- [ ] cfn-lint 通過
- [ ] sam validate 通過
- [ ] 測試環境部署成功
- [ ] SFN definition 驗證通過
- [ ] IAM 權限驗證通過
- [ ] Integration test 通過
- [ ] deployer/README.md 已更新
- [ ] 無 breaking changes

## Phase 10: Production 部署

### 10.1 Production 部署

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

### 10.2 監控 CloudWatch Logs

```bash
aws logs tail /aws/lambda/bouncer-deployer-notifier --follow
```

### 10.3 驗證 Production SFN

觸發一次測試 deploy（非 critical project），驗證：
- StartBuild → WaitForPackage 轉換正常
- WaitForPackage 進入等待狀態（預期 timeout，因 sam_deploy.py 尚未實作）

## 回滾計畫

如果 Production 部署失敗：

```bash
# 回滾到舊版 template.yaml
cp deployer/template.yaml.backup-s35-001a deployer/template.yaml

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

1. **SFN state machine 更新是 in-place：** 舊的 running executions 不受影響
2. **IAM 權限需要時間生效：** 部署後等待 1-2 分鐘再測試
3. **WaitForPackage timeout 是預期行為：** 在 S35-001b 完成前，SFN 會在此 state timeout
4. **不影響現有 deploy：** 舊版 bouncer_deploy MCP tool 仍可正常運作（只是沒有 auto-approve）
