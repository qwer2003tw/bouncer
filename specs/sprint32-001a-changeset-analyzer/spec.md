# Changeset Analyzer — Core Logic

## Feature（一句話）
新增 `src/changeset_analyzer.py`，透過 CFN Changeset dry-run 分析一次 SAM deploy 的實際變更範圍，判斷是否為「純 Lambda Code 替換」，為後續 auto-approve 流程提供判斷依據。

---

## User Stories

- **U1** — 作為 Bouncer 開發者，我想要一個可獨立測試的 changeset 分析模組，不依賴 mcp_tool_deploy 流程，方便單元測試。
- **U2** — 作為 Bouncer 系統，我需要能建立 dry-run changeset（不執行）並取得所有 ResourceChange 的詳細資訊。
- **U3** — 作為 Bouncer 系統，我需要能判斷一次 deploy 是否「只改了 Lambda Code」，以決定是否需要人工審批。
- **U4** — 作為 Bouncer 系統，每次 dry-run 完必須清除 changeset，避免殘留影響後續真正的部署。

---

## Acceptance Scenarios（Given/When/Then）

### S1 — 純 Code 變更（should auto-approve）
```
Given  一個 SAM stack 有 2 個 Lambda Functions
When   deploy 只更新了這 2 個 Lambda 的 ImageUri / S3Key
Then   is_code_only_change(result) == True
And    result.is_code_only == True
And    result.error is None
```

### S2 — 含基礎設施變更（should require approval）
```
Given  一個 SAM stack 有 Lambda + DynamoDB
When   deploy 更新了 Lambda Code + DynamoDB BillingMode
Then   is_code_only_change(result) == False
And    result.resource_changes 包含 DynamoDB 的 Modify 條目
```

### S3 — 新增資源（should require approval）
```
Given  一個 SAM stack
When   deploy 新增了一個新的 Lambda Function
Then   ResourceChange.Action == Add → is_code_only_change(result) == False
```

### S4 — 移除資源（should require approval）
```
Given  一個 SAM stack 有 3 個 Lambda Functions
When   deploy 移除了其中一個 Lambda
Then   ResourceChange.Action == Remove → is_code_only_change(result) == False
```

### S5 — Changeset 建立失敗（fail-safe）
```
Given  CFN CreateChangeSet API 拋出例外
When   create_dry_run_changeset() 被呼叫
Then   AnalysisResult.error 不為 None
And    is_code_only_change(result) == False（fail-safe，預設人工審批）
```

### S6 — Changeset describe timeout（fail-safe）
```
Given  Changeset 狀態卡在 CREATE_IN_PROGRESS 超過等待上限
When   analyze_changeset() 被呼叫
Then   拋出 TimeoutError 或回傳 AnalysisResult.error != None
And    is_code_only_change(result) == False
```

### S7 — Lambda Properties 非 Code 屬性變更（should require approval）
```
Given  一個 Lambda Function 的 Timeout 被修改
When   changeset 中 Details[].Target.Name == "Timeout"（非 "Code"）
Then   is_code_only_change(result) == False
```

### S8 — cleanup_changeset 正常執行
```
Given  一個已建立的 dry-run changeset（名稱已知）
When   cleanup_changeset(cfn_client, stack_name, changeset_name) 被呼叫
Then   DeleteChangeSet API 被呼叫一次
And    不拋出例外（即使 changeset 不存在也忽略 ChangeSetNotFoundException）
```

---

## Edge Cases

| Case | 行為 |
|------|------|
| `resource_changes` 為空清單（no-op deploy） | `is_code_only_change` 回傳 `True`（無任何變更，視為安全） |
| CFN 回傳 `ChangeSetNotFoundException` on delete | 靜默忽略（changeset 已自動清除） |
| `Details` 清單為空 | 視為「無 Code 屬性變更」→ False（fail-safe） |
| Changeset 名稱衝突 | 加入時間戳 suffix 避免衝突 |
| `FAILED` changeset status | 回傳 AnalysisResult.error，is_code_only = False |

---

## Requirements

### Functional
- F1: `create_dry_run_changeset(cfn_client, stack_name, template_url)` — 建立 CHANGESET_TYPE=UPDATE、NO_EXECUTE changeset，回傳 changeset_name（str）
- F2: `analyze_changeset(cfn_client, stack_name, changeset_name)` — describe changeset，wait 直到 CREATE_COMPLETE 或 FAILED，回傳 `AnalysisResult`
- F3: `is_code_only_change(analysis_result)` — 純函數，根據 AnalysisResult 回傳 bool
- F4: `cleanup_changeset(cfn_client, stack_name, changeset_name)` — 靜默 delete，ChangeSetNotFoundException 忽略
- F5: `AnalysisResult` dataclass 含 `is_code_only: bool`, `resource_changes: list[dict]`, `error: Optional[str]`

### Non-functional
- NF1: 不引入新外部依賴（只用 boto3 / stdlib）
- NF2: 模組可獨立 import，不依賴 deployer.py 的 global state
- NF3: 所有 AWS API 呼叫使用傳入的 `cfn_client`（不自建）→ 方便 mock
- NF4: Logging 遵循現有 powertools Logger 格式（`extra={"src_module": "changeset_analyzer", ...}`）
- NF5: Changeset 名稱格式：`bouncer-dryrun-{uuid4_hex[:12]}`

---

## Interface Contract

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class AnalysisResult:
    is_code_only: bool
    resource_changes: list  # raw ResourceChange dicts from CFN
    error: Optional[str] = None


def create_dry_run_changeset(
    cfn_client,           # boto3 CloudFormation client
    stack_name: str,      # e.g. "bouncer-prod"
    template_url: str,    # S3 URL of the rendered SAM template
) -> str:
    """建立 dry-run changeset，回傳 changeset_name。
    Raises: botocore.exceptions.ClientError on failure.
    """

def analyze_changeset(
    cfn_client,
    stack_name: str,
    changeset_name: str,
    poll_interval: float = 2.0,
    max_wait: float = 60.0,
) -> AnalysisResult:
    """Describe changeset，等待完成，回傳 AnalysisResult。
    Never raises — error 寫入 AnalysisResult.error。
    """

def is_code_only_change(analysis_result: AnalysisResult) -> bool:
    """判斷是否為純 Lambda Code 變更。純函數，不呼叫 AWS。
    White-list logic:
      1. result.error is None
      2. All ResourceChange.Action == "Modify"
      3. All ResourceChange.ResourceType == "AWS::Lambda::Function"
      4. All Details[].Target.Attribute == "Properties"
      5. All Details[].Target.Name == "Code"
      OR resource_changes is empty (no-op)
    """

def cleanup_changeset(
    cfn_client,
    stack_name: str,
    changeset_name: str,
) -> None:
    """Delete changeset。ChangeSetNotFoundException 靜默忽略。"""
```

### Auto-approve 白名單判斷（完整條件，ALL must be True）

```
1. analysis_result.error is None
2. len(resource_changes) == 0  OR  all of:
   a. rc["Action"] == "Modify"  for all rc in resource_changes
   b. rc["ResourceType"] == "AWS::Lambda::Function"  for all rc
   c. all Detail["Target"]["Attribute"] == "Properties"  for all Details in all rc
   d. all Detail["Target"]["Name"] == "Code"  for all Details in all rc
```
