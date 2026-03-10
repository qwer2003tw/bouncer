# Sprint 17-005: 啟用 API Gateway Access Log

> GitHub Issue: #76
> Priority: P1
> TCS: 4
> Generated: 2026-03-08

---

## Problem Statement

Bouncer API Gateway (`n8s3f1mus6`) 的 access log 和 execution log 都沒有啟用。當出現 callback 延遲、webhook 丟失等問題時，無法確認：
- Telegram webhook 請求是否真的打進來了
- 請求的精確時間
- HTTP status code（是否有 4xx/5xx）
- 延遲是在 API Gateway 層還是 Lambda 層

### 觸發背景
2026-03-06 Canary deployment 期間，approve callback 的 Lambda invoke 延遲了約 2.5 分鐘。Lambda log 顯示 `05:30:43` 才有 cold start，但沒有 API Gateway access log，無法判斷是 API Gateway 問題還是 Lambda 問題。

## Root Cause

`template.yaml` 中 `BouncerApi`（`AWS::Serverless::Api`）沒有設定 `AccessLogSetting`。只有基本的 `LoggingConfig` 和 `StageName`。

## Scope

### 變更 1: 新增 CloudWatch Log Group

**檔案：** `template.yaml`

```yaml
  # ============================================================
  # API Gateway Access Log
  # ============================================================
  ApiGatewayAccessLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: !Sub "/aws/apigateway/bouncer-${Environment}-access"
      RetentionInDays: 30
      Tags:
        - Key: Project
          Value: Bouncer
```

### 變更 2: API Gateway Stage 啟用 Access Log

**檔案：** `template.yaml`

在 `BouncerApi` 的 Properties 加入 `AccessLogSetting`：

```yaml
  BouncerApi:
    Type: AWS::Serverless::Api
    Properties:
      Name: !Sub "bouncer-${Environment}-api"
      StageName: prod
      AccessLogSetting:
        DestinationArn: !GetAtt ApiGatewayAccessLogGroup.Arn
        Format: >-
          {"requestId":"$context.requestId",
          "ip":"$context.identity.sourceIp",
          "requestTime":"$context.requestTime",
          "httpMethod":"$context.httpMethod",
          "path":"$context.path",
          "status":"$context.status",
          "responseLatency":"$context.responseLatency",
          "integrationLatency":"$context.integrationLatency",
          "userAgent":"$context.identity.userAgent"}
      Tags:
        Project: Bouncer
        auto-delete: "no"
```

### 變更 3: IAM — API Gateway 寫 CloudWatch Logs 權限

**檔案：** `template.yaml`

確認 API Gateway 有 CloudWatch Logs 寫入權限。SAM 的 `AWS::Serverless::Api` 通常自動處理，但需要帳號層級的 `apigateway.amazonaws.com` → CloudWatch Logs 權限。

檢查是否需要新增 `AWS::ApiGateway::Account` resource：

```yaml
  ApiGatewayAccount:
    Type: AWS::ApiGateway::Account
    Properties:
      CloudWatchRoleArn: !GetAtt ApiGatewayCloudWatchRole.Arn

  ApiGatewayCloudWatchRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub "bouncer-${Environment}-apigw-cloudwatch"
      AssumeRolePolicyDocument:
        Version: "2012-10-0"
        Statement:
          - Effect: Allow
            Principal:
              Service: apigateway.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs
```

⚠️ **注意：** `AWS::ApiGateway::Account` 是帳號層級 singleton。如果其他 stack 已設定，不需要重複建立。**部署前需確認**帳號是否已有此設定：
```bash
aws apigateway get-account --region us-east-1
# 看 cloudwatchRoleArn 是否已設定
```

## 設計決策

| 決策 | 選項 | 選擇 | 理由 |
|------|------|------|------|
| Log format | CLF vs JSON | JSON | 可用 CloudWatch Insights 查詢 |
| Retention | 7 / 14 / 30 / 90 天 | 30 天 | 足夠 debug，成本低 |
| 是否包含 userAgent | 是 vs 否 | 是 | 與 #74 audit trail 互補，可辨識請求來源 |
| 是否啟用 execution log | 是 vs 否 | 否（本輪） | execution log 會記錄 request/response body，含敏感資訊 + 成本高 |
| ApiGateway::Account | 新增 vs 確認既有 | 部署前確認 | 避免覆蓋其他 stack 的設定 |

## Out of Scope

- API Gateway execution log（DataTraceEnabled=true — 含敏感資料）
- CloudWatch Dashboard 建立
- Alarm based on access log metrics
- WAF 整合

## Test Plan

### 部署前驗證

```bash
# 確認帳號是否已有 API Gateway CloudWatch role
aws apigateway get-account --region us-east-1
```

### 部署後驗證

| # | 測試 | 預期 |
|---|------|------|
| T1 | 發一個 bouncer_execute 請求 | API Gateway access log 出現在 `/aws/apigateway/bouncer-prod-access` |
| T2 | 查看 log 格式 | JSON 格式，含 requestId, ip, status, latency |
| T3 | Telegram webhook callback | access log 記錄 webhook 的到達時間和 integration latency |
| T4 | CloudFormation stack update | Log Group + AccessLogSetting 正確建立 |

### CloudFormation 驗證

```bash
# 確認 Log Group 建立
aws logs describe-log-groups --log-group-name-prefix "/aws/apigateway/bouncer-prod-access"

# 確認 Access Log 設定
aws apigateway get-stage --rest-api-id {api-id} --stage-name prod \
  --query 'accessLogSettings'
```

## Acceptance Criteria

- [ ] CloudWatch Log Group `/aws/apigateway/bouncer-{env}-access` 建立
- [ ] API Gateway Stage 有 AccessLogSetting 指向此 Log Group
- [ ] Access log 為 JSON 格式，含 requestId/ip/status/latency/userAgent
- [ ] Retention 30 天
- [ ] 部署後驗證：至少一筆 access log 出現
- [ ] 既有 API 行為不受影響
