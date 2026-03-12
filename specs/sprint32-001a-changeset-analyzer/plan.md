# Implementation Plan — 001a Changeset Analyzer Core

## Technical Context

### 影響檔案
| 檔案 | 操作 | 風險 |
|------|------|------|
| `src/changeset_analyzer.py` | **新增** | 低：全新模組，零改現有程式碼 |
| `tests/test_changeset_analyzer.py` | **新增** | 低：unit tests only，不需真實 AWS |

### 現有架構關鍵點
- `deployer.py` 已有 `_get_cfn_client()` lazy-init CFN client（line ~82）→ 001b 會複用
- `stack_name` 在 project config 以 `project.get('stack_name', '')` 取得（line ~279, 939）
- CFN client 已用在 `describe_stack_events`（line ~732）→ 確認 IAM 可延伸
- `template_url` 需在 001b 中從 SAM deployer 流程取得（S3 presigned 或已知 bucket key）

### 風險評估
| 風險 | 機率 | 緩解 |
|------|------|------|
| CFN CreateChangeSet 需 IAM 權限（現在沒有） | 高 | 001b 負責加 IAM；001a 純邏輯不呼叫真實 AWS |
| Changeset 名稱衝突 | 低 | UUID suffix |
| Changeset 殘留（cleanup 失敗） | 低 | CloudFormation 自動 GC 舊 changeset；加重試 |
| 等待 changeset 建立 timeout | 中 | max_wait=60s 預設；測試用 mock |

---

## Constitution Check

### 安全
- ✅ dry-run changeset 不執行任何變更（`--no-execute`）
- ✅ 只讀取 CFN describe API，不寫入任何資源
- ✅ fail-safe：任何分析錯誤 → False → 照舊人工審批
- ✅ 無新外部依賴（boto3 already audited）

### 成本
- CloudFormation changeset 建立免費（不計費）
- DescribeChangeSet API 呼叫：$0.0035/1000 次，極低
- 預估每次 deploy 增加 2-3 次 API 呼叫

### 架構
- ✅ 純函數設計（`is_code_only_change` 是 stateless）
- ✅ cfn_client 注入（依賴注入，易測）
- ✅ 不破壞任何現有 API 或流程
- ✅ AnalysisResult dataclass 比 dict 更型別安全

---

## Implementation Phases

### Phase 1.1 — AnalysisResult + is_code_only_change（純邏輯）
**目標：** 寫完 dataclass 和判斷邏輯，不涉及 AWS  
**測試：** T001-T004（白名單邏輯測試，全用假 data）

```python
# src/changeset_analyzer.py skeleton

from dataclasses import dataclass, field
from typing import Optional
import uuid
import time
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

@dataclass
class AnalysisResult:
    is_code_only: bool
    resource_changes: list
    error: Optional[str] = None

def is_code_only_change(result: AnalysisResult) -> bool:
    if result.error is not None:
        return False
    if not result.resource_changes:
        return True  # no-op
    for rc in result.resource_changes:
        if rc.get("Action") != "Modify":
            return False
        if rc.get("ResourceType") != "AWS::Lambda::Function":
            return False
        for detail in rc.get("Details", []):
            target = detail.get("Target", {})
            if target.get("Attribute") != "Properties":
                return False
            if target.get("Name") != "Code":
                return False
    return True
```

### Phase 1.2 — create_dry_run_changeset
**目標：** 呼叫 `cfn_client.create_change_set(ChangeSetType='UPDATE', ...)`  
**測試：** T005（mock cfn_client）

```python
def create_dry_run_changeset(cfn_client, stack_name: str, template_url: str) -> str:
    changeset_name = f"bouncer-dryrun-{uuid.uuid4().hex[:12]}"
    cfn_client.create_change_set(
        StackName=stack_name,
        ChangeSetName=changeset_name,
        TemplateURL=template_url,
        ChangeSetType='UPDATE',
        Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
    )
    return changeset_name
```

### Phase 1.3 — analyze_changeset（poll + describe）
**目標：** poll until CREATE_COMPLETE / FAILED，parse ResourceChanges  
**測試：** T006（mock status sequence）

```python
def analyze_changeset(cfn_client, stack_name: str, changeset_name: str,
                      poll_interval: float = 2.0, max_wait: float = 60.0) -> AnalysisResult:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        resp = cfn_client.describe_change_set(
            StackName=stack_name, ChangeSetName=changeset_name)
        status = resp.get("Status")
        if status == "CREATE_COMPLETE":
            changes = resp.get("Changes", [])
            resource_changes = [c["ResourceChange"] for c in changes
                                if c.get("Type") == "Resource"]
            return AnalysisResult(is_code_only=False, resource_changes=resource_changes)
        if status == "FAILED":
            reason = resp.get("StatusReason", "Unknown")
            return AnalysisResult(is_code_only=False, resource_changes=[], error=reason)
        time.sleep(poll_interval)
    return AnalysisResult(is_code_only=False, resource_changes=[],
                          error="Timeout waiting for changeset")
```

> Note: `is_code_only` 先設 False，最後由 `is_code_only_change()` 計算正確值。

### Phase 1.4 — cleanup_changeset
**目標：** 靜默 delete  
**測試：** T007, T008

### Phase 1.5 — 補全測試（8+ cases）
**測試覆蓋：**
- T001: 2x Lambda Code-only → True
- T002: Lambda + DynamoDB → False
- T003: Action=Add → False
- T004: Action=Remove → False
- T005: Lambda Timeout change → False
- T006: error 不為空 → False
- T007: empty resource_changes → True (no-op)
- T008: cleanup ChangeSetNotFoundException → no raise
