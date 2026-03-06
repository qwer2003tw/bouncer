# Sprint 13-001: entities Phase 2 — 全面遷移 notifications.py

> GitHub Issue: #52
> Priority: P0
> TCS: 9
> Generated: 2026-03-05
> Depends on: Sprint 12-007 (entities Phase 1 — MessageBuilder + send_approval_request POC)

---

## Problem Statement

Phase 1（Sprint 12-007）已建立 `telegram_entities.py` MessageBuilder 並遷移 `send_approval_request()`。
但 `notifications.py` 仍有 **28 處 `_escape_markdown()` 呼叫**，分佈在 14+ 個 `send_*` 函數中。

這些函數仍使用 legacy Markdown `parse_mode`，導致：
1. **使用者輸入含特殊字元時仍會觸發 400 error**（reason、source、command 等欄位）
2. **Fallback 機制** 會移除所有格式化（`telegram.py:102-105`），訊息變 plain text
3. **Phase 1 只遷移了 1 個函數**，格式風格不一致（entities vs Markdown 混合）

### 目標

將 `notifications.py` 中**所有** `send_*` 函數遷移到 entities 模式，全面消除 `_escape_markdown()` 依賴。

## Root Cause

Phase 1 是 POC 驗證（只遷移 `send_approval_request`）。Phase 2 完成剩餘 13 個函數的遷移。

## User Stories

**US-1: 全面消除 Markdown escape**
As the **Bouncer system**,
I want all notification messages to use entities mode,
So that no user-provided text can cause Telegram parse errors.

**US-2: 統一格式化風格**
As a **developer**,
I want all notification functions to use the same MessageBuilder pattern,
So that code is consistent and easier to maintain.

**US-3: 移除死代碼**
As a **developer**,
I want `_escape_markdown()` usage in notifications.py reduced to zero,
So that future code doesn't accidentally reintroduce Markdown escape issues.

## Scope

### 需遷移的函數（14 個）

| # | 函數 | 行數 | `_escape_markdown` 數 | 複雜度 |
|---|------|------|----------------------|--------|
| 1 | `send_account_approval_request` | L254 | 1 | 低 |
| 2 | `send_trust_auto_approve_notification` | L290 | 2 | 中 |
| 3 | `send_grant_request_notification` | L334 | 2 | 高（命令列表） |
| 4 | `send_grant_execute_notification` | L427 | 0* | 低 |
| 5 | `send_grant_complete_notification` | L473 | 1 | 低 |
| 6 | `send_blocked_notification` | L490 | 2 | 低 |
| 7 | `send_trust_upload_notification` | L516 | 1 | 低 |
| 8 | `send_batch_upload_notification` | L553 | 2 | 高（info_lines） |
| 9 | `send_presigned_notification` | L634 | 4 | 低 |
| 10 | `send_presigned_batch_notification` | L664 | 3 | 低 |
| 11 | `send_trust_session_summary` | L697 | 1 | 高（動態列表） |
| 12 | `send_deploy_frontend_notification` | L780 | 5 | 高（檔案列表） |
| 13 | `build_info_lines` (utils.py) | — | 2* | 中 |
| 14 | `_send_message` / `_send_message_silent` | L49 | 0 | 中（需支援 entities） |

*注：`build_info_lines()` 在 `utils.py` 中，內部也有 escape，需一併遷移。

### 遷移模式（統一）

```python
# Before (Markdown)
text = f"🔐 *標題*\n📋 `{cmd}`\n💬 *原因：* {_escape_markdown(reason)}"
_send_message(text, keyboard)

# After (entities)
mb = MessageBuilder()
mb.text("🔐 ").bold("標題").newline()
mb.text("📋 ").code(cmd).newline()
mb.text("💬 ").bold("原因：").text(f" {reason}")
text, entities = mb.build()
_send_message(text, entities=entities, keyboard=keyboard)
```

### `_send_message` / `_send_message_silent` 改造

需擴展這兩個 helper 支援 entities 模式：
```python
def _send_message(text, keyboard=None, entities=None) -> dict:
    if entities is not None:
        return send_message_with_entities(text, entities, reply_markup=keyboard)
    return send_telegram_message(text, reply_markup=keyboard)

def _send_message_silent(text, keyboard=None, entities=None) -> dict:
    if entities is not None:
        return send_message_with_entities(text, entities, reply_markup=keyboard, silent=True)
    return send_telegram_message_silent(text, reply_markup=keyboard)
```

## Out of Scope

- 不改 `callbacks.py` 中的 `update_message()` / `escape_markdown` 呼叫（Phase 3）
- 不改 `paging.py` 中 `send_remaining_pages()` 的 Markdown 格式（Phase 3）
- 不移除 `telegram.py` 中的 `escape_markdown()` 函數（callbacks.py 仍在用）
- 不改 `telegram.py` 中 `send_telegram_message()` 的 `parse_mode` 邏輯

## Acceptance Criteria

1. `notifications.py` 中 `_escape_markdown()` 呼叫降至 0
2. `build_info_lines()` 有 entities-compatible 版本（`build_info_builder()`）
3. 所有 14 個 `send_*` 函數使用 entities 模式
4. 現有測試全部通過
5. 新增測試覆蓋每個遷移函數的 entities 輸出
