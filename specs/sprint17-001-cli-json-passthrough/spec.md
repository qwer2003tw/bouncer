# Sprint 17-001: CLI JSON pass-through + --reason 驗證修復

> GitHub Issues: #85, #51
> Priority: P0
> TCS: 5
> Generated: 2026-03-08

---

## Problem Statement

`bouncer_exec.sh` 有兩個互相關聯的問題：

### #85 — pipe `|` 被 shell 截斷
Bash 在腳本接收 args 之前就把 `|` 解析成 pipe，導致 CloudWatch Logs `--query-string 'fields @timestamp | filter ...'` 中 pipe 後面的部分被截斷。`"$*"` 拿到的已是截斷後的 args，Sprint 16 的 jq 序列化無法救回。

### #51 — --reason 參數位置衝突
當使用者把 `--reason` 放在 AWS 命令後面（`bash bouncer_exec.sh aws s3 ls --reason "..."`），`--reason` 不會被消費，而是洩漏到 AWS CLI 的 argv，導致 `Unknown options` 錯誤。

## Root Cause

1. **#85**: `$*` 接收到的字串已被 shell pipe 截斷。任何 quoting 方案都無法避免 shell 在 script 層級的 pipe 分割。
2. **#51**: `while` loop 遇到非 `--reason`/`--account`/`--source` 的 token 就 `break`，但 `--reason` 只在命令前面被 parse。放後面時它被包進 `$*` 送給 AWS CLI。

## Scope

### 變更 1: 新增 `--json-args` 模式（修 #85）

**檔案：** `skills/bouncer-exec/scripts/bouncer_exec.sh`

在 args parsing `while` loop 新增 `--json-args` flag：

```bash
    --json-args)
      JSON_ARGS="$2"
      shift 2
      ;;
```

當 `--json-args` 有值時：
- 跳過 `$*` → `COMMAND_DISPLAY` 組裝
- 從 JSON 提取 `command`：`COMMAND_DISPLAY=$(echo "$JSON_ARGS" | jq -r '.command')`
- 直接合併 JSON_ARGS 到 MCPORTER_JSON（保留 reason/source/trust_scope 等既有欄位）
- 仍然驗證 `--reason`（必填、15 字、非命令字串）

**使用方式：**
```bash
bash bouncer_exec.sh --reason "查詢 Lambda error log" --json-args '{
  "command": "aws logs start-query --query-string \"fields @timestamp | filter level = ERROR\""
}'
```

### 變更 2: 全位置 --reason 解析（修 #51）

**檔案：** `skills/bouncer-exec/scripts/bouncer_exec.sh`

**方案：** 兩階段解析
1. 第一輪：掃描所有 args，提取 `--reason`、`--account`、`--source`（無論位置）
2. 第二輪：剩餘 args 組成 `COMMAND_DISPLAY`

```bash
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)  REASON="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --source)  CUSTOM_SOURCE="$2"; shift 2 ;;
    --json-args) JSON_ARGS="$2"; shift 2 ;;
    *)         ARGS+=("$1"); shift ;;
  esac
done
COMMAND_DISPLAY="${ARGS[*]}"
```

### 變更 3: reason 驗證強化

現有驗證保持不變（必填、≥15 字、不能以 `aws ` 開頭），新增：
- 當 `--json-args` 模式時，reason 仍然從 `--reason` flag 取得（不從 JSON 裡取）
- 保持 `_validate_reason()` 函數不變

## Out of Scope

- mcporter 本身的 JSON 傳輸（已正常）
- bouncer_execute Lambda 端（不改）
- source 動態化（#72，另開 task）

## Test Plan

### 手動測試

| # | 測試 | 預期 |
|---|------|------|
| T1 | `--json-args` 帶 pipe 字元 | pipe 完整傳到 Bouncer，不被 shell 截斷 |
| T2 | `--reason` 放在 `aws` 命令後面 | reason 被正確消費，不洩漏到 AWS CLI |
| T3 | `--reason` 放在 `aws` 命令前面 | 行為不變（向後兼容） |
| T4 | 不帶 `--reason` | exit 1 + 錯誤訊息 |
| T5 | `--json-args` + 不帶 `--reason` | exit 1 + 錯誤訊息 |
| T6 | `--json-args` 帶 `@` 和 `\` | 特殊字元完整傳送 |

### 自動化測試（shell script test）

建立 `skills/bouncer-exec/tests/test_bouncer_exec.sh`：
- Mock `mcporter` 命令，驗證傳入的 JSON args 格式正確
- 驗證 reason validation 各 case
- 驗證 `--reason` 在命令前/後都能被正確提取

## Acceptance Criteria

- [ ] `--json-args` 模式：pipe `|`、`@`、`\`、`"` 等特殊字元完整傳送到 Bouncer
- [ ] `--reason` 放在命令前/中/後任意位置都能被正確提取
- [ ] 向後兼容：不使用 `--json-args` 的呼叫行為不變
- [ ] reason 驗證規則不變（必填、≥15 字、非命令字串）
- [ ] 新增 shell test 覆蓋 6 個 case
