# Sprint 9-006: fix: base64 CLI args 超長

> GitHub Issue: #37
> Priority: P2
> Generated: 2026-03-02

---

## Feature Name

Base64 CLI Argument Length Fix — 解決 `mcporter call bouncer` 透過 CLI 傳遞大型 base64 content 時引數超長的問題。

## Background

當 Agent 使用 mcporter CLI 呼叫 `bouncer_upload` 或 `bouncer_upload_batch` 時，base64 content 作為 JSON argument 傳入。大檔案的 base64 string 可能導致：

1. **OS arg length limit**：Linux `ARG_MAX` 通常 2MB，但 shell expansion 和 environment 可能更低
2. **Shell escape 問題**：大量 base64 字元可能包含 shell special chars
3. **mcporter CLI 的 `--args` JSON parsing**：超長 JSON string 可能有 performance 問題

目前 TOOLS.md 已記載使用 HTTP API 直呼繞過 CLI arg length limit 的 workaround。但這不是正式解法。

## User Stories

**US-1: CLI 大檔案上傳**
As an **AI agent**,
I want to upload files > 100KB via mcporter CLI without hitting argument length limits,
So that I can use the standard tool interface for all file sizes.

## Acceptance Scenarios

### Scenario 1: mcporter 支援 stdin/file input
- **Given**: Agent 有一個 500KB base64 file 要上傳
- **When**: 使用 `mcporter call bouncer bouncer_upload --args-file /tmp/upload.json`
- **Then**: mcporter 從檔案讀取 JSON args
- **And**: 正常呼叫 Bouncer API

### Scenario 2: mcporter 支援 stdin pipe
- **Given**: Agent 有一個 500KB base64 file 要上傳
- **When**: 使用 `echo '{"content":"..."}' | mcporter call bouncer bouncer_upload --args-stdin`
- **Then**: mcporter 從 stdin 讀取 JSON args
- **And**: 正常呼叫 Bouncer API

### Scenario 3: 小檔案維持現有行為
- **Given**: Agent 有一個 10KB file
- **When**: 使用 `mcporter call bouncer bouncer_upload --args '{"content":"..."}'`
- **Then**: 行為不變，正常上傳

### Scenario 4: HTTP direct call（現有 workaround）
- **Given**: Agent 直接呼叫 Bouncer HTTP API
- **When**: POST JSON body 到 `/mcp`
- **Then**: 不受 CLI arg limit 影響（已有功能）

## Edge Cases

1. **args-file 不存在**：返回 clear error
2. **args-file JSON 格式錯誤**：返回 parse error
3. **stdin 為空**：返回 error
4. **同時提供 --args 和 --args-file**：--args-file 優先（或 error）

## Requirements

- **R1**: mcporter CLI 支援 `--args-file <path>` 參數
- **R2**: mcporter CLI 支援 `--args-stdin` 從 stdin 讀取
- **R3**: 現有 `--args` 行為不變
- **R4**: 或者：Agent 改用 HTTP direct call + helper script（不改 mcporter）

## Interface Contract

### 無 Bouncer API 變更

此 fix 在 client 端（mcporter CLI 或 agent helper script），不影響 Bouncer Lambda。

### 方案 A: mcporter 改動
```bash
# 從檔案讀取
mcporter call bouncer bouncer_upload --args-file /tmp/upload.json

# 從 stdin 讀取
cat /tmp/upload.json | mcporter call bouncer bouncer_upload --args-stdin
```

### 方案 B: Agent helper script（不改 mcporter）
```bash
# 直接呼叫 HTTP API
curl -s -X POST https://API_ENDPOINT/prod/mcp \
  -H 'Content-Type: application/json' \
  -d @/tmp/request.json
```
