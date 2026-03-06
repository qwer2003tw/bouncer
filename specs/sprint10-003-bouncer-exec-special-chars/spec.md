# Sprint 10-003: bug: bouncer_exec.sh pipe + --reason parsing issues

> GitHub Issues: #49, #51
> Priority: P1
> Generated: 2026-03-03

---

## Bug Name

bouncer_exec.sh Special Character Handling — `|` pipe 字元被 shell 解析截斷 AWS CLI 參數；`--reason` 參數在特定位置與 AWS CLI 參數解析衝突。

## Root Cause Analysis

### Issue #49: Pipe `|` 被截斷

**問題代碼**（`bouncer_exec.sh` L40）：
```bash
COMMAND="$*"
```

`$*` 把所有 positional parameters 用 `IFS` 的第一個字元（空格）join 起來。但在呼叫時如果 shell 已經把 `|` 解析為 pipe，到這裡已經截斷了。

**實際 root cause**：不在 `bouncer_exec.sh` 本身，而是**呼叫方**沒有正確 escape。`set -euo pipefail` 在 bouncer_exec.sh 內生效，但如果 agent 呼叫時寫：
```bash
bash bouncer_exec.sh aws logs start-query --query-string 'fields @timestamp | filter ...'
```

Shell 會在 exec 之前就把 `|` 解析為 pipe operator。即使有 single quotes，如果 agent 的 exec command 是用 `"..."` 包裝整條命令，內部的 single quotes 會被吃掉。

**真正的修復**：`bouncer_exec.sh` 改用 `COMMAND` 的構建方式——保留 argv 的 quoting，而非用 `$*` 把 argv 攤平成一個字串。

### Issue #51: --reason 參數衝突

**問題代碼**（`bouncer_exec.sh` L24-28）：
```bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)
      REASON="$2"
      shift 2
      ;;
```

問題場景：
```bash
bash bouncer_exec.sh aws s3 ls --reason "查看 S3"
```

如果 AWS CLI 自己也有 `--reason` 參數（如 `aws kms` 系列），或 `--reason` 出現在 AWS 命令 arguments 之間（不在開頭），while loop 的 `*) break ;;` 會在碰到第一個不認識的 token（`aws`）時 break → `--reason` 留在後面 → 被 `$*` 包進 COMMAND → AWS CLI 報 `Unknown options`。

但看目前的 code，`--reason` 和 `--account` 必須出現在 **aws 命令之前**（while 碰到非 `--reason`/`--account` 的 token 就 break）。問題是：agent 可能把 `--reason` 放在命令後面。

## User Stories

**US-1: 含 pipe 的 AWS CLI 命令**
As an **AI agent**,
I want to execute CloudWatch Insights queries containing `|` via bouncer_exec.sh,
So that I can use the full power of CloudWatch Insights without workaround.

**US-2: --reason 位置自由**
As an **AI agent**,
I want `--reason` to work regardless of its position in the command line,
So that I don't need to remember a specific argument order.

## Acceptance Scenarios

### Scenario 1: CloudWatch Insights query with pipe
- **Given**: Agent 執行 `bouncer_exec.sh aws logs start-query --query-string 'fields @timestamp, message | filter message like /error/ | limit 20' --log-group-name /app/log --start-time 123 --end-time 456`
- **When**: bouncer_exec.sh 解析 argv
- **Then**: COMMAND 完整包含 `fields @timestamp, message | filter message like /error/ | limit 20`
- **And**: `|` 不被 shell 解析為 pipe

### Scenario 2: --reason at end
- **Given**: Agent 執行 `bouncer_exec.sh aws s3 ls --reason "查看 S3"`
- **When**: bouncer_exec.sh 解析 argv
- **Then**: COMMAND = `aws s3 ls`（不包含 `--reason "查看 S3"`）
- **And**: REASON = `查看 S3`

### Scenario 3: --reason at beginning（現有行為保留）
- **Given**: Agent 執行 `bouncer_exec.sh --reason "測試" aws s3 ls`
- **When**: bouncer_exec.sh 解析 argv
- **Then**: COMMAND = `aws s3 ls`, REASON = `測試`

### Scenario 4: 無 --reason
- **Given**: Agent 執行 `bouncer_exec.sh aws s3 ls`
- **When**: bouncer_exec.sh 解析 argv
- **Then**: COMMAND = `aws s3 ls`, REASON = `aws s3 ls`（fallback 預設）

## Requirements

- **R1**: 支援 `--reason` 在 AWS 命令之前或之後
- **R2**: AWS CLI 參數中的 `|` 不被 shell 解析為 pipe
- **R3**: COMMAND 構建保留 argv 的 quoting（每個 arg 個別 quote）
- **R4**: 向後兼容 — 現有不含特殊字元的呼叫方式不受影響
- **R5**: `--account` 同樣支援前後位置

## Technical Notes

### COMMAND 構建方式改進

**現有**：`COMMAND="$*"` → 攤平所有 arg，丟失 quoting。

**改進**：
```bash
# 先 strip --reason / --account from any position
# 用 array 保留每個 arg 的 quoting
AWS_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason) REASON="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    *) AWS_ARGS+=("$1"); shift ;;
  esac
done
# 傳給 mcporter 時每個 arg 個別 quote
COMMAND=$(printf '%q ' "${AWS_ARGS[@]}")
```

注意：最終 COMMAND 作為字串傳給 mcporter → Bouncer Lambda → `execute_command()` → subprocess。Bouncer Lambda 端用 `shlex.split()` 拆分。所以 `printf '%q '` 確保 quoting 正確。
