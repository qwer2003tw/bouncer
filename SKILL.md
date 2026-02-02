---
name: bouncer
description: Execute AWS CLI commands with Telegram approval. Safe commands auto-execute, dangerous commands require human approval via Telegram.
metadata: {"openclaw": {"emoji": "🔐", "requires": {"bins": ["mcporter"]}}}
---

# Bouncer - AWS Command Approval System

Use `mcporter` to execute AWS CLI commands through the Bouncer approval system.

## Available Tools

### bouncer_execute
Execute an AWS CLI command. Safe commands (describe/list/get) auto-execute. Dangerous commands require Telegram approval.

```bash
mcporter call bouncer.bouncer_execute command="<aws command>" reason="<why you need this>" source="<your name>"
```

**Parameters:**
- `command` (required): AWS CLI command (e.g., `aws ec2 describe-instances`)
- `reason` (required): Explain why you need this command - this is shown to the approver
- `source` (required): Identify yourself - use your agent name from IDENTITY.md or SOUL.md
- `account` (optional): Target AWS account ID (12 digits), defaults to the main account
- `timeout` (optional): Approval timeout in seconds (default: 300)

### bouncer_status
Check the status of an approval request.

```bash
mcporter call bouncer.bouncer_status request_id="<id>"
```

### bouncer_list_accounts
List configured AWS accounts.

```bash
mcporter call bouncer.bouncer_list_accounts
```

### bouncer_add_account
Add or update an AWS account configuration (requires Telegram approval).

```bash
mcporter call bouncer.bouncer_add_account account_id="111111111111" name="Production" role_arn="arn:aws:iam::111111111111:role/BouncerRole"
```

**Parameters:**
- `account_id` (required): AWS account ID (12 digits)
- `name` (required): Account name (e.g., Production, Staging)
- `role_arn` (required): IAM Role ARN that Bouncer will assume

### bouncer_remove_account
Remove an AWS account configuration (requires Telegram approval).

```bash
mcporter call bouncer.bouncer_remove_account account_id="111111111111"
```

## Command Classification

| Type | Behavior | Examples |
|------|----------|----------|
| **BLOCKED** | Always rejected | `iam create-*`, shell injection |
| **SAFELIST** | Auto-execute | `describe-*`, `list-*`, `get-*` |
| **APPROVAL** | Requires Telegram approval | `start-*`, `stop-*`, `delete-*`, `create-*` |

## Multi-Account Support

Bouncer supports multiple AWS accounts. The default account (where Lambda runs) doesn't require a role. Other accounts need:

1. A role in the target account that trusts the Bouncer Lambda
2. The role registered via `bouncer_add_account`

### Example: Execute in a different account
```bash
mcporter call bouncer.bouncer_execute command="aws s3 ls" reason="檢查 Production 的 S3" account="111111111111"
```

## Examples

### List S3 buckets (auto-approved)
```bash
mcporter call bouncer.bouncer_execute command="aws s3 ls" reason="檢查現有的 S3 buckets" source="<your-name>"
```

### Start an EC2 instance (requires approval)
```bash
mcporter call bouncer.bouncer_execute command="aws ec2 start-instances --instance-ids i-xxx" reason="Steven 要求啟動開發環境" source="<your-name>"
```

### Execute in a different account
```bash
mcporter call bouncer.bouncer_execute command="aws lambda list-functions" reason="檢查 Production Lambda" account="111111111111" source="<your-name>"
```

## Important Notes

1. **Always provide source** - Identify yourself so Steven knows who's requesting
2. **Always provide a clear reason** - The approver sees this in Telegram
3. **Wait for response** - Approval commands will block until approved/denied/timeout
4. **Check the result** - Even approved commands may fail due to AWS permissions
5. **Multi-account** - Use `account` parameter to target different AWS accounts
