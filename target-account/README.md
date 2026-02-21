# Bouncer Target Account Setup

Deploy this CloudFormation stack **once** in each target AWS account to enable Bouncer operations.

## What It Creates

| Resource | Purpose |
|----------|---------|
| `BouncerRole` | Unified IAM role for execute, deploy, and upload |
| `bouncer-uploads-{AccountId}` | S3 bucket for file uploads (Block Public Access, 30-day lifecycle) |

## Quick Start

```bash
# Deploy in the target account
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bouncer-target-account \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides BouncerAccountId=190825685292

# Get the role ARN from outputs
aws cloudformation describe-stacks \
  --stack-name bouncer-target-account \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' \
  --output text

# Register in Bouncer
mcporter call bouncer bouncer_add_account \
  --account_id <ACCOUNT_ID> \
  --name "My Account" \
  --role_arn <ROLE_ARN>
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `BouncerAccountId` | `190825685292` | Central Bouncer account ID |
| `RoleName` | `BouncerRole` | IAM role name |
| `UploadBucketLifecycleDays` | `30` | Auto-delete uploads after N days (0 to disable) |

## Security

- **PowerUser with guardrails**: Can do most operations, but dangerous IAM actions are explicitly denied
- **Denied actions**: CreateUser, DeleteUser, CreateAccessKey, MFA manipulation, Organizations, self-escalation
- **Trust policy**: Only allows assume from specific Bouncer roles in the central account (Lambda + CodeBuild)
- **S3 bucket**: Block Public Access enforced, server-side encryption enabled

## After Deployment

The stack outputs include:
- `RoleArn` — Use this when registering the account in Bouncer
- `UploadBucketName` — Automatically used by Bouncer upload
- `AddAccountCommand` — Copy-paste command to register the account
