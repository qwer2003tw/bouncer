# Sprint 12-007: MarkdownV2 → entities Phase 1

> GitHub Issue: #52
> Priority: P1
> TCS: 8
> Generated: 2026-03-05

---

## Problem Statement

Bouncer 目前所有 Telegram 訊息都使用 `parse_mode: 'Markdown'`（legacy Markdown，非 MarkdownV2）。這導致：

1. **Escape 地獄**：`escape_markdown()` 只處理 `\ * _ \` [`，但使用者輸入的 reason/command 可能含 `)`、`~`、`>`、`|` 等字元，造成 Telegram API 400 error。
2. **Fallback 機制粗暴**：`_telegram_request()` 在 400 error 時會整個移除 `parse_mode`，導致整段訊息變成 plain text（`telegram.py:102-105`）。
3. **功能受限**：Markdown mode 不支援 spoiler、strikethrough、underline 等 Telegram entity types。
4. **`notifications.py` 大量 `_escape_markdown()` 呼叫**：29 處，每處都是潛在的 escape bug。

### Telegram `entities` 的優勢

Telegram sendMessage API 支援 `entities` 欄位，可直接用 offset/length 指定格式化範圍，完全不需 escape：

```json
{
  "text": "AWS 執行請求\n來源：Private Bot",
  "entities": [
    {"type": "bold", "offset": 0, "length": 8},
    {"type": "bold", "offset": 9, "length": 3}
  ]
}
```

**好處**：
- 不需要 escape 任何字元
- 不可能有 parse error（格式和文字分離）
- 支援所有 Telegram entity types

### Phase 1 範圍

Issue #52 是大型重構（notifications.py 859 行、29 處 escape）。Phase 1 的目標是：
1. 建立 `entities` builder utility（`telegram_entities.py`）
2. 改造 `telegram.py` 的低階 API，支援 entities 模式
3. 遷移 1-2 個高頻 notification function 作為 POC
4. 不移除現有 Markdown 路徑（共存）

## Root Cause

Sprint 初期選擇了 legacy Markdown 作為格式化方式。隨著訊息複雜度增加（template scan、account info、multi-line commands），escape 問題越來越多。

## User Stories

**US-1: Entities builder**
As a **developer**,
I want a fluent builder API for constructing Telegram message entities,
So that I can build formatted messages without manual offset calculation.

**US-2: Reliable message formatting**
As the **Bouncer system**,
I want approval messages to use entities instead of Markdown,
So that user-provided text (reason, command, source) never causes parse errors.

**US-3: Backward compatible**
As a **developer**,
I want entities mode to coexist with existing Markdown mode,
So that migration can be incremental across sprints.

## Scope

### Phase 1 Deliverables

1. **`telegram_entities.py`** — 新模組，MessageBuilder class
   - `bold(text)`, `code(text)`, `text(text)`, `newline()`
   - `build()` → `(text: str, entities: list[dict])`
   - Offset/length 自動計算（UTF-16 code units — Telegram 的計算方式）

2. **`telegram.py`** 擴展
   - `send_telegram_message()` 支援 `entities` 參數（替代 `parse_mode`）
   - `update_message()` 支援 `entities` 參數
   - 不移除現有 `parse_mode` 路徑

3. **POC 遷移：`send_approval_request()`**（notifications.py 最核心的 function）
   - 用 MessageBuilder 重寫 approval 訊息組裝
   - 移除該 function 中的所有 `_escape_markdown()` 呼叫

## Out of Scope（後續 Phase）

- 不遷移其他 notification functions（Phase 2+）
- 不移除 `escape_markdown()` function（仍有其他 caller）
- 不移除 `parse_mode` fallback 機制
- 不改 `editMessageText` 的 Markdown（Phase 2 再改 `update_message`）
