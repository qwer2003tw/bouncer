# Bouncer SAM Deployer

這個模組讓 Bouncer 可以安全地部署 SAM 專案。

## 架構

```
Bouncer → Step Functions → CodeBuild → CloudFormation
              ↓
         Telegram 通知
```

## 資源

- **KMS Key**: 加密所有資源
- **S3 Bucket**: SAM artifacts
- **DynamoDB**: 專案配置、部署歷史、鎖
- **CodeBuild**: 執行 sam build/deploy
- **Step Functions**: 流程編排
- **Notifier Lambda**: Telegram 通知

## 部署

### 1. 建立 GitHub PAT Secret

```bash
aws secretsmanager create-secret \
  --name sam-deployer/github-pat \
  --secret-string "ghp_your_token_here" \
  --region us-east-1
```

### 2. 建立 Bouncer Secrets (用於部署 Bouncer 自己)

```bash
aws secretsmanager create-secret \
  --name sam-deployer/projects/bouncer \
  --secret-string '{
    "TelegramBotToken": "xxx:yyy",
    "RequestSecret": "abc123",
    "TelegramWebhookSecret": "def456"
  }' \
  --region us-east-1
```

### 3. 部署 Deployer Stack

```bash
cd deployer
sam build
sam deploy \
  --stack-name bouncer-deployer \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    TelegramBotToken=xxx:yyy \
    TelegramChatId=999999999
```

## MCP Tools

| Tool | 說明 |
|------|------|
| `bouncer_deploy` | 觸發部署（需審批）|
| `bouncer_deploy_status` | 查詢狀態 |
| `bouncer_deploy_cancel` | 取消部署 |
| `bouncer_deploy_logs` | 查看 logs |
| `bouncer_project_list` | 列出專案 |
| `bouncer_project_add` | 新增專案（需審批）|

## Cross-Account 部署

讓 Bouncer Deployer 可以部署到其他 AWS 帳號。

### 在目標帳號建立 BouncerDeployerRole

```bash
# 方式一：Admin 權限（簡單，適合內部/開發帳號）
aws cloudformation deploy \
  --template-file cross-account-role.yaml \
  --stack-name bouncer-deployer-role \
  --parameter-overrides \
    BouncerAccountId=YOUR_BOUNCER_ACCOUNT_ID \
    PermissionLevel=admin \
  --capabilities CAPABILITY_NAMED_IAM

# 方式二：Scoped 權限（最小權限，適合生產環境）
aws cloudformation deploy \
  --template-file cross-account-role.yaml \
  --stack-name bouncer-deployer-role \
  --parameter-overrides \
    BouncerAccountId=YOUR_BOUNCER_ACCOUNT_ID \
    PermissionLevel=scoped \
  --capabilities CAPABILITY_NAMED_IAM
```

| 模式 | 權限 | 適用場景 |
|------|------|---------|
| `admin` | AdministratorAccess | 內部開發帳號、快速上手 |
| `scoped` | 最小權限（CloudFormation + Lambda + API Gateway + DynamoDB + CloudFront + WAF + IAM role 管理等） | 生產環境、需要嚴格控管 |

兩種模式都有 **Deny 保護**：禁止建立 IAM User、Access Key、修改 Organizations 等危險操作。

### 在 Bouncer 註冊帳號

```bash
mcporter call bouncer.bouncer_add_account \
  account_id="TARGET_ACCOUNT_ID" \
  name="帳號別名" \
  role_arn="arn:aws:iam::TARGET_ACCOUNT_ID:role/BouncerDeployerRole" \
  source="你的Bot名稱"
```

## 安全

- Permission Boundary 限制 SAM 建立的 Role
- S3 Bucket 禁止公開存取
- 所有資料 KMS 加密
- Cross-account 需要 ExternalId
