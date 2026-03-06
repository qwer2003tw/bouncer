# Sprint 14-005: bouncer_exec.sh rate_limited 無聲失敗

> GitHub Issue: #73
> Priority: P1
> TCS: 3
> Generated: 2026-03-06

---

## Problem Statement

Bouncer Lambda 回傳 `rate_limited` 或 `pending_limit_exceeded` 狀態時，`bouncer_exec.sh` 走到 `*)` fallback branch，輸出含糊的錯誤訊息：

```
Error: unexpected status: rate_limited
```

這個訊息讓 caller（Agent）以為是程式錯誤而非 rate limit，導致：
- Agent 誤判為命令無回應或 log 不存在
- 花大量時間排查錯誤方向
- 最終才發現是 Bouncer rate limit 靜默擋掉

### 現況

```bash
# bouncer_exec.sh (line ~149-152)
*)
  echo "Error: unexpected status: ${STATUS}" >&2
  echo "$RESPONSE" >&2
  exit 1
  ;;
```

同樣的問題也出現在 poll loop 中（line ~136）：
```bash
*)
  echo "Error: unexpected poll status: ${POLL_STATUS}" >&2
  echo "$POLL_RESPONSE" >&2
  exit 1
  ;;
```

## Root Cause

`bouncer_exec.sh` 的 case statement 只處理了 `auto_approved|trust_auto_approved|approved`、`pending_approval`、`rejected` 三種已知狀態，沒有針對 `rate_limited` 和 `pending_limit_exceeded` 做明確處理。

## User Stories

**US-1: 明確的 rate limit 錯誤訊息**
As an **MCP client (Agent)**,
I want `bouncer_exec.sh` to output a clear rate limit message,
So that I know to wait and retry instead of investigating a phantom error.

**US-2: poll 中的 rate limit 處理**
As an **MCP client (Agent)**,
I want the poll loop to handle rate_limited gracefully,
So that a temporary rate limit during polling doesn't abort the entire request.

## Scope

### 變更 1: 主 case statement 加入 rate_limited 處理

**檔案：** `skills/bouncer-exec/scripts/bouncer_exec.sh` (line ~149)

在 `*)` 前加入明確的 case：

```bash
rate_limited)
  RETRY_AFTER=$(echo "$RESPONSE" | jq -r '.retry_after // "unknown"' 2>/dev/null)
  echo "⛔ Rate limit：同一來源請求過於頻繁，請等待 ${RETRY_AFTER} 秒後再試" >&2
  exit 1
  ;;
pending_limit_exceeded)
  echo "⛔ Pending limit：已有太多等待審批的請求，請先等待現有請求完成" >&2
  exit 1
  ;;
```

### 變更 2: poll loop 加入 rate_limited 處理

**檔案：** `skills/bouncer-exec/scripts/bouncer_exec.sh` (line ~136)

poll loop 中 `rate_limited` 應該重試而非退出：

```bash
rate_limited)
  # Rate limited during poll — wait longer and retry
  sleep "$POLL_INTERVAL"
  ;;
```

### 變更 3: 改善 poll loop 的 unexpected status 訊息

```bash
*)
  echo "Error: unexpected poll status: ${POLL_STATUS}" >&2
  echo "Full response: ${POLL_RESPONSE}" >&2
  exit 1
  ;;
```

## Out of Scope

- 修改 Bouncer Lambda 的 rate limit 邏輯
- 加入自動 retry 機制（rate limit 時直接失敗，讓 caller 決定是否重試）
- 修改 MCP 層的 rate limit 回傳格式

## Test Plan

### Manual Testing

| # | 測試 | 驗證 |
|---|------|------|
| T1 | 模擬 `{"status":"rate_limited","retry_after":10}` response | 輸出 `⛔ Rate limit` 訊息 |
| T2 | 模擬 `{"status":"pending_limit_exceeded"}` response | 輸出 `⛔ Pending limit` 訊息 |
| T3 | poll 中收到 rate_limited | 不退出，繼續 poll |
| T4 | 正常 auto_approved/pending_approval 流程 | 不受影響 |

### Shell Script Testing

```bash
# 可以用 mock mcporter 測試
echo '{"status":"rate_limited","retry_after":10}' | bash -c '
  STATUS="rate_limited"
  RESPONSE=$(cat)
  case "$STATUS" in
    rate_limited)
      RETRY_AFTER=$(echo "$RESPONSE" | jq -r ".retry_after // \"unknown\"")
      echo "⛔ Rate limit：請等待 ${RETRY_AFTER} 秒" >&2
      ;;
  esac
'
```

## Acceptance Criteria

- [ ] `rate_limited` 狀態輸出中文明確訊息（含 retry_after 秒數）
- [ ] `pending_limit_exceeded` 狀態輸出中文明確訊息
- [ ] poll loop 中的 `rate_limited` 不中斷 polling
- [ ] 既有正常流程不受影響
- [ ] exit code 1（rate limit = 錯誤，caller 應處理）
