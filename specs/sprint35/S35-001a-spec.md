# S35-001a: SFN Flow Changes for Post-Package Changeset Analysis

## 概述

修改 `deployer/template.yaml` 中的 Step Functions state machine，在 CodeBuild package 完成後加入 waitForTaskToken 機制，以便在正確的時機（新 template 已產出）進行 changeset 分析。

## 背景

當前流程：
```
NotifyStart → StartBuild (CodeBuild) → NotifySuccess/NotifyFailure
```

問題：`bouncer_deploy` MCP tool 在請求時同步做 changeset 分析，用的是舊版 `template_s3_url`，CFN 永遠回報 "No updates"，auto-approve 無法觸發。

## 正確架構：Post-Package Changeset Analysis

新流程：
```
NotifyStart
  → StartBuild (CodeBuild: git clone → sam build → sam package → 回呼 taskToken)
  → WaitForPackage (waitForTaskToken，等待 CodeBuild 回呼)
  → AnalyzeChangeset (Lambda Task，呼叫 NotifierLambda)
    → code-only → 繼續
    → infra change → WaitForInfraApproval (waitForTaskToken，等人工)
  → SamDeploy (CodeBuild)
  → NotifySuccess/NotifyFailure
```

## 修改項目

### 1. Step Functions Definition

**檔案：** `deployer/template.yaml` (Lines 604-686)

**變更：**

在 `StartBuild` state 之後插入新的 states：

```yaml
StartBuild:
  Type: Task
  Resource: "arn:aws:states:::codebuild:startBuild"
  Parameters:
    ProjectName: sam-deployer
    EnvironmentVariablesOverride:
      # ... existing vars ...
      - Name: SFN_TASK_TOKEN
        Type: PLAINTEXT
        Value.$: "$$.Task.Token"  # 注入 taskToken 給 CodeBuild
  ResultPath: $.package_result
  Next: WaitForPackage
  Catch:
    - ErrorEquals: ["States.ALL"]
      ResultPath: $.error
      Next: NotifyFailure

WaitForPackage:
  Type: Task
  Resource: "arn:aws:states:::lambda:invoke.waitForTaskToken"
  Parameters:
    FunctionName: !GetAtt NotifierLambda.Arn
    Payload:
      action: wait_package
      deploy_id.$: $.deploy_id
      task_token.$: $$.Task.Token
  TimeoutSeconds: 1800  # 30 min (CodeBuild max)
  HeartbeatSeconds: 60
  ResultPath: $.package_callback
  Next: AnalyzeChangeset
  Catch:
    - ErrorEquals: ["States.TaskTimedOut", "States.HeartbeatTimeout"]
      ResultPath: $.error
      Next: NotifyFailure

AnalyzeChangeset:
  Type: Task
  Resource: !GetAtt NotifierLambda.Arn
  Parameters:
    action: analyze
    deploy_id.$: $.deploy_id
    project_id.$: $.project_id
    template_s3_url.$: $.package_callback.template_s3_url
    stack_name.$: $.stack_name
    task_token.$: $$.Task.Token
  ResultPath: $.analysis_result
  Next: CheckChangesetResult
  Catch:
    - ErrorEquals: ["States.ALL"]
      ResultPath: $.error
      Next: NotifyFailure

CheckChangesetResult:
  Type: Choice
  Choices:
    - Variable: $.analysis_result.is_code_only
      BooleanEquals: true
      Next: SamDeploy
  Default: WaitForInfraApproval

WaitForInfraApproval:
  Type: Task
  Resource: "arn:aws:states:::lambda:invoke.waitForTaskToken"
  Parameters:
    FunctionName: !GetAtt NotifierLambda.Arn
    Payload:
      action: wait_infra_approval
      deploy_id.$: $.deploy_id
      project_id.$: $.project_id
      task_token.$: $$.Task.Token
  TimeoutSeconds: 300  # 5 min approval window
  ResultPath: $.infra_approval
  Next: SamDeploy
  Catch:
    - ErrorEquals: ["States.TaskTimedOut"]
      ResultPath: $.error
      Next: NotifyFailure

SamDeploy:
  Type: Task
  Resource: "arn:aws:states:::codebuild:startBuild.sync"
  Parameters:
    ProjectName: sam-deployer
    EnvironmentVariablesOverride:
      # ... existing vars ...
      - Name: SKIP_PACKAGE
        Type: PLAINTEXT
        Value: "true"  # package 已完成，只執行 deploy
  ResultPath: $.build_result
  Next: NotifySuccess
  Catch:
    - ErrorEquals: ["States.ALL"]
      ResultPath: $.error
      Next: NotifyFailure
```

### 2. IAM 權限更新

**NotifierLambdaRole** 新增 Step Functions callback 權限：

```yaml
- Sid: StepFunctionsCallback
  Effect: Allow
  Action:
    - states:SendTaskSuccess
    - states:SendTaskFailure
    - states:SendTaskHeartbeat
  Resource: !Ref DeployStateMachine
```

**CodeBuildRole** 新增（已有 DynamoDB 權限，只需確認）：

```yaml
# 已存在，無需新增
- Sid: ProjectsTableUpdateDirect
  Effect: Allow
  Action:
    - dynamodb:UpdateItem
    - dynamodb:GetItem
  Resource: "arn:aws:dynamodb:us-east-1:190825685292:table/bouncer-projects"
```

**StepFunctionsRole** 更新 Lambda invoke 權限（確保可呼叫 waitForTaskToken）：

```yaml
- Sid: Lambda
  Effect: Allow
  Action:
    - lambda:InvokeFunction
  Resource:
    - !GetAtt NotifierLambda.Arn
```

### 3. CodeBuild BuildSpec 變更

**檔案：** `deployer/template.yaml` (Lines 515-561)

**變更：**

在 `build` phase 加入環境變數注入和 heartbeat 邏輯：

```yaml
build:
  commands:
    - export WORK_DIR=$(pwd)/repo/${SAM_TEMPLATE_PATH}
    - echo "Building SAM application in $WORK_DIR..."
    - cd $WORK_DIR && sam build --use-container
    - |
      # Package phase (sam_deploy.py will send taskToken callback)
      cd $WORK_DIR
      aws s3 cp "s3://${ARTIFACTS_BUCKET}/deployer-scripts/sam_deploy.py" /tmp/sam_deploy.py

      # Assume role if needed (before package, for S3 access)
      TRIMMED_ROLE_ARN=$(echo "$TARGET_ROLE_ARN" | tr -d '[:space:]' | tr -d '"')
      if [[ "$TRIMMED_ROLE_ARN" == arn:* ]]; then
        echo "Assuming role $TRIMMED_ROLE_ARN for package..."
        CREDS=$(aws sts assume-role --role-arn "$TRIMMED_ROLE_ARN" --role-session-name "bouncer-package-${DEPLOY_ID}" --output json)
        export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['AccessKeyId'])")
        export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SecretAccessKey'])")
        export AWS_SESSION_TOKEN=$(echo "$CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SessionToken'])")
      fi

      # Package only (no deploy yet)
      python3 /tmp/sam_deploy.py --package-only

      # If SKIP_PACKAGE=true, skip above and run deploy-only
      if [[ "$SKIP_PACKAGE" == "true" ]]; then
        python3 /tmp/sam_deploy.py --deploy-only
      fi
```

## TCS 評估

**D1 Files (檔案數量):** 1 (template.yaml) = **2/5**

**D2 Cross-module (跨模組):** 跨 SFN + NotifierLambda + CodeBuild = **4/4**

**D3 Testing (測試需求):** 新增 integration test (mock SFN execution) = **2/4**

**D4 Infra (基礎建設):** CloudFormation 架構變更 (SFN definition + IAM) = **4/4**

**D5 External (外部 API):** Step Functions API (waitForTaskToken) = **1/4**

**Total TCS:** 2 + 4 + 2 + 4 + 1 = **13** (borderline Complex)

**Note:** 原預估 TCS=9，實際為 13 是因為 SFN definition 變更的複雜度被低估（需要 5 個新 states + Choice logic）。但因為不涉及新增外部服務，仍屬 Medium 複雜度的高端。

## 測試策略

1. **Unit Tests:**
   - Mock SFN execution，驗證 state 轉換邏輯
   - 驗證 taskToken 參數正確傳遞

2. **Integration Tests:**
   - 部署 template.yaml 到測試環境
   - 觸發 SFN execution，驗證 WaitForPackage → AnalyzeChangeset 流程
   - 驗證 code-only 變更自動跳過 WaitForInfraApproval
   - 驗證 infra 變更進入 WaitForInfraApproval

3. **Manual Tests:**
   - 測試 timeout 場景（CodeBuild 超時）
   - 測試 heartbeat 機制

## 安全考量

- **taskToken 安全性：** taskToken 只在 SFN runtime 有效，不持久化，不暴露給外部
- **IAM 權限最小化：** NotifierLambda 只能呼叫 Step Functions callback API，不能啟動新 execution
- **Timeout 設定：** WaitForPackage 30 min（CodeBuild max），WaitForInfraApproval 5 min（審批 timeout）

## 成本考量

- **Step Functions:** 每次狀態轉換計費 $0.000025，新增 5 個 states = 每次 deploy $0.000125 (極低)
- **Lambda:** NotifierLambda 已存在，只是新增 action handler，無額外費用
- **CodeBuild:** 無額外 build time（package 和 deploy 分開但總時長不變）

## 部署步驟

1. **更新 template.yaml**
2. **部署 deployer stack：** `sam deploy --template-file deployer/template.yaml ...`
3. **驗證 IAM 權限：** 檢查 NotifierLambdaRole 和 StepFunctionsRole
4. **測試 SFN execution：** 觸發測試 deploy，驗證新 states 正常運作
5. **監控 CloudWatch Logs：** 確認 taskToken callback 成功

## 回滾計畫

如果新 SFN flow 失敗：
1. **立即：** 回滾 template.yaml 到舊版（移除新 states）
2. **DDB：** bouncer-deploy-history 中舊 deploy 記錄不受影響
3. **無資料遺失：** SFN state 是 stateless，回滾無副作用

## 相依性

- **前置：** 無（獨立 task）
- **後續：**
  - S35-001b (sam_deploy.py taskToken callback)
  - S35-001c (notifier/app.py handle_analyze)
