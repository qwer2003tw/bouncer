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

⚠️ **`--reason` 是必填參數**，必須說明「為什麼要執行」，不能是命令字串本身。

```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh --reason "<原因>" [--source "<來源>"] <aws command>
```

Example:
```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh \
  --reason "排查 ZTP Files API 500 錯誤，查詢最近 1 小時的 Lambda log" \
  aws logs filter-log-events --log-group /aws/lambda/ztp-files-api
```

### reason 規範（強制）

| 規則 | 說明 |
|------|------|
| 必填 | 不帶 `--reason` → 直接 exit 1 |
| `--source` | 選填 | 自訂審批通知來源（預設 `Agent (命令)`）|
| 最少 15 字 | 太短 → exit 1 |
| 不能是命令字串 | reason ≈ command 或 reason 以 `aws ` 開頭 → exit 1 |

```
✅ --reason "排查 Lambda 冷啟動問題，確認記憶體配置"
✅ --reason "查詢 ZTP Files API 最近 1 小時的錯誤 log，定位 500 根因"
❌ --reason "aws logs filter-log-events ..."   ← 命令字串，拒絕
❌ --reason "查 log"                            ← 太短，拒絕
❌ （不帶 --reason）                            ← 必填，拒絕
```

### 2. What the script does

1. Calls `mcporter call bouncer bouncer_execute` with:
   - `command` = the full AWS command
   - `reason` = **必填，由呼叫者提供的人類可讀說明**
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

Pass `--reason "custom reason"` before the aws command（**必填**）:

Pass `--source "Source Name"` to customize the requester label shown in Telegram（選填，預設 `Agent`）:
```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh \
  --reason "排查 ZTP Files Lambda 記憶體使用，確認是否需要調整" \
  --source "Private Bot (ZTP Files - 排查)" \
  aws lambda get-function-configuration --function-name ztp-files-api
```

Pass `--account <account_id>` for cross-account:
```bash
bash ~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh --account 992382394211 aws s3 ls
```

### ⚠️ && 串接限制

`&&` 只支援串接 **aws 命令**，不能串接 shell 命令（echo、grep 等）：

```bash
# ✅ 正確：兩個都是 aws 命令
bash bouncer_exec.sh --reason "..." aws s3 ls && aws sts get-caller-identity

# ❌ 錯誤：echo 不是 aws 命令
bash bouncer_exec.sh --reason "..." aws s3 ls && echo DONE
# → 第一個命令會執行，但整體顯示 ❌（#96 待修）
```

**重要**：如果誤用了非 aws 命令串接，第一個 aws 命令**可能已經執行成功**，即使顯示 ❌ 也不代表沒執行。不要重複執行危險命令（delete/terminate）。

## References

- [Bouncer API Reference](references/bouncer-api.md) — tool schemas, parameters, response formats

---

## ⚠️ Deploy 追蹤規則（不適用 bouncer_exec.sh）

`bouncer_deploy` / `bouncer_deploy_status` 是 MCP-only tools，不走 `bouncer_exec.sh`，需用 `mcporter` 直接呼叫。

**Deploy 請求發出後必須 spawn sub-agent 追蹤，不能在主 session 跑 poll loop：**

⚠️ **`bouncer_deploy` 回傳 `request_id`，不是 `deploy_id`！**
`bouncer_deploy_status` 需要 `deploy_id`，必須從 `bouncer_deploy_history` 取得。

```python
# ✅ 正確：spawn sub-agent 追蹤
sessions_spawn(task="""
# Step 1: 等批准後取 deploy_id
for i in range(15):
    sleep(20)
    history = mcporter call bouncer bouncer_deploy_history project="{project}" limit=1
    if history 最新一筆時間 > {request_sent_time}:
        deploy_id = history.history[0].deploy_id
        break
if not deploy_id: message("⏳ 5 分鐘無 deploy_id") → exit（不重發）

# Step 2: poll deploy_id
for i in range(15):
    sleep(20)
    STATUS = mcporter call bouncer bouncer_deploy_status deploy_id="{deploy_id}"
    SUCCESS → message 回報 → exit
    FAILED  → message 回報原因 → exit
message("⏳ timeout") → exit（只回報主 session，不問 Steven）
""")

# ❌ 錯誤：用 request_id 查 deploy_status（永遠是 pending）
bouncer_deploy_status deploy_id="{request_id}"  # ← 這樣查不到！
```

**注意：**
- 只看 `status` 欄位（RUNNING / SUCCESS / FAILED）
- 不看 `phase`（永遠是 INITIALIZING，bug #53）
- 不知道前一個請求狀態，不能自己重發
