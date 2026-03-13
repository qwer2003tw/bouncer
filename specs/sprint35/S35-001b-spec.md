# S35-001b: sam_deploy.py TaskToken Callback

## 概述

修改 `deployer/scripts/sam_deploy.py`，在 `sam package` 完成後回呼 Step Functions taskToken，將 `template_s3_url` 傳遞給 SFN，讓後續的 AnalyzeChangeset state 能使用正確的新 template 進行 changeset 分析。

## 背景

當前流程：
- `sam_deploy.py` 執行 `sam build` → `sam package` → `sam deploy` 一氣呵成
- `template_s3_url` 在 package 完成後上傳到 S3 並更新到 DDB

問題：
- SFN state machine 無法在 package 和 deploy 之間插入 changeset 分析邏輯
- 無法實現 post-package changeset analysis

## 新架構：Split Package and Deploy

新流程：
1. **Package phase:**
   - `sam build` → `sam package`
   - 上傳 packaged template 到 S3
   - 回呼 SFN taskToken，傳遞 `template_s3_url`
   - 更新 DDB `bouncer-projects` table

2. **Deploy phase:**
   - 使用已 package 的 template 執行 `sam deploy`
   - 跳過 `sam package` step

## 修改項目

### 1. 新增命令列參數

**檔案：** `deployer/scripts/sam_deploy.py`

**新增參數：**

```python
def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    # 新增 flag
    package_only = "--package-only" in argv
    deploy_only = "--deploy-only" in argv

    if package_only and deploy_only:
        print("ERROR: --package-only and --deploy-only are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    # ... existing code ...
```

### 2. 新增 TaskToken Callback 函數

**檔案：** `deployer/scripts/sam_deploy.py` (在 `update_template_s3_url` 之後)

```python
def send_sfn_task_token_callback(
    task_token: str,
    template_s3_url: str,
    artifacts_bucket: str,
    project_id: str,
) -> None:
    """Send Step Functions taskToken callback with package result.

    Called after sam package completes successfully. Sends taskSuccess to SFN
    with the template_s3_url so AnalyzeChangeset state can fetch it.

    Non-fatal: exceptions are caught and printed; deploy continues regardless
    (SFN will timeout and fail, but CodeBuild won't crash).

    Args:
        task_token: SFN taskToken from environment variable SFN_TASK_TOKEN
        template_s3_url: S3 URL of packaged template
        artifacts_bucket: S3 bucket name
        project_id: Project ID
    """
    if not task_token:
        print("[SFN] No SFN_TASK_TOKEN, skipping callback")
        return

    try:
        import boto3 as _boto3
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        sfn = _boto3.client("stepfunctions", region_name=region)

        output = json.dumps({
            "template_s3_url": template_s3_url,
            "artifacts_bucket": artifacts_bucket,
            "project_id": project_id,
            "package_timestamp": int(time.time()),
        })

        sfn.send_task_success(
            taskToken=task_token,
            output=output,
        )
        print(f"[SFN] TaskToken callback sent: {template_s3_url}")
    except Exception as exc:
        # Non-fatal: don't break deploy, but SFN will timeout
        print(f"[SFN] Warning: failed to send taskToken callback: {exc}")
```

### 3. 修改 main() 函數流程

**檔案：** `deployer/scripts/sam_deploy.py` (Lines 603-716)

**變更：**

```python
def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    package_only = "--package-only" in argv
    deploy_only = "--deploy-only" in argv

    if package_only and deploy_only:
        print("ERROR: --package-only and --deploy-only are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    _check_github_pat()

    stack = os.environ.get("STACK_NAME", "").strip()
    _validate_stack_name(stack)

    params_raw = os.environ.get("SAM_PARAMS", "").strip()
    cfn_role = os.environ.get("CFN_ROLE_ARN", "").strip()
    target_role = os.environ.get("TARGET_ROLE_ARN", "").strip()
    artifacts_bucket = os.environ.get("ARTIFACTS_BUCKET", "").strip()
    project_id = os.environ.get("PROJECT_ID", "").strip()
    sfn_task_token = os.environ.get("SFN_TASK_TOKEN", "").strip()

    # --- Package phase (skip if deploy-only) ---
    if not deploy_only:
        _run_sam_package(artifacts_bucket, project_id)
        update_template_s3_url(project_id, artifacts_bucket)

        # Send SFN taskToken callback (if provided)
        if sfn_task_token:
            template_s3_url = f"https://{artifacts_bucket}.s3.amazonaws.com/{project_id}/packaged-template.yaml"
            send_sfn_task_token_callback(
                task_token=sfn_task_token,
                template_s3_url=template_s3_url,
                artifacts_bucket=artifacts_bucket,
                project_id=project_id,
            )

    # Exit early if package-only
    if package_only:
        print("[package-only] Exiting after package phase")
        sys.exit(0)

    # --- Deploy phase ---
    cmd = _build_sam_cmd(stack, params_raw, cfn_role, target_role, artifacts_bucket, project_id)
    sys.stdout.flush()

    deploy_result = _run_deploy(cmd)

    if deploy_result.succeeded:
        sys.exit(0)

    # ... rest of existing error handling (import logic) ...
```

### 4. 修改 CodeBuild IAM Role

**檔案：** `deployer/template.yaml` (Lines 270-356)

**新增 Step Functions 權限：**

```yaml
- Sid: StepFunctionsCallback
  Effect: Allow
  Action:
    - states:SendTaskSuccess
    - states:SendTaskFailure
  Resource: !Ref DeployStateMachine
```

## TCS 評估

**D1 Files (檔案數量):** 1 (sam_deploy.py) = **1/5**

**D2 Cross-module (跨模組):** 跨 CodeBuild + Step Functions = **3/4**

**D3 Testing (測試需求):** 新增 unit test (mock boto3 sfn client) = **2/4**

**D4 Infra (基礎建設):** IAM 權限變更 = **2/4**

**D5 External (外部 API):** Step Functions send_task_success API = **2/4**

**Total TCS:** 1 + 3 + 2 + 2 + 2 = **10** (Medium)

## 測試策略

### 1. Unit Tests

**檔案：** `tests/test_sam_deploy_tasktoken.py`

```python
import pytest
from unittest.mock import patch, MagicMock
from deployer.scripts import sam_deploy

def test_send_sfn_task_token_callback_success():
    """TaskToken callback 成功"""
    with patch('boto3.client') as mock_boto:
        mock_sfn = MagicMock()
        mock_boto.return_value = mock_sfn

        sam_deploy.send_sfn_task_token_callback(
            task_token="test-token",
            template_s3_url="https://bucket.s3.amazonaws.com/project/template.yaml",
            artifacts_bucket="bucket",
            project_id="project",
        )

        mock_sfn.send_task_success.assert_called_once()
        args = mock_sfn.send_task_success.call_args
        assert args[1]['taskToken'] == "test-token"
        assert "template_s3_url" in args[1]['output']

def test_send_sfn_task_token_callback_no_token():
    """無 taskToken 時跳過"""
    with patch('boto3.client') as mock_boto:
        mock_sfn = MagicMock()
        mock_boto.return_value = mock_sfn

        sam_deploy.send_sfn_task_token_callback(
            task_token="",
            template_s3_url="https://bucket.s3.amazonaws.com/project/template.yaml",
            artifacts_bucket="bucket",
            project_id="project",
        )

        mock_sfn.send_task_success.assert_not_called()

def test_send_sfn_task_token_callback_exception():
    """Exception 不中斷流程"""
    with patch('boto3.client') as mock_boto:
        mock_sfn = MagicMock()
        mock_sfn.send_task_success.side_effect = Exception("SFN API error")
        mock_boto.return_value = mock_sfn

        # Should not raise
        sam_deploy.send_sfn_task_token_callback(
            task_token="test-token",
            template_s3_url="https://bucket.s3.amazonaws.com/project/template.yaml",
            artifacts_bucket="bucket",
            project_id="project",
        )

def test_main_package_only_exits_early():
    """--package-only 只執行 package phase"""
    with patch('deployer.scripts.sam_deploy._run_sam_package') as mock_package, \
         patch('deployer.scripts.sam_deploy.update_template_s3_url'), \
         patch('deployer.scripts.sam_deploy.send_sfn_task_token_callback'), \
         patch('deployer.scripts.sam_deploy._build_sam_cmd') as mock_build, \
         patch.dict(os.environ, {
             'STACK_NAME': 'test-stack',
             'ARTIFACTS_BUCKET': 'test-bucket',
             'PROJECT_ID': 'test-project',
         }):

        with pytest.raises(SystemExit) as exc:
            sam_deploy.main(['--package-only'])

        assert exc.value.code == 0
        mock_package.assert_called_once()
        mock_build.assert_not_called()  # deploy phase not reached

def test_main_deploy_only_skips_package():
    """--deploy-only 跳過 package phase"""
    with patch('deployer.scripts.sam_deploy._run_sam_package') as mock_package, \
         patch('deployer.scripts.sam_deploy._build_sam_cmd') as mock_build, \
         patch('deployer.scripts.sam_deploy._run_deploy') as mock_deploy, \
         patch.dict(os.environ, {
             'STACK_NAME': 'test-stack',
             'ARTIFACTS_BUCKET': 'test-bucket',
             'PROJECT_ID': 'test-project',
         }):

        mock_deploy.return_value = MagicMock(succeeded=True)

        with pytest.raises(SystemExit) as exc:
            sam_deploy.main(['--deploy-only'])

        assert exc.value.code == 0
        mock_package.assert_not_called()
        mock_build.assert_called_once()
```

### 2. Integration Tests

在測試環境觸發 SFN execution，驗證：
1. CodeBuild 執行 `sam_deploy.py --package-only`
2. SFN WaitForPackage state 收到 taskToken callback
3. SFN 進入 AnalyzeChangeset state
4. CodeBuild 執行 `sam_deploy.py --deploy-only`（在第二個 StartBuild call）

### 3. Manual Tests

1. 測試 SFN_TASK_TOKEN 不存在時（向後相容）
2. 測試 taskToken callback 失敗時（SFN timeout）
3. 測試 package 和 deploy 分離流程

## 安全考量

- **taskToken 不持久化：** 只在內存中傳遞，不寫入日誌或 DDB
- **IAM 權限最小化：** CodeBuild 只能呼叫 Step Functions send_task_success，不能啟動新 execution
- **Exception handling：** taskToken callback 失敗不中斷 CodeBuild（但 SFN 會 timeout）

## 成本考量

- **Step Functions API：** `send_task_success` 呼叫免費（不計入 state transitions）
- **CodeBuild：** 無額外 build time（package 和 deploy 時間總和與原流程相同）
- **S3：** 無額外 storage（packaged template 已存在）

## 部署步驟

1. **上傳 sam_deploy.py 到 S3：**
   ```bash
   make -C deployer upload-deploy-script
   ```

2. **驗證 IAM 權限：** 確認 CodeBuildRole 有 `states:SendTaskSuccess` 權限

3. **測試環境驗證：**
   - 觸發 SFN execution
   - 監控 CloudWatch Logs（CodeBuild）
   - 驗證 taskToken callback 成功

4. **Production 部署：**
   - 上傳新版 `sam_deploy.py` 到 S3
   - 觸發測試 deploy，驗證流程正常

## 回滾計畫

如果 taskToken callback 邏輯失敗：

```bash
# 回滾 sam_deploy.py 到舊版
git checkout HEAD~1 deployer/scripts/sam_deploy.py

# 重新上傳到 S3
make -C deployer upload-deploy-script
```

**注意：** 回滾不影響已運行的 deploy，因為 taskToken callback 失敗是 non-fatal（SFN timeout 但 CodeBuild 繼續）。

## 向後相容性

- **無 SFN_TASK_TOKEN 環境變數時：** 跳過 callback，行為與舊版相同
- **無 --package-only / --deploy-only flag 時：** 執行完整流程（package + deploy）
- **舊版 SFN definition：** 仍可正常運作（不會進入 WaitForPackage state）

## 相依性

- **前置：** S35-001a (SFN flow changes)
- **後續：** S35-001c (notifier/app.py handle_analyze)
