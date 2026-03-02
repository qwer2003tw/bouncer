# Bouncer API Reference

## MCP Server

- **Name:** `bouncer`
- **Transport:** STDIO
- **Command:** `python3 /home/ec2-user/projects/bouncer/bouncer_mcp.py`
- **CLI:** `mcporter call bouncer <tool_name> key=value ...`

---

## Tools

### bouncer_execute

Execute an AWS CLI command. Safe commands auto-approve; dangerous commands require Telegram approval.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `command` | string | ✅ | AWS CLI command (e.g. `aws ec2 describe-instances`) |
| `reason` | string | ✅ | Reason shown in approval request |
| `trust_scope` | string | ✅ | Trust scope identifier for Trust Session matching |
| `source` | string | ❌ | Source description (e.g. `Private Bot (Bouncer 部署)`) |
| `account` | string | ❌ | Target AWS account ID (12 digits). Default account if omitted |
| `sync` | boolean | ❌ | Synchronous mode: wait for approval (may timeout). Default `false` |
| `grant_id` | string | ❌ | Grant Session ID for pre-approved batch execution |

**Response:**

```json
{
  "status": "auto_approved" | "pending_approval" | "trust_auto_approved",
  "result": "command output (when auto-approved)",
  "request_id": "uuid (when pending_approval)",
  "command": "aws ...",
  "account": "190825685292",
  "account_name": "2nd"
}
```

**Status values (initial):**
- `auto_approved` — safe command, result included immediately
- `trust_auto_approved` — matched an active trust session, result included
- `pending_approval` — sent to Telegram for Steven's approval, poll with `bouncer_status`

---

### bouncer_status

Poll for approval status of a pending request.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `request_id` | string | ✅ | Request ID from `bouncer_execute` |

**Response:**

```json
{
  "status": "approved" | "rejected" | "pending_approval",
  "result": "command output (when approved)",
  "request_id": "uuid"
}
```

**Status values (poll):**
- `approved` — Steven approved, `result` contains command output
- `rejected` — Steven rejected the request
- `pending_approval` — still waiting for approval

---

### bouncer_help

Get AWS CLI parameter documentation without executing anything.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `command` | string | ❌ | AWS CLI command (e.g. `ec2 describe-instances`) |
| `service` | string | ❌ | List all operations for a service (e.g. `ec2`) |

---

## AWS Accounts

| Name | Account ID | Access |
|------|-----------|--------|
| 2nd (Default) | 190825685292 | Lambda execution role |
| Dev | 992382394211 | Assume `BouncerExecutionRole` |
| 1st | 841882238387 | Assume `BouncerExecutionRole` |

Use `account` parameter in `bouncer_execute` to target non-default accounts.

---

## Usage Examples

```bash
# Simple command (auto-approved safe command)
mcporter call bouncer bouncer_execute \
  command="aws sts get-caller-identity" \
  reason="Check identity" \
  source="Agent (aws sts get-caller-identity)" \
  trust_scope="agent-bouncer-exec"

# Cross-account
mcporter call bouncer bouncer_execute \
  command="aws s3 ls" \
  reason="List buckets in Dev" \
  source="Agent (aws s3 ls)" \
  trust_scope="agent-bouncer-exec" \
  account="992382394211"

# Poll pending request
mcporter call bouncer bouncer_status \
  request_id="abc-123-def"
```
