# S35-001c: Notifier Lambda handle_analyze

## 概述

在 `deployer/notifier/app.py` 中新增 `handle_analyze()` action handler，接收 SFN AnalyzeChangeset state 的請求，使用 `src/changeset_analyzer.py` 進行 changeset 分析，並根據結果決定：
- **code-only 變更：** 呼叫 `sfn.send_task_success`，讓 SFN 繼續執行 SamDeploy
- **infra 變更：** 發送 Telegram 通知給 Steven，進入 WaitForInfraApproval state

## 背景

當前流程（S35-001a + S35-001b 完成後）：
```
StartBuild → WaitForPackage（taskToken callback）→ AnalyzeChangeset（待實作）
```

問題：
- AnalyzeChangeset state 需要 Lambda handler 處理 `action: analyze` 請求
- Changeset 分析邏輯已存在（`src/changeset_analyzer.py`），但 notifier Lambda 尚未整合

## 新架構：Changeset Analysis in Notifier Lambda

新流程：
1. **AnalyzeChangeset state** 呼叫 NotifierLambda，action = `analyze`
2. **NotifierLambda** 呼叫 `create_dry_run_changeset()` 和 `analyze_changeset()`
3. **分析結果：**
   - **code-only:** 呼叫 `sfn.send_task_success(output={"is_code_only": true})`
   - **infra change:** 發送 Telegram 通知 + 進入 WaitForInfraApproval（手動審批）

## 修改項目

### 1. 新增 handle_analyze() 函數

**檔案：** `deployer/notifier/app.py` (在 `lambda_handler` 之後)

```python
def handle_analyze(event):
    """Analyze CloudFormation changeset after package phase.

    Called by Step Functions AnalyzeChangeset state.

    Args:
        event: {
            "action": "analyze",
            "deploy_id": "...",
            "project_id": "...",
            "template_s3_url": "...",
            "stack_name": "...",
            "task_token": "..."  # Optional, for waitForTaskToken
        }

    Returns:
        {
            "is_code_only": bool,
            "change_count": int,
            "changeset_name": str
        }
    """
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    template_s3_url = event.get('template_s3_url', '')
    stack_name = event.get('stack_name', '')
    task_token = event.get('task_token', '')

    if not all([deploy_id, project_id, template_s3_url, stack_name]):
        error_msg = "Missing required parameters for analyze action"
        print(f"[analyze] ERROR: {error_msg}")
        if task_token:
            _send_task_failure(task_token, error_msg)
        return {'error': error_msg}

    # Import changeset_analyzer (assume it's deployed with notifier Lambda)
    try:
        import sys
        import os
        # Add parent directory to path (for local imports)
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from changeset_analyzer import create_dry_run_changeset, analyze_changeset, cleanup_changeset
        import boto3
    except ImportError as e:
        error_msg = f"Failed to import changeset_analyzer: {e}"
        print(f"[analyze] ERROR: {error_msg}")
        if task_token:
            _send_task_failure(task_token, error_msg)
        return {'error': error_msg}

    try:
        # Create CloudFormation client
        cfn = boto3.client('cloudformation')

        # Create dry-run changeset
        print(f"[analyze] Creating dry-run changeset for {stack_name}...")
        changeset_name = create_dry_run_changeset(
            cfn_client=cfn,
            stack_name=stack_name,
            template_s3_url=template_s3_url,
        )

        # Analyze changeset
        print(f"[analyze] Analyzing changeset {changeset_name}...")
        result = analyze_changeset(
            cfn_client=cfn,
            stack_name=stack_name,
            changeset_name=changeset_name,
            max_wait=60,
        )

        # Cleanup changeset (best-effort)
        cleanup_changeset(cfn, stack_name, changeset_name)

        # Prepare output
        output = {
            'is_code_only': result.is_code_only,
            'change_count': len(result.resource_changes),
            'changeset_name': changeset_name,
        }

        print(f"[analyze] Result: is_code_only={result.is_code_only}, changes={len(result.resource_changes)}")

        # Send Telegram notification (for infra changes)
        if not result.is_code_only:
            _send_infra_change_notification(
                deploy_id=deploy_id,
                project_id=project_id,
                change_count=len(result.resource_changes),
                changeset_name=changeset_name,
            )
            # Do NOT send task_success yet — wait for manual approval

        return output

    except Exception as e:
        error_msg = f"Changeset analysis failed: {str(e)}"
        print(f"[analyze] ERROR: {error_msg}")
        if task_token:
            _send_task_failure(task_token, error_msg)
        return {'error': error_msg}


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

    # Use existing Telegram helper
    try:
        send_telegram_message(text)
    except Exception as e:
        print(f"[Telegram] Failed to send notification: {e}")
```

### 2. 修改 lambda_handler() 函數

**檔案：** `deployer/notifier/app.py` (Lines 25-41)

**變更：**

```python
def lambda_handler(event, context):
    """處理通知請求"""
    action = event.get('action', '')
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')

    if action == 'start':
        return handle_start(event)
    elif action == 'progress':
        return handle_progress(event)
    elif action == 'success':
        return handle_success(event)
    elif action == 'failure':
        return handle_failure(event)
    elif action == 'analyze':  # 新增
        return handle_analyze(event)
    else:
        return {'error': f'Unknown action: {action}'}
```

### 3. 部署 changeset_analyzer.py 到 Notifier Lambda

**選項 A：** 在 `deployer/template.yaml` 的 NotifierLambda 中加入 Layer

**選項 B：** 將 `src/changeset_analyzer.py` 複製到 `deployer/notifier/` 目錄

**推薦：選項 B**（簡單，不需要建立 Lambda Layer）

**步驟：**

```bash
cp src/changeset_analyzer.py deployer/notifier/changeset_analyzer.py
```

**修改 import：**

```python
# deployer/notifier/app.py
from changeset_analyzer import create_dry_run_changeset, analyze_changeset, cleanup_changeset
```

### 4. 更新 NotifierLambda IAM 權限

**檔案：** `deployer/template.yaml` (Lines 360-402)

**新增 CloudFormation 權限：**

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

**新增 S3 權限（讀取 template）：**

```yaml
- Sid: S3TemplateRead
  Effect: Allow
  Action:
    - s3:GetObject
  Resource:
    - !Sub "${ArtifactsBucket.Arn}/*"
```

**已存在的 Step Functions callback 權限（S35-001a 已加）：**

```yaml
- Sid: StepFunctionsCallback
  Effect: Allow
  Action:
    - states:SendTaskSuccess
    - states:SendTaskFailure
    - states:SendTaskHeartbeat
  Resource: !Ref DeployStateMachine
```

### 5. 修改 CheckChangesetResult Choice Logic (Optional)

**檔案：** `deployer/template.yaml` (在 S35-001a 的 CheckChangesetResult state)

**確認 Choice 條件正確：**

```yaml
CheckChangesetResult:
  Type: Choice
  Choices:
    - Variable: $.analysis_result.is_code_only
      BooleanEquals: true
      Next: SamDeploy
  Default: WaitForInfraApproval
```

（S35-001a 已實作，此處只需驗證）

## TCS 評估

**D1 Files (檔案數量):** 2 (notifier/app.py, changeset_analyzer.py copy) = **2/5**

**D2 Cross-module (跨模組):** 跨 notifier Lambda + changeset_analyzer + Step Functions = **3/4**

**D3 Testing (測試需求):** 新增 unit test (mock cfn client + sfn client) = **3/4**

**D4 Infra (基礎建設):** IAM 權限變更 = **2/4**

**D5 External (外部 API):** CloudFormation changeset API + Step Functions API = **2/4**

**Total TCS:** 2 + 3 + 3 + 2 + 2 = **12** (Medium-High)

## 測試策略

### 1. Unit Tests

**檔案：** `tests/test_notifier_analyze.py`

```python
import pytest
from unittest.mock import patch, MagicMock
from deployer.notifier import app

def test_handle_analyze_code_only():
    """code-only 變更，回傳 is_code_only=true"""
    with patch('deployer.notifier.app.create_dry_run_changeset') as mock_create, \
         patch('deployer.notifier.app.analyze_changeset') as mock_analyze, \
         patch('deployer.notifier.app.cleanup_changeset'), \
         patch('boto3.client'):

        mock_create.return_value = "test-changeset-123"
        mock_analyze.return_value = MagicMock(
            is_code_only=True,
            resource_changes=[],
            error=None,
        )

        event = {
            'action': 'analyze',
            'deploy_id': 'test-deploy-1',
            'project_id': 'test-project',
            'template_s3_url': 'https://bucket.s3.amazonaws.com/project/template.yaml',
            'stack_name': 'test-stack',
        }

        result = app.handle_analyze(event)

        assert result['is_code_only'] == True
        assert result['change_count'] == 0
        mock_create.assert_called_once()
        mock_analyze.assert_called_once()

def test_handle_analyze_infra_change():
    """infra 變更，發送 Telegram 通知"""
    with patch('deployer.notifier.app.create_dry_run_changeset') as mock_create, \
         patch('deployer.notifier.app.analyze_changeset') as mock_analyze, \
         patch('deployer.notifier.app.cleanup_changeset'), \
         patch('deployer.notifier.app._send_infra_change_notification') as mock_notify, \
         patch('boto3.client'):

        mock_create.return_value = "test-changeset-456"
        mock_analyze.return_value = MagicMock(
            is_code_only=False,
            resource_changes=[{'ResourceChange': {'Action': 'Modify', 'ResourceType': 'AWS::S3::Bucket'}}],
            error=None,
        )

        event = {
            'action': 'analyze',
            'deploy_id': 'test-deploy-2',
            'project_id': 'test-project',
            'template_s3_url': 'https://bucket.s3.amazonaws.com/project/template.yaml',
            'stack_name': 'test-stack',
        }

        result = app.handle_analyze(event)

        assert result['is_code_only'] == False
        assert result['change_count'] == 1
        mock_notify.assert_called_once()

def test_handle_analyze_missing_params():
    """缺少參數，回傳 error"""
    event = {
        'action': 'analyze',
        'deploy_id': 'test-deploy-3',
        # Missing template_s3_url, stack_name
    }

    result = app.handle_analyze(event)

    assert 'error' in result
    assert 'Missing required parameters' in result['error']
```

### 2. Integration Tests

在測試環境觸發 SFN execution，驗證：
1. AnalyzeChangeset state 成功呼叫 NotifierLambda
2. code-only 變更：SFN 進入 SamDeploy state
3. infra 變更：發送 Telegram 通知 + 進入 WaitForInfraApproval state

### 3. Manual Tests

1. 測試 code-only deploy（只修改 Lambda code）
2. 測試 infra change deploy（修改 DynamoDB table）
3. 測試 changeset 分析失敗場景

## 安全考量

- **Changeset 不執行：** `create_dry_run_changeset` 只建立 changeset，不執行 deploy
- **taskToken 只在 Lambda 內存中：** 不持久化，不寫入日誌
- **IAM 權限最小化：** NotifierLambda 只能建立/讀取 changeset，不能執行 CloudFormation stack 操作
- **Telegram 通知不含敏感資訊：** 只顯示 change_count 和 changeset_name

## 成本考量

- **CloudFormation changeset API：** 免費（`CreateChangeSet`, `DescribeChangeSet`, `DeleteChangeSet`）
- **Lambda：** NotifierLambda 執行時間增加 ~5 秒（changeset 分析），費用極低
- **Telegram API：** 免費

## 部署步驟

1. **複製 changeset_analyzer.py：**
   ```bash
   cp src/changeset_analyzer.py deployer/notifier/changeset_analyzer.py
   ```

2. **修改 deployer/notifier/app.py：** 新增 `handle_analyze()` 和修改 `lambda_handler()`

3. **更新 IAM 權限：** 修改 `deployer/template.yaml` NotifierLambdaRole

4. **部署 deployer stack：**
   ```bash
   sam deploy --template-file deployer/template.yaml ...
   ```

5. **驗證 NotifierLambda 包含 changeset_analyzer.py：**
   ```bash
   aws lambda get-function --function-name bouncer-deployer-notifier \
     --query 'Code.Location' | xargs curl -s | tar -tzf - | grep changeset_analyzer
   ```

## 回滾計畫

如果 `handle_analyze` 邏輯失敗：

```bash
# 回滾 notifier/app.py 到舊版
git checkout HEAD~1 deployer/notifier/app.py

# 移除 changeset_analyzer.py
rm deployer/notifier/changeset_analyzer.py

# 重新部署
sam deploy --template-file deployer/template.yaml ...
```

**注意：** 回滾不影響已運行的 deploy（SFN 會在 AnalyzeChangeset state 失敗，NotifyFailure 處理）。

## 相依性

- **前置：** S35-001a (SFN flow changes), S35-001b (sam_deploy.py taskToken callback)
- **後續：** 無（功能完整）

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
