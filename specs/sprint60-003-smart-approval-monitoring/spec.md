# Feature Specification: Smart Approval Phase 6 監控基礎

Feature Branch: feat/sprint60-003-smart-approval-monitoring
Sprint: 60
Task ID: bouncer-s60-003

## Problem Statement

Smart Approval 系統（Phase 5, Sprint 55-59）已穩定運行，但缺乏監控基礎設施：

1. **ShadowApprovalsTable 缺 PITR**：其他 3 個 DynamoDB table 都有 `PointInTimeRecoveryEnabled: true`（template.yaml lines 116-117, 140-141, 184-185），但 ShadowApprovalsTable（line 195）沒有
2. **smart_approval.py + sequence_analyzer.py 無 emit_metric**：911 行的 `smart_approval.py` 和 760 行的 `sequence_analyzer.py` 完全沒有 CloudWatch metric emission
3. **無 custom metric alarm**：現有 4 個 CloudWatch Alarms 都是 AWS 原生 metrics（Lambda Errors、API 5xx、Lambda Duration、DLQ Depth），沒有 Bouncer custom metric alarm

### 現行 CloudWatch Alarms（template.yaml）

| Alarm | Metric | Namespace |
|-------|--------|-----------|
| HighErrorAlarm (line 536) | Errors | AWS/Lambda |
| ApiGateway5xxAlarm (line 575) | 5XXError | AWS/ApiGateway |
| LambdaDurationAlarm (line 595) | Duration (p99) | AWS/Lambda |
| DLQDepthAlarm (line 626) | ApproximateNumberOfMessagesVisible | AWS/SQS |

---

## User Scenarios & Testing

### User Story 1：ShadowApprovalsTable 加 PITR

> 作為系統管理員，我需要 ShadowApprovalsTable 啟用 Point-in-Time Recovery，以便在資料損壞時可恢復至任意時間點。

**Given** ShadowApprovalsTable 目前沒有 PITR（其他 3 個 table 都有）
**When** 在 template.yaml 中加入 `PointInTimeRecoverySpecification`
**Then** 部署後 ShadowApprovalsTable 啟用 PITR
**And** 不影響現有 table 資料

### User Story 2：Smart Approval metric emission

> 作為 DevOps 工程師，我需要 smart_approval 的決策結果和 sequence_analyzer 的分析結果發射 CloudWatch metrics，以建立業務監控 dashboard。

**Given** `smart_approval.py` 的 `evaluate_command()` 完成風險評估
**When** 成功產生 `ApprovalDecision`
**Then** 發射以下 metrics（Namespace: `Bouncer`）：
  - `SmartApprovalScore`（value=final_score, dimensions={Decision: auto_approve|blocked|...}）
  - `SmartApprovalDecision`（value=1, dimensions={Decision: auto_approve|blocked|needs_approval|...}）

**Given** `sequence_analyzer.py` 的 `analyze_sequence()` 完成序列分析
**When** 分析出 risk_modifier
**Then** 發射 metric `SequenceRiskModifier`（value=risk_modifier, dimensions={HasPriorQuery: true|false}）

### User Story 3：Custom metric CloudWatch Alarms

> 作為系統管理員，我需要 Bouncer custom metrics 觸發 CloudWatch Alarm，當 BlockedCommand 或 ScannerError 異常增加時收到通知。

**Given** Bouncer 已在多處 emit `BlockedCommand`、`ScannerError`（Sprint 60-001 新增）等 custom metrics
**When** 5 分鐘內 `BlockedCommand` count > 10 或 `ScannerError` count > 3
**Then** 觸發 CloudWatch Alarm → SNS notification

---

## Requirements

### FR-001：ShadowApprovalsTable PITR
- 在 `template.yaml` 的 ShadowApprovalsTable Properties 中加入：
  ```yaml
  PointInTimeRecoverySpecification:
    PointInTimeRecoveryEnabled: true
  ```
- 位置：約 line 208（在 TimeToLiveSpecification 之後）

### FR-002：smart_approval.py emit_metric
- 在 `evaluate_command()` 成功路徑（line ~120）emit 兩個 metrics：
  1. `SmartApprovalScore` — value=final_score
  2. `SmartApprovalDecision` — value=1, dimensions={Decision: decision_type}
- 在 error fallback 路徑（line ~130）emit：
  1. `SmartApprovalError` — value=1
- Import：`from metrics import emit_metric`

### FR-003：sequence_analyzer.py emit_metric
- 在 `analyze_sequence()`（line ~636）return 前 emit：
  1. `SequenceRiskModifier` — value=risk_modifier, dimensions={HasPriorQuery: str(has_service_match)}
- 在 `get_sequence_risk_modifier()` 的 error path 不 emit（已有 return 0.0）
- Import：`from metrics import emit_metric`

### FR-004：CloudWatch Alarms for custom metrics
- 在 `template.yaml` 新增 3 個 Alarms：

**BlockedCommandAlarm**
```yaml
MetricName: BlockedCommand
Namespace: Bouncer
Statistic: Sum
Period: 300
EvaluationPeriods: 1
Threshold: 10
ComparisonOperator: GreaterThanThreshold
```

**ScannerErrorAlarm**
```yaml
MetricName: ScannerError
Namespace: Bouncer
Statistic: Sum
Period: 300
EvaluationPeriods: 1
Threshold: 3
ComparisonOperator: GreaterThanThreshold
```

**SmartApprovalErrorAlarm**
```yaml
MetricName: SmartApprovalError
Namespace: Bouncer
Statistic: Sum
Period: 300
EvaluationPeriods: 1
Threshold: 5
ComparisonOperator: GreaterThanThreshold
```

所有 Alarms 的 `AlarmActions` 指向現有 `!Ref AlarmNotificationTopic`。

### FR-005：TreatMissingData 策略
- 所有新增 Alarms 使用 `TreatMissingData: notBreaching`
- Custom metrics 不一定每個 period 都有 data point，notBreaching 避免誤報

---

## Interface Contract

### smart_approval.py 變更

```python
# 新增 import
from metrics import emit_metric

# evaluate_command() 成功路徑（return 前）
emit_metric('Bouncer', 'SmartApprovalScore', final_score, dimensions={'Decision': decision})
emit_metric('Bouncer', 'SmartApprovalDecision', 1, dimensions={'Decision': decision})

# evaluate_command() error fallback 路徑
emit_metric('Bouncer', 'SmartApprovalError', 1)
```

### sequence_analyzer.py 變更

```python
# 新增 import
from metrics import emit_metric

# analyze_sequence() return 前
emit_metric('Bouncer', 'SequenceRiskModifier', risk_modifier, unit='None',
            dimensions={'HasPriorQuery': str(has_service_match)})
```

### template.yaml 變更

- ShadowApprovalsTable：+3 行（PITR）
- 新增 3 個 CloudWatch::Alarm resources（~60 行）
- 這是 **infra-only deploy**（code 改動只加 metric，不影響 API）

---

## TCS 計算

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| D1 Files | 2/5 | 3 files：`smart_approval.py`、`sequence_analyzer.py`、`template.yaml` |
| D2 Cross-module | 1/4 | 只加 `from metrics import emit_metric`，metrics.py 無需修改 |
| D3 Testing | 2/4 | 需 mock emit_metric 驗證呼叫、template.yaml 無 unit test（靠 SAM validate） |
| D4 Infrastructure | 2/4 | template.yaml 改動（PITR + 3 Alarms），需 SAM deploy |
| D5 External | 0/4 | 無外部 API 整合 |

**Total TCS: 7 (Simple)**
→ Sub-agent strategy: 1 agent timeout 600s

---

## Cost Analysis

### PITR 成本

- DynamoDB PITR: $0.20 per GB per month
- ShadowApprovalsTable 目前資料量：shadow mode 下資料量小（估計 < 100MB）
- **預估月成本：~$0.02/month**
- 有 TTL 自動清理，資料量不會無限成長

### CloudWatch Alarm 成本

- Standard Alarm: $0.10/alarm/month
- 新增 3 個 Alarms: **$0.30/month**
- Custom metric: 前 10 個免費，之後 $0.30/metric/month
- SmartApprovalScore、SmartApprovalDecision、SmartApprovalError、SequenceRiskModifier → 4 個新 metrics
- **預估新增 custom metric 成本：$0（在免費額度內，因 Bouncer 已用的 metrics + 新增仍 < 10）**

### 總新增成本

**~$0.32/month**

---

## Success Criteria

- SC-001：ShadowApprovalsTable PITR 啟用確認（`aws dynamodb describe-continuous-backups`）
- SC-002：`smart_approval.py` 的 `evaluate_command()` emit 至少 2 個 metrics
- SC-003：`sequence_analyzer.py` 的 `analyze_sequence()` emit `SequenceRiskModifier`
- SC-004：3 個新 CloudWatch Alarms 建立成功
- SC-005：所有既有 tests 通過
- SC-006：`sam validate` 通過
- SC-007：error fallback 路徑也有 metric emission
