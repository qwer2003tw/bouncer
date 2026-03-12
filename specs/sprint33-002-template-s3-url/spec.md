# Sprint 33-002: feat: sam package explicit + DDB template_s3_url update + IAM

> GitHub Issues: #120 (feat) + #120 (IAM)
> Tasks covered: sprint33-002 + sprint33-003
> Priority: P1
> Sprint: 33

---

## Background

`template_s3_url` 是 `bouncer-projects` DDB table 的欄位，用於 Sprint 32 的 `auto_approve_deploy` changeset 分析流程（`changeset_analyzer.py` 使用此 URL 建立 dry-run changeset）。

**目前問題**：
1. `sam_deploy.py` 使用 `--resolve-s3` flag（隱式 package），SAM CLI 自動把 artifacts 上傳到它自管的 bucket，但不輸出 packaged template 的 S3 URL
2. `template_s3_url` 目前需要手動在 `add_project` / `update_project` 時填入，每次部署 template 變更時不會自動更新
3. `CodeBuildRole` 的 IAM policy 沒有 `dynamodb:UpdateItem` on `ProjectsTable`，即使 sam_deploy.py 想更新也無法執行

**目標**：
- `sam_deploy.py` 改為先執行 `sam package`（explicit），輸出 packaged template 到 `ARTIFACTS_BUCKET/templates/{STACK_NAME}-packaged.yaml`
- 從 packaged template S3 URL 更新 ProjectsTable 的 `template_s3_url` 欄位
- `CodeBuildRole` 新增 `dynamodb:UpdateItem` on `ProjectsTable` ARN

---

## User Stories

**US-1: 自動維護 template_s3_url**
As a Bouncer maintainer,
I want the deploy pipeline to automatically update `template_s3_url` after each deploy,
So that the changeset analyzer always uses the latest packaged template.

**US-2: Explicit sam package**
As a Bouncer developer,
I want `sam_deploy.py` to run `sam package` explicitly before `sam deploy`,
So that I can control where the packaged template is stored and extract its S3 URL.

**US-3: IAM 授權完整**
As a Bouncer infra engineer,
I want `CodeBuildRole` to have `dynamodb:UpdateItem` on `ProjectsTable`,
So that the deploy script can update `template_s3_url` without IAM errors.

**US-4: Fail-safe**
As a Bouncer operator,
I want the DDB update failure to be non-blocking (best-effort),
So that a DDB error doesn't cause a deploy to be reported as failed.

---

## Acceptance Scenarios

### S1: sam package 顯式執行，template 上傳到 ARTIFACTS_BUCKET
```
Given  sam build 已完成
When   sam_deploy.py 執行
Then   sam package 被呼叫，--output-template-file 指向 /tmp/{stack_name}-packaged.yaml
And    --s3-bucket 使用 ARTIFACTS_BUCKET env var
And    --s3-prefix 為 "templates"
And    packaged template 被上傳到 s3://{ARTIFACTS_BUCKET}/templates/{stack_name}-packaged.yaml
```

### S2: sam deploy 使用 packaged template，不帶 --resolve-s3
```
Given  sam package 完成並產出 packaged template
When   sam deploy 執行
Then   --template-file /tmp/{stack_name}-packaged.yaml 被傳入
And    --resolve-s3 flag 不存在於 sam deploy 命令
```

### S3: 部署成功後 ProjectsTable 更新 template_s3_url
```
Given  sam deploy 成功
And    packaged template S3 URL 為 s3://{bucket}/templates/{stack_name}-packaged.yaml
When   主流程執行
Then   boto3 update_item 被呼叫，ProjectsTable key={project_id}
And    template_s3_url = "https://{bucket}.s3.{region}.amazonaws.com/templates/{stack_name}-packaged.yaml"
And    更新失敗時，log error 但 sys.exit(0)（部署仍視為成功）
```

### S4: DDB 更新失敗 → 部署不受影響
```
Given  sam deploy 成功
And    boto3 update_item 拋出 ClientError（e.g. AccessDenied 或 ResourceNotFoundException）
When   DDB update 執行
Then   錯誤被捕捉，log error 包含 deploy_id 和 project_id
And    sys.exit(0)（不因 DDB 失敗而 fail 整個 deploy）
```

### S5: ARTIFACTS_BUCKET 未設定 → 跳過 package，fallback 原有 --resolve-s3
```
Given  ARTIFACTS_BUCKET 環境變數未設定（空字串）
When   sam_deploy.py 執行
Then   回退到原有的 --resolve-s3 模式
And    DDB update 不執行
And   警告 log 輸出
```

### S6: PROJECT_ID 未設定 → 跳過 DDB update
```
Given  ARTIFACTS_BUCKET 已設定但 PROJECT_ID 未設定
When   sam deploy 成功後
Then   packaged template 仍上傳到 ARTIFACTS_BUCKET
And    DDB update 被跳過（不嘗試更新，log info）
```

### S7: CodeBuildRole IAM 允許 DDB UpdateItem
```
Given  CodeBuild 執行 sam_deploy.py
When   boto3 dynamodb.update_item on ProjectsTable 被呼叫
Then   IAM 允許（不拋出 AccessDeniedException）
```

---

## Edge Cases

- **sam package 失敗**：應視同部署失敗，non-zero exit，不繼續 sam deploy
- **packaged template 路徑含特殊字元**：STACK_NAME 已有 validation（`_validate_stack_name`），路徑安全
- **CodeBuild cross-account 模式**：assume-role 後，boto3 client 使用 temporary credentials → ProjectsTable 在主帳號，cross-account update 需要原始 credentials（assume-role 前的 CodeBuild role）— **重要邊界條件**，DDB update 必須在 assume-role 之前或使用 CodeBuild 原始 role
- **S3 URL 格式**：packaged template URL 需為 HTTPS 格式（CloudFormation CreateChangeSet 需要 HTTPS URL，不接受 s3:// URI）

---

## Requirements

### Functional

| # | Requirement |
|---|-------------|
| F1 | `sam_deploy.py`：在 `sam build` 後、`sam deploy` 前，執行 `sam package --template-file template.yaml --output-template-file /tmp/{stack}-packaged.yaml --s3-bucket {ARTIFACTS_BUCKET} --s3-prefix templates` |
| F2 | `_build_sam_cmd()` 或新的 `_build_deploy_cmd()`：移除 `--resolve-s3`，改用 `--template-file /tmp/{stack}-packaged.yaml` |
| F3 | 部署成功後，用 boto3 更新 ProjectsTable `template_s3_url`（HTTPS URL 格式） |
| F4 | DDB update 為 best-effort（失敗不 exit non-zero） |
| F5 | ARTIFACTS_BUCKET 未設定時 fallback 到 `--resolve-s3`（不破壞現有行為） |
| F6 | PROJECT_ID 未設定時跳過 DDB update（不報錯） |
| F7 | `deployer/template.yaml`：CodeBuildRole 新增 `dynamodb:UpdateItem` on `!GetAtt ProjectsTable.Arn` |
| F8 | CodeBuild buildspec 需傳遞 PROJECT_ID 環境變數（從 Step Function input `$.project_id`） |

### Non-functional

| # | Requirement |
|---|-------------|
| N1 | sam package 失敗 → sys.exit(non-zero)，不繼續 deploy |
| N2 | 所有新路徑有對應單元測試（mock subprocess + mock boto3） |
| N3 | DDB update 使用 `boto3.client('dynamodb', region_name=AWS_DEFAULT_REGION)` — 注意 cross-account 模式下需在 assume-role 前執行 |
| N4 | IAM 變更需要 deployer stack 重新部署（CodeBuildRole 修改） |
