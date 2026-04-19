# Tasks: Smart Approval Phase 6 監控基礎

Sprint: 60 | Task: bouncer-s60-003

## Phase 1: Setup

```bash
cd /home/ec2-user/projects/bouncer
git worktree add /tmp/s60-003-smart-approval-monitoring feat/sprint60-003-smart-approval-monitoring -b feat/sprint60-003-smart-approval-monitoring
cd /tmp/s60-003-smart-approval-monitoring
```

## Phase 2: Infrastructure（template.yaml）

### Task 2.1：ShadowApprovalsTable 加 PITR

在 `template.yaml` 的 ShadowApprovalsTable（line ~195）Properties 中，TimeToLiveSpecification 之後加入：

```yaml
      PointInTimeRecoverySpecification:
        PointInTimeRecoveryEnabled: true
```

### Task 2.2：新增 BlockedCommandAlarm

在 DLQDepthAlarm 之後（line ~640 區域），新增：

```yaml
  BlockedCommandAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${Environment}-bouncer-blocked-commands"
      AlarmDescription: High rate of blocked commands - possible attack or misconfiguration
      MetricName: BlockedCommand
      Namespace: Bouncer
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 10
      ComparisonOperator: GreaterThanThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmNotificationTopic
```

### Task 2.3：新增 ScannerErrorAlarm

```yaml
  ScannerErrorAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${Environment}-bouncer-scanner-errors"
      AlarmDescription: Upload scanner errors - scanner may be failing
      MetricName: ScannerError
      Namespace: Bouncer
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 3
      ComparisonOperator: GreaterThanThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmNotificationTopic
```

### Task 2.4：新增 SmartApprovalErrorAlarm

```yaml
  SmartApprovalErrorAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${Environment}-bouncer-smart-approval-errors"
      AlarmDescription: Smart approval evaluation errors - risk scoring may be failing
      MetricName: SmartApprovalError
      Namespace: Bouncer
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 5
      ComparisonOperator: GreaterThanThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmNotificationTopic
```

### Task 2.5：SAM validate

```bash
sam validate --template template.yaml
```

## Phase 3: Code — smart_approval.py

### Task 3.1：加入 emit_metric import

```python
# Line 1-10 區塊，加入：
from metrics import emit_metric
```

### Task 3.2：成功路徑 metric emission

在 `evaluate_command()` 的 return 之前（line ~125，`return ApprovalDecision(...)` 之前）加入：

```python
        # Phase 6: emit monitoring metrics
        emit_metric('Bouncer', 'SmartApprovalScore', final_score,
                     dimensions={'Decision': decision})
        emit_metric('Bouncer', 'SmartApprovalDecision', 1,
                     dimensions={'Decision': decision})
```

### Task 3.3：error fallback 路徑 metric emission

在 `evaluate_command()` 的 except block（line ~130）中，`return ApprovalDecision(...)` 之前加入：

```python
        emit_metric('Bouncer', 'SmartApprovalError', 1)
```

## Phase 4: Code — sequence_analyzer.py

### Task 4.1：加入 emit_metric import

```python
# 檔案頂部 import 區塊加入：
from metrics import emit_metric
```

### Task 4.2：analyze_sequence() metric emission

在 `analyze_sequence()` 的 3 個 return 路徑前分別加入 metric（line ~700 區域）：

```python
    # 在 return SequenceAnalysis(...) 之前：
    emit_metric('Bouncer', 'SequenceRiskModifier', risk_modifier, unit='None',
                dimensions={'HasPriorQuery': str(has_service_match)})
```

⚠️ 注意：`analyze_sequence()` 有 4 個 return 路徑：
1. 非危險操作早期 return（line ~669）→ 不需 emit（risk_modifier=0, 正常）
2. ClientError（歷史查詢失敗, line ~682）→ emit
3. 最終結果 return（line ~729）→ emit

只在 path 2 和 path 3 emit，path 1（非危險操作）不需要。

## Phase 5: Tests

### Task 5.1：smart_approval.py tests

```python
# tests/test_smart_approval.py 或對應 test file

def test_evaluate_command_emits_score_metric():
    """evaluate_command 成功時 emit SmartApprovalScore"""
    with patch('smart_approval.emit_metric') as mock_emit:
        with patch('smart_approval.calculate_risk') as mock_risk:
            mock_risk.return_value = RiskResult(score=30, ...)
            with patch('smart_approval.get_sequence_risk_modifier', return_value=(0.0, '')):
                evaluate_command('aws s3 ls', 'test', 'agent', '123')
                # 檢查 emit_metric 被呼叫，包含 SmartApprovalScore
                calls = [c[0] for c in mock_emit.call_args_list]
                assert any('SmartApprovalScore' in str(c) for c in calls)

def test_evaluate_command_error_emits_error_metric():
    """evaluate_command error 時 emit SmartApprovalError"""
    with patch('smart_approval.emit_metric') as mock_emit:
        with patch('smart_approval.calculate_risk', side_effect=Exception("boom")):
            result = evaluate_command('aws s3 ls', 'test', 'agent', '123')
            assert result.decision == ApprovalDecision.NEEDS_APPROVAL
            mock_emit.assert_called_with('Bouncer', 'SmartApprovalError', 1)
```

### Task 5.2：sequence_analyzer.py tests

```python
def test_analyze_sequence_emits_modifier_metric():
    """analyze_sequence emit SequenceRiskModifier"""
    with patch('sequence_analyzer.emit_metric') as mock_emit:
        with patch('sequence_analyzer.get_recent_commands', return_value=[]):
            # 用一個危險操作觸發分析
            result = analyze_sequence('agent', 'aws ec2 terminate-instances --instance-ids i-123')
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][1] == 'SequenceRiskModifier'
```

### Task 5.3：全 tests regression

```bash
python -m pytest tests/ -x --tb=short 2>&1 | tail -30
```

## Phase 6: Lint & Commit

```bash
ruff check src/smart_approval.py src/sequence_analyzer.py template.yaml
git add src/smart_approval.py src/sequence_analyzer.py template.yaml tests/
git commit -m "feat(monitoring): smart approval Phase 6 monitoring foundation (#s60-003)

- Enable PITR on ShadowApprovalsTable
- Add emit_metric to smart_approval.py (score, decision, error)
- Add emit_metric to sequence_analyzer.py (risk modifier)
- Add CloudWatch Alarms: BlockedCommand, ScannerError, SmartApprovalError
- All alarms use TreatMissingData: notBreaching
- Cost: ~$0.32/month (PITR + 3 alarms)
"
```

## TCS Summary

TCS=7 → 1 agent timeout 600s

⚠️ **部署注意**：此任務包含 template.yaml 改動（PITR + Alarms），需 SAM deploy。
- PITR 啟用是 **非破壞性** 的 table update，不會影響資料
- CloudWatch Alarms 是新增 resource，不影響現有 Alarms
- 建議 infra 改動（template.yaml）和 code 改動一起 deploy
