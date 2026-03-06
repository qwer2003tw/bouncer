# Sprint 9-004: Tasks — Canary Deployment + Alarm Rollback

> Generated: 2026-03-02

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | template.yaml (1 file) |
| D2 Cross-module | 0 | 純 infrastructure，無 code 變更 |
| D3 Testing | 0 | 無程式測試需求（驗證透過實際部署） |
| D4 Infrastructure | 4 | 修改 template.yaml（DeploymentPreference + Alarms） |
| D5 External | 2 | CodeDeploy（已知 AWS service，SAM 自動管理） |
| **Total TCS** | **7** | ✅ 不需拆分 |

## Task List

```
[004-T1] [P1] [US-1] template.yaml: DeploymentPreference.Type 改為 Canary10Percent5Minutes
[004-T2] [P1] [US-2] template.yaml: DeploymentPreference.Alarms 加入 HighErrorAlarm + ApiGateway5xxAlarm
[004-T3] [P2] [US-2] Review alarm thresholds 是否適合 canary 窗口（Period/EvaluationPeriods）
[004-T4] [P2] [US-3] 部署後驗證：確認 CodeDeploy deployment group 正確建立 + alarm 關聯
[004-T5] [P2] [US-3] 文檔更新：部署行為說明、rollback 流程
```
