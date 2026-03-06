# Sprint 9-004: ops: Canary Deployment + Alarm Rollback

> Priority: P1
> Generated: 2026-03-02

---

## Feature Name

Canary Deployment with Alarm-Based Rollback — 將 Bouncer Lambda 部署從 `AllAtOnce` 改為 Canary，搭配 CloudWatch Alarm 自動 rollback。

## Background

目前 `template.yaml:283` 設定 `DeploymentPreference.Type: AllAtOnce`，表示每次部署直接切 100% 流量到新版本。如果新版本有 bug，所有流量立刻受影響。

已有的 alarm 基礎建設：
- `HighErrorAlarm`（Lambda error rate > 5/5min）
- `ApiGateway5xxAlarm`（API 5xx > 5/5min）
- `LambdaDurationAlarm`（p99 duration approaching timeout）
- `AlarmNotificationTopic`（SNS）

## User Stories

**US-1: 漸進部署**
As a **DevOps operator**,
I want Lambda deployments to use canary or linear traffic shifting,
So that only a small percentage of traffic hits the new version initially.

**US-2: 自動 rollback**
As a **DevOps operator**,
I want CloudWatch alarms to automatically trigger rollback during deployment,
So that bad deployments are caught and reverted without manual intervention.

**US-3: 部署可觀測性**
As **Steven**,
I want Telegram notifications when a deployment starts canary, completes, or rolls back,
So that I'm aware of deployment progress without checking the console.

## Acceptance Scenarios

### Scenario 1: 正常 Canary 部署
- **Given**: `DeploymentPreference.Type: Canary10Percent5Minutes`
- **When**: SAM deploy 完成
- **Then**: 10% 流量切到新版本
- **And**: 5 分鐘後無 alarm → 100% 切到新版本
- **And**: CodeDeploy deployment 狀態 = Succeeded

### Scenario 2: Alarm 觸發 rollback
- **Given**: Canary 部署中，10% 流量已切到新版本
- **When**: `HighErrorAlarm` 進入 ALARM 狀態
- **Then**: CodeDeploy 自動 rollback 到舊版本
- **And**: 100% 流量回到舊版本
- **And**: SNS 通知 rollback 事件

### Scenario 3: Manual rollback
- **Given**: Canary 部署中
- **When**: Steven 在 CodeDeploy console 手動 stop + rollback
- **Then**: 流量回到舊版本

### Scenario 4: Alarm 在部署前就處於 ALARM 狀態
- **Given**: `HighErrorAlarm` 已在 ALARM 狀態
- **When**: 觸發新部署
- **Then**: 部署行為取決於 SAM/CodeDeploy 配置（可能需要先 resolve alarm）

## Edge Cases

1. **冷啟動影響**：新版本 Lambda 冷啟動可能觸發 duration alarm → 需要調整 alarm threshold 或 evaluation periods
2. **Provisioned Concurrency**：若啟用 provisioned concurrency，canary 行為可能不同
3. **API Gateway cache**：若有 cache 層，canary 流量可能不均勻
4. **Bouncer 的回 callback 場景**：pending 審批 → canary 期間版本切換 → 新舊版本的 callback handler 都要能處理

## Requirements

- **R1**: `template.yaml` 修改 `DeploymentPreference.Type` 為 `Canary10Percent5Minutes`
- **R2**: `DeploymentPreference.Alarms` 列出 `HighErrorAlarm` 和 `ApiGateway5xxAlarm`
- **R3**: 確保 alarm evaluation periods 和 canary 時間窗口匹配
- **R4**: 不需要新增 Lambda code 變更（純 infrastructure）

## Interface Contract

### template.yaml 變更

```yaml
DeploymentPreference:
  Type: Canary10Percent5Minutes
  Alarms:
    - !Ref HighErrorAlarm
    - !Ref ApiGateway5xxAlarm
```

### 無 API/DDB Schema 變更

此 task 純 infrastructure，不涉及 code 或 API 變更。
