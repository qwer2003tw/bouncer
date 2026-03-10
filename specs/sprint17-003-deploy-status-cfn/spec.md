# Sprint 17-003: deploy_status 失敗時自動附上 CloudFormation events + 真正錯誤原因

> GitHub Issue: #55
> Priority: P1
> TCS: 7
> Generated: 2026-03-08

---

## Problem Statement

deploy 失敗時，`bouncer_deploy_status` 和 `bouncer_deploy_history` 回傳的錯誤資訊不足以直接診斷問題。`error_message` 存的是 buildspec 命令文字，不是 sam deploy 或 CloudFormation 的實際失敗原因。

### 實際案例（2026-03-03）
deploy-049f41986df8 失敗，Bouncer 回傳：
```
error_message: [BUILD] Error while executing command: cd ...aws s3 cp...
```
實際原因（需查 CloudFormation 才知道）：
```
SharesTable UPDATE_FAILED: Table ztp-files-dev-shares does not exist
```

### 診斷流程問題
每次 debug 需要：
1. 查 Bouncer DDB → `error_message` 無用
2. 查 CloudFormation stack events → 需要 Bouncer 審批
3. 查 CodeBuild logs → 又要審批

三跳審批的 debug 體驗極差。

## Root Cause

1. `DeployErrorExtractor.from_sfn_history()` 只從 Step Functions execution history 抓 error，拿到的是 CodeBuild 的 buildspec 執行錯誤，不是 CloudFormation 層的失敗原因。
2. `get_deploy_status()` 沒有查詢 CloudFormation stack events。

## Scope

### 變更 1: 新增 CloudFormation error extraction

**檔案：** `src/deployer.py`

新增 `DeployErrorExtractor` 方法：

```python
@staticmethod
def from_cfn_events(stack_name: str, region: str = None) -> list[dict]:
    """查詢 CloudFormation stack events，提取 FAILED resources。
    
    Returns:
        List of dicts: [{"resource": "SharesTable", "status": "UPDATE_FAILED", 
                         "reason": "Table does not exist"}]
    """
    cfn = boto3.client('cloudformation', region_name=region)
    try:
        resp = cfn.describe_stack_events(StackName=stack_name)
        events = resp.get('StackEvents', [])
        failed = []
        for e in events[:50]:  # 最近 50 個事件
            status = e.get('ResourceStatus', '')
            if 'FAILED' in status:
                failed.append({
                    'resource': e.get('LogicalResourceId', ''),
                    'type': e.get('ResourceType', ''),
                    'status': status,
                    'reason': (e.get('ResourceStatusReason', '') or '')[:500],
                    'timestamp': e.get('Timestamp', '').isoformat() if hasattr(e.get('Timestamp', ''), 'isoformat') else str(e.get('Timestamp', '')),
                })
        return failed
    except Exception as exc:
        logger.warning(f"[deployer] describe_stack_events failed for {stack_name}: {exc}")
        return []
```

### 變更 2: get_deploy_status() 失敗時附上 CFN events

**檔案：** `src/deployer.py`（`get_deploy_status()` 函數，line ~548）

在偵測到 `status == 'FAILED'` 時，補查 CloudFormation events：

```python
if sfn_status in ['FAILED', 'TIMED_OUT', 'ABORTED']:
    new_status = 'SUCCESS' if sfn_status == 'SUCCEEDED' else 'FAILED'
    # ... existing code ...
    
    if new_status == 'FAILED':
        # 既有：from SFN history
        error_lines = DeployErrorExtractor.from_sfn_history(history_events)
        
        # 新增：from CloudFormation events
        stack_name = record.get('stack_name') or _infer_stack_name(project_id)
        failed_resources = DeployErrorExtractor.from_cfn_events(stack_name)
        if failed_resources:
            ddb_update['failed_resources'] = failed_resources
            # 產生人類可讀 error_summary
            error_summary = '; '.join(
                f"{r['resource']} {r['status']}: {r['reason'][:100]}"
                for r in failed_resources[:3]
            )
            ddb_update['error_summary'] = error_summary
```

### 變更 3: 回傳結構增強

**檔案：** `src/deployer.py`（`get_deploy_status()` 回傳值）

當 `status == 'FAILED'` 時，response 增加：
```json
{
  "status": "FAILED",
  "error_summary": "SharesTable UPDATE_FAILED: Table does not exist",
  "failed_resources": [
    {"resource": "SharesTable", "status": "UPDATE_FAILED", "reason": "...", "type": "AWS::DynamoDB::Table"}
  ],
  "error_lines": ["[BUILD] Error while executing command: ..."]
}
```

### 變更 4: _infer_stack_name() helper

**檔案：** `src/deployer.py`

```python
def _infer_stack_name(project_id: str) -> str:
    """從 PROJECT_CONFIGS 或慣例推斷 CloudFormation stack name。"""
    configs = _get_project_configs()
    if project_id in configs:
        return configs[project_id].get('stack_name', f'{project_id}-dev')
    return f'{project_id}-dev'
```

### 變更 5: deploy history 也顯示 error_summary

**檔案：** `src/mcp_history.py`（或 `deployer.py` 中的 history 函數）

`bouncer_deploy_history` 回傳 FAILED deploy 時，若 record 有 `error_summary`，一併回傳。

## 設計決策

| 決策 | 選項 | 選擇 | 理由 |
|------|------|------|------|
| CFN query 時機 | 每次查詢 vs 只在狀態轉換時 | 狀態轉換時（RUNNING→FAILED） | 避免重複 API 呼叫，且只有第一次偵測到 FAILED 才有最新的 stack events |
| stack_name 來源 | DDB record vs PROJECT_CONFIGS | DDB record 優先，fallback to PROJECT_CONFIGS | deploy 時可記錄 stack_name，更準確 |
| CFN client region | hardcode vs 從 record 取 | 從 record 取，fallback to us-east-1 | 支援多 region deploy |
| failed_resources 上限 | 無限 vs 限制 | 最多 10 個 | 避免 DDB item 過大 |

## Out of Scope

- CodeBuild log 的提取（需要更複雜的 CloudWatch Logs 查詢，另案處理）
- deploy 成功時的 CFN events 記錄
- CFN drift detection

## Test Plan

### Unit Tests（新增）

**檔案：** `tests/test_deployer.py`（補充）

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_from_cfn_events_extracts_failed_resources` | mock CFN describe_stack_events → 正確提取 FAILED resources |
| T2 | `test_from_cfn_events_empty_on_success` | 全部 events 都是 COMPLETE → 回傳空 list |
| T3 | `test_from_cfn_events_api_error_graceful` | CFN API 失敗 → 回傳空 list，不 crash |
| T4 | `test_get_deploy_status_failed_includes_cfn` | FAILED deploy → response 含 failed_resources + error_summary |
| T5 | `test_get_deploy_status_success_no_cfn` | SUCCESS deploy → 不觸發 CFN query |
| T6 | `test_infer_stack_name_from_config` | PROJECT_CONFIGS 有設定 → 回傳設定的 stack_name |
| T7 | `test_infer_stack_name_fallback` | 未知 project → 回傳 `{project}-dev` |

## Acceptance Criteria

- [ ] `DeployErrorExtractor.from_cfn_events()` 正確提取 FAILED resources
- [ ] `get_deploy_status()` FAILED 時自動附上 `error_summary` + `failed_resources`
- [ ] `bouncer_deploy_history` FAILED deploy 顯示 `error_summary`
- [ ] CFN API 失敗時 graceful degradation（不影響原有功能）
- [ ] IAM 權限：Lambda execution role 已有 `cloudformation:DescribeStackEvents`（確認）
- [ ] 新增 7 個測試，全部通過
- [ ] 所有既有測試通過
