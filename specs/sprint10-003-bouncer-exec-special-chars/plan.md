# Sprint 10-003: Plan — bouncer_exec.sh pipe + --reason parsing

> Generated: 2026-03-03

---

## Technical Context

### 現狀分析

1. **`bouncer_exec.sh`**（`~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh`）：
   - L15-33: while loop 解析 `--reason` 和 `--account`，碰到其他 token 就 break
   - L40: `COMMAND="$*"` 攤平剩餘 args
   - L71-79: 構建 mcporter args array，COMMAND 作為 `command=${COMMAND}` 傳入

2. **Bouncer Lambda 端**：`commands.py` 的 `execute_command()` 接收 command string → `_split_chain()` 拆 `&&` → 每個子命令用 `shlex.split()` 拆分 → `subprocess.run()`

3. **mcporter 傳輸**：`command=<string>` → HTTP body → Lambda 收到完整字串

### 問題定位

- `$*` 攤平 → 丟失 quoting → 含空格的引號參數被拆開
- `--reason` 只能在命令前面 → 放後面會留在 COMMAND 裡
- 呼叫端 shell 解析 `|` → bouncer_exec.sh 收到的 argv 已被截斷

### 影響範圍

- `~/.openclaw/workspace/skills/bouncer-exec/scripts/bouncer_exec.sh` — 唯一修改檔案
- 不影響 Bouncer Lambda（command string 格式不變）

## Implementation Phases

### Phase 1: 重寫 argument parsing

1. 改用全 argv 掃描（不 break），strip `--reason` / `--account` from any position：
```bash
AWS_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason) REASON="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    *) AWS_ARGS+=("$1"); shift ;;
  esac
done
```

2. COMMAND 構建：
```bash
# 用 printf '%q' 保留每個 arg 的 quoting
COMMAND=$(printf '%q ' "${AWS_ARGS[@]}")
COMMAND="${COMMAND% }"  # 去尾空格
```

3. REASON fallback：
```bash
REASON="${REASON:-${AWS_ARGS[*]}}"
```

### Phase 2: 文件更新

1. Usage comment 更新：說明 `--reason` 和 `--account` 可出現在任何位置
2. 加入 pipe 使用範例到 comment

### Phase 3: 測試（手動驗證，此為 client-side shell script）

測試 cases：
1. `bouncer_exec.sh aws s3 ls` — 基本命令
2. `bouncer_exec.sh --reason "test" aws s3 ls` — reason 在前
3. `bouncer_exec.sh aws s3 ls --reason "test"` — reason 在後
4. `bouncer_exec.sh aws logs start-query --query-string 'fields @timestamp | limit 20'` — pipe 字元
5. `bouncer_exec.sh --account 992382394211 aws s3 ls --reason "cross account"` — 混合

### 關於 pipe 的限制

即使 bouncer_exec.sh 內部正確處理，如果 agent 用 `exec("bash bouncer_exec.sh ... | ...")` 呼叫，shell 仍會截斷。這需要 agent 端 escape（如 `\|` 或用 `$'...'`）。bouncer_exec.sh 本身無法解決這個問題——需在 SKILL.md 加使用說明。
