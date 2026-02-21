# Bouncer Runbook

## Table of Contents

1. [Lambda 錯誤率飆升](#1-lambda-錯誤率飆升)
2. [DynamoDB Throttle](#2-dynamodb-throttle)
3. [Deployer Stuck/Failed 恢復](#3-deployer-stuckfailed-恢復)
4. [Telegram Webhook 失效](#4-telegram-webhook-失效)
5. [Secret 輪換流程](#5-secret-輪換流程)

---

## 1. Lambda 錯誤率飆升

### 告警

- **Alarm**: `bouncer-{env}-high-error-rate` — Lambda Errors > 5 in 5 minutes

### 排查步驟

1. **查看 CloudWatch Logs**
   ```bash
   aws logs filter-log-events \
     --log-group-name /aws/lambda/bouncer-prod-function \
     --start-time $(date -d '30 minutes ago' +%s000) \
     --filter-pattern "ERROR"
   ```

2. **檢查錯誤類型**
   - `ImportError` / `ModuleNotFoundError` → 部署問題，rollback
   - `ClientError` (DynamoDB) → 見 [DynamoDB Throttle](#2-dynamodb-throttle)
   - `ConnectionError` (Telegram API) → Telegram 服務問題，通常自動恢復
   - `TimeoutError` → Lambda 記憶體不足或外部服務慢

3. **檢查 Lambda 配置**
   ```bash
   aws lambda get-function-configuration \
     --function-name bouncer-prod-function
   ```
   確認 timeout、memory、environment variables 正確。

4. **檢查最近部署**
   ```bash
   aws lambda list-versions-by-function \
     --function-name bouncer-prod-function \
     --max-items 5
   ```

5. **緊急 Rollback**（如果是部署造成）
   ```bash
   # 找到上一個正常版本
   aws lambda list-aliases --function-name bouncer-prod-function
   # 更新 alias 指向上一版本
   aws lambda update-alias \
     --function-name bouncer-prod-function \
     --name live \
     --function-version <PREVIOUS_VERSION>
   ```

6. **檢查 X-Ray Traces**
   - 到 AWS Console → X-Ray → Traces
   - 過濾 `bouncer-prod-function`
   - 找出延遲瓶頸或錯誤節點

### 恢復確認

- HighErrorAlarm 回到 OK 狀態
- CloudWatch Logs 不再有新 ERROR
- API Gateway 5xx rate 回到 0

---

## 2. DynamoDB Throttle

### 症狀

- Lambda 日誌出現 `ProvisionedThroughputExceededException`
- 請求延遲增加
- 部分 API 回傳 500

### 排查步驟

1. **確認是哪張表**
   ```bash
   # 檢查各表的 throttle 指標
   for table in bouncer-prod-requests bouncer-prod-accounts bouncer-prod-command-history; do
     echo "=== $table ==="
     aws cloudwatch get-metric-statistics \
       --namespace AWS/DynamoDB \
       --metric-name ThrottledRequests \
       --dimensions Name=TableName,Value=$table \
       --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
       --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
       --period 300 --statistics Sum
   done
   ```

2. **檢查表的容量模式**
   ```bash
   aws dynamodb describe-table --table-name bouncer-prod-requests \
     --query 'Table.BillingModeSummary'
   ```
   - Bouncer 使用 PAY_PER_REQUEST（On-Demand），理論上不應 throttle
   - 如果發生，可能超過 On-Demand 的 burst limit

3. **緊急處理**
   - On-Demand 模式下，DynamoDB 會自動調整，通常等待幾分鐘即可
   - 如果持續 throttle，考慮切換到 Provisioned 模式並設定足夠的 RCU/WCU
   - 檢查是否有突發大量請求（可能是攻擊或 bot 迴圈）

4. **檢查 GSI**
   ```bash
   aws dynamodb describe-table --table-name bouncer-prod-requests \
     --query 'Table.GlobalSecondaryIndexes[*].{Name:IndexName,Status:IndexStatus}'
   ```

### 預防

- 監控 `ConsumedReadCapacityUnits` / `ConsumedWriteCapacityUnits`
- 設定 TTL 確保舊記錄自動清理
- 考慮加 DAX 快取（如果讀取量大）

---

## 3. Deployer Stuck/Failed 恢復

### 症狀

- `bouncer_deploy_status` 顯示 DEPLOYING 超過 30 分鐘
- Step Function 執行卡住
- deploy lock 未釋放

### 排查步驟

1. **查看部署狀態**
   ```bash
   # 透過 Bouncer API
   mcporter call bouncer bouncer_deploy_status --deployment-id <ID>
   
   # 或直接查 DynamoDB
   aws dynamodb get-item \
     --table-name bouncer-deploy-history \
     --key '{"deployment_id": {"S": "<ID>"}}'
   ```

2. **檢查 Step Function**
   ```bash
   aws stepfunctions describe-execution \
     --execution-arn <EXECUTION_ARN>
   ```

3. **檢查 CloudFormation Stack**
   ```bash
   aws cloudformation describe-stacks --stack-name <STACK_NAME>
   aws cloudformation describe-stack-events --stack-name <STACK_NAME> \
     --max-items 20
   ```

4. **強制取消**
   ```bash
   # 取消 Step Function
   aws stepfunctions stop-execution \
     --execution-arn <EXECUTION_ARN> \
     --cause "Manual intervention - stuck deployment"
   
   # 釋放 deploy lock
   aws dynamodb delete-item \
     --table-name bouncer-deploy-locks \
     --key '{"project_name": {"S": "<PROJECT>"}}'
   ```

5. **CloudFormation Rollback 卡住**
   ```bash
   # 如果 stack 在 UPDATE_ROLLBACK_FAILED
   aws cloudformation continue-update-rollback --stack-name <STACK_NAME>
   
   # 如果還是失敗，跳過問題資源
   aws cloudformation continue-update-rollback \
     --stack-name <STACK_NAME> \
     --resources-to-skip <LOGICAL_RESOURCE_ID>
   ```

### 恢復確認

- Deploy lock 已釋放
- Stack 狀態回到 `*_COMPLETE`
- 後續部署可以正常執行

---

## 4. Telegram Webhook 失效

### 症狀

- 審批按鈕點擊無反應
- Telegram Bot 不回覆
- API Gateway 收不到 webhook 請求

### 排查步驟

1. **檢查 Webhook 狀態**
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo" | python3 -m json.tool
   ```
   確認：
   - `url` 正確指向 API Gateway
   - `last_error_date` 和 `last_error_message` 是否有問題
   - `pending_update_count` 是否積壓

2. **常見問題**
   - **SSL 證書問題**：API Gateway 使用 AWS 託管證書，不應有問題
   - **URL 錯誤**：部署後 API Gateway URL 改變
   - **Secret 不匹配**：`TELEGRAM_WEBHOOK_SECRET` 與設定的不同

3. **重新設定 Webhook**
   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://<API_ID>.execute-api.us-east-1.amazonaws.com/prod/webhook",
       "secret_token": "<WEBHOOK_SECRET>",
       "allowed_updates": ["message", "callback_query"],
       "max_connections": 40
     }'
   ```

4. **測試 Webhook**
   ```bash
   # 發送測試請求到 API Gateway
   curl -X POST "https://<API_ID>.execute-api.us-east-1.amazonaws.com/prod/webhook" \
     -H "Content-Type: application/json" \
     -H "X-Telegram-Bot-Api-Secret-Token: <WEBHOOK_SECRET>" \
     -d '{"update_id": 0, "message": {"text": "test"}}'
   ```

5. **檢查 API Gateway 日誌**
   - CloudWatch Logs → API Gateway execution logs
   - 確認請求有到達且回傳 200

### 預防

- 部署後自動驗證 webhook URL
- 定期檢查 `getWebhookInfo` 的 `last_error_date`

---

## 5. Secret 輪換流程

### 需要輪換的 Secrets

| Secret | 位置 | 影響 |
|--------|------|------|
| `TelegramBotToken` | CloudFormation Parameter | Bot 完全失效 |
| `RequestSecret` | CloudFormation Parameter | API 請求驗證失敗 |
| `TelegramWebhookSecret` | CloudFormation Parameter + Telegram Webhook | Webhook 驗證失敗 |
| GitHub PAT | Secrets Manager (`sam-deployer/github-pat`) | 部署失敗 |

### 輪換步驟

#### TelegramBotToken

1. 在 @BotFather 使用 `/revoke` 獲取新 token
2. 更新 CloudFormation：
   ```bash
   aws cloudformation update-stack \
     --stack-name clawdbot-bouncer \
     --use-previous-template \
     --parameters \
       ParameterKey=TelegramBotToken,ParameterValue=<NEW_TOKEN> \
       ParameterKey=ApprovedChatId,UsePreviousValue=true \
       ParameterKey=RequestSecret,UsePreviousValue=true \
       ParameterKey=TelegramWebhookSecret,UsePreviousValue=true \
       ParameterKey=EnableHmac,UsePreviousValue=true \
       ParameterKey=McpMaxWait,UsePreviousValue=true \
       ParameterKey=DefaultAccountId,UsePreviousValue=true \
     --capabilities CAPABILITY_IAM
   ```
3. 等待 stack 更新完成
4. 重新設定 Telegram Webhook（見上方）

#### RequestSecret

1. 生成新 secret：`openssl rand -hex 32`
2. 更新 CloudFormation（同上，改 `RequestSecret` 參數）
3. 更新所有呼叫方（Clawdbot、其他 Bot）的配置

#### TelegramWebhookSecret

1. 生成新 secret：`openssl rand -hex 32`
2. 更新 CloudFormation
3. 重新設定 Telegram Webhook（帶新 `secret_token`）

#### GitHub PAT

1. 在 GitHub 生成新 PAT
2. 更新 Secrets Manager：
   ```bash
   aws secretsmanager update-secret \
     --secret-id sam-deployer/github-pat \
     --secret-string <NEW_PAT>
   ```

### 注意事項

- ⚠️ Token 輪換會導致短暫服務中斷
- 建議在低峰期進行（UTC 夜間）
- 輪換後驗證所有功能正常
- 更新 1Password 中的備份
