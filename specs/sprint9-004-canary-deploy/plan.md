# Sprint 9-004: Plan — Canary Deployment + Alarm Rollback

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **`template.yaml:281-283`**：
   ```yaml
   AutoPublishAlias: live
   DeploymentPreference:
     Type: AllAtOnce
   ```

2. **現有 Alarms**：
   - `HighErrorAlarm`（`template.yaml:415`）：Lambda error > 5/5min
   - `ApiGateway5xxAlarm`（`template.yaml:454`）：API 5xx > 5/5min
   - `LambdaDurationAlarm`（`template.yaml:474`）：p99 duration > 50s

3. **SAM Deployment Preference 支援的類型**：
   - `Canary10Percent5Minutes` — 10% → 5 min → 100%
   - `Canary10Percent10Minutes` — 10% → 10 min → 100%
   - `Canary10Percent15Minutes` — 10% → 15 min → 100%
   - `Canary10Percent30Minutes` — 10% → 30 min → 100%
   - `Linear10PercentEvery1Minute` — 每分鐘 +10%
   - `AllAtOnce` — 一次切 100%（現狀）

4. **Alarm 與 CodeDeploy 整合**：SAM 自動在 deployment preference 下建立 CodeDeploy deployment group，`Alarms` 列表讓 CodeDeploy 在部署期間監控這些 alarm。

### 設計選擇

**推薦 `Canary10Percent5Minutes`**：
- 10% 流量做 5 分鐘 canary → 對 Bouncer 來說足夠偵測問題
- Bouncer 不是高流量服務，5 分鐘的 10% 已能涵蓋足夠請求
- 過長（30 min）會拖慢部署節奏

## Implementation Phases

### Phase 1: template.yaml 修改

1. 修改 `DeploymentPreference.Type` 為 `Canary10Percent5Minutes`
2. 新增 `DeploymentPreference.Alarms`:
   ```yaml
   Alarms:
     - !Ref HighErrorAlarm
     - !Ref ApiGateway5xxAlarm
   ```
3. **不包含** `LambdaDurationAlarm`（冷啟動可能誤觸）

### Phase 2: Alarm threshold review

1. 確認 `HighErrorAlarm` 的 `Period: 300` 和 `EvaluationPeriods: 1` 在 5 分鐘 canary 窗口內有效
2. 確認 `Threshold: 5` 是否合理（10% 流量下，5 個 error 意味著較高 error rate）
3. 若需調整：降低 `Period` 到 60 秒，`EvaluationPeriods` 調整

### Phase 3: 測試部署

1. 部署此變更（用 bouncer_deploy）
2. 確認 CodeDeploy deployment group 正確建立
3. 確認 canary 期間 alarm 關聯正確
4. 下一次功能部署驗證 canary 行為

### Phase 4: 文檔

1. 更新部署文檔，說明 canary 行為
2. 記錄 alarm + rollback 行為
