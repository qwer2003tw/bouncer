# Sprint 13-003: On-Demand Pagination

> GitHub Issue: #54
> Priority: P1
> TCS: 6
> Generated: 2026-03-05

---

## Problem Statement

目前 Bouncer 的長輸出分頁機制是 **auto-push**：命令執行後，`callbacks.py:344` 會呼叫 `send_remaining_pages()`，自動把 page 2..N 全部推送到 Telegram。

問題：
1. **洗版**：大輸出（如 `aws ec2 describe-instances`）可能產生 5-10 頁，每頁一條 Telegram 訊息，嚴重洗版
2. **浪費**：大多數情況下使用者只需要第一頁（概要），不需要後續頁面
3. **MCP client 已有 `bouncer_get_page`**：Agent 可以用 `bouncer_get_page` tool 按需拉取下一頁，Telegram 不需要再推

### 現狀

```
命令執行 → store_paged_output() 寫 DDB
         → MCP response 回傳 page 1 + next_page + total_pages
         → callbacks.py 呼叫 send_remaining_pages() → 自動推 page 2..N 到 Telegram
         → MCP client 也可以用 bouncer_get_page 拉後續頁
```

**結果**：Telegram 和 MCP client 都收到完整輸出，Telegram 端洗版。

### 目標

改為 **on-demand** 模式：
- Telegram 只顯示 page 1 + 「有更多頁」提示 + 可選的「顯示下一頁」按鈕
- MCP client 用 `bouncer_get_page` 按需拉取
- **不再自動推送** page 2..N 到 Telegram

## Root Cause

最初設計時 MCP client（Agent）不支援 `bouncer_get_page`，只能靠 Telegram push 確保 Steven 看到完整輸出。現在 `bouncer_get_page` 已穩定，Telegram auto-push 成為多餘。

## User Stories

**US-1: 停止 Telegram 洗版**
As the **admin (Steven)**,
I want long command output to NOT auto-push all pages to Telegram,
So that my Telegram chat stays clean.

**US-2: On-demand page via button**
As the **admin (Steven)**,
I want a "Show more" button on truncated output messages,
So that I can optionally view more pages directly in Telegram if needed.

**US-3: MCP on-demand unchanged**
As an **MCP client (Agent)**,
I want `bouncer_get_page` to continue working as-is,
So that I can programmatically fetch pages when needed.

## Scope

### Phase 1: 移除 auto-push + 提示訊息

1. **`callbacks.py`** — 移除 `send_remaining_pages()` 呼叫
   - 替換為在結果訊息中顯示「📄 共 N 頁，用 bouncer_get_page 查看更多」

2. **`callbacks.py`** — 可選：加 inline button「📄 Show Page 2」
   - callback_data: `show_page:{request_id}:2`
   - 按鈕按下 → 從 DDB 拉 page 2 → 發新訊息 → 按鈕更新為「📄 Show Page 3」
   - 最後一頁無按鈕

3. **`callbacks.py`** — 新增 `handle_show_page_callback()`
   - 解析 callback_data `show_page:{request_id}:{page_num}`
   - 從 DDB 拉頁面 → 發訊息 → answer_callback

### Phase 2（可選，本 sprint 視時間）

4. **`paging.py`** — 可考慮移除 `send_remaining_pages()` 函數本身
   - 但需確認無其他 caller

## Out of Scope

- 不改 `bouncer_get_page` MCP tool（已可用）
- 不改 `store_paged_output()` 邏輯
- 不改 `mcp_execute.py` 中 MCP response 的分頁 metadata

## Acceptance Criteria

1. 命令執行後 Telegram 不再自動推送 page 2..N
2. 結果訊息包含分頁提示（共 N 頁）
3. 可選：inline button 支援 on-demand 顯示下一頁
4. `bouncer_get_page` MCP tool 不受影響
5. 現有測試通過（需移除/調整 `send_remaining_pages` 相關測試）
