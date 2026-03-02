---
name: bouncer-exec
description: >
  Execute AWS CLI commands via Bouncer with clean output. Use when user says
  "bouncer aws s3 ls" or similar aws commands via Bouncer. Syntax: bouncer {aws command}.
  Auto-polls for approval, outputs only the command result without JSON wrapper.
metadata:
  bouncer_version: "3.8.0"
---

# Bouncer Exec

Execute AWS CLI commands through Bouncer with a simple syntax and clean output.

## Trigger

When the user message starts with `bouncer` followed by an AWS command:

```
bouncer aws s3 ls
bouncer aws sts get-caller-identity
bouncer aws ec2 describe-instances --region us-east-1
```

## Workflow

### 1. Run the command

```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh <aws command>
```

Example:
```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh aws s3 ls
```

### 2. What the script does

1. Calls `mcporter call bouncer bouncer_execute` with:
   - `command` = the full AWS command
   - `reason` = the command itself (or custom `--reason`)
   - `source` = `"Agent (<command>)"`
   - `trust_scope` = `"agent-bouncer-exec"`
2. If status is `auto_approved` or `trust_auto_approved` → prints result immediately
3. If status is `pending_approval` → polls `bouncer_status` every 10 seconds (up to 10 minutes)
   - `approved` → prints result
   - `rejected` → prints `❌ 請求被拒絕`
   - Still `pending_approval` after timeout → prints `⏰ 等待審批超時（10 分鐘）`
4. Output is clean: only the command result, no JSON wrapper
5. JSON results are pretty-printed; escaped newlines are unescaped

### 3. Display the output

Show the script output directly to the user. No additional formatting needed — the script handles cleanup.

### 4. Optional parameters

Pass `--reason "custom reason"` before the aws command:
```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh --reason "檢查 bucket 列表" aws s3 ls
```

Pass `--account <account_id>` for cross-account:
```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh --account 992382394211 aws s3 ls
```

## References

- [Bouncer API Reference](references/bouncer-api.md) — tool schemas, parameters, response formats
