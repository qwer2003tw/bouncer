# Sprint 9-006: Plan — base64 CLI args 超長

> Generated: 2026-03-02

---

## Technical Context

### 現狀分析

1. **mcporter CLI**（`~/.npm-global/lib/node_modules/openclaw/`）：使用 `--args` 傳入 JSON string。大型 base64 content 會超過 shell arg limit。

2. **現有 workaround**：TOOLS.md 記載「用 HTTP API 直呼（繞過 CLI arg length limit）」。

3. **Bouncer API**：接受 MCP JSON-RPC over HTTP POST。不受 CLI arg limit 影響。

### 方案比較

| 方案 | 優點 | 缺點 | 複雜度 |
|------|------|------|--------|
| A: mcporter 加 --args-file | 正式解法，所有 MCP server 都受益 | 需改 mcporter（不在 bouncer repo） | 中 |
| B: Agent helper script | 不需改任何依賴 | 不通用，只解決 bouncer 場景 | 低 |
| C: bouncer-exec skill 整合 | 在 skill layer 處理，自動判斷大小切換 CLI/HTTP | 不通用 | 低 |

**推薦方案 C**：在 `bouncer-exec` skill 或 agent 的 upload helper 中加入邏輯：
- 小 payload → mcporter CLI
- 大 payload → HTTP direct call（寫入 temp file + curl）

同時可以提 mcporter upstream feature request（方案 A）。

## Implementation Phases

### Phase 1: bouncer upload helper script

1. 新增 `scripts/bouncer_upload_http.sh`（或整合到 bouncer-exec skill）
2. 接受 JSON 檔案路徑作為 input
3. 直接 POST 到 Bouncer MCP endpoint
4. Parse response 並格式化輸出

### Phase 2: Agent 使用流程

1. Agent 判斷 payload 大小：
   - < 100KB base64 → 用 mcporter CLI
   - ≥ 100KB base64 → 寫入 temp file + 用 HTTP helper
2. 或者：一律用 HTTP helper（更簡單）

### Phase 3: 文檔更新

1. 更新 TOOLS.md：正式記錄 HTTP direct call 方式
2. 移除「workaround」標記 → 改為「推薦方式」
