# Sprint 14-003: button style 欄位驗證/修復

> GitHub Issue: #60
> Priority: P1
> TCS: 3
> Generated: 2026-03-06
> Related: Sprint 11-008（sprint11-008-button-style-json-body）

---

## Problem Statement

Sprint 10 在 `notifications.py` 的所有 InlineKeyboardButton 中加入了 `style` 欄位（Bot API 9.4 支援）。Sprint 11-008 嘗試修復 JSON body 發送問題。但目前需要驗證：

1. `style` 欄位是否真的在 Telegram 客戶端渲染
2. `_strip_unsupported_button_fields()` 是否仍在移除 `style`（這是 Sprint 11 加的保護，但如果 Bot API 已支援則不需要）
3. 各發送路徑是否都用 `json_body=True`

### 現況分析

**`telegram.py`：**
- `_strip_unsupported_button_fields()`（line 149-156）：**主動移除** `style` 欄位
- `send_telegram_message()`（line 161）：有 `json_body=True` ✅
- `send_telegram_message_silent()`（line 177）：有 `json_body=True` ✅
- `send_message_with_entities()`（line 272）：有 `json_body=True` ✅

**問題：** `_strip_unsupported_button_fields()` 在每個有 `reply_markup` 的函數中都被呼叫，它會移除 `style` key。這代表即使 `json_body=True`，**style 欄位也永遠不會發送到 Telegram API**。

## Root Cause

`_strip_unsupported_button_fields()` 是安全保護：避免未知欄位導致 API error。但如果 Telegram Bot API 現在支援 `style`，這個保護反而阻止了 style 的渲染。

## User Stories

**US-1: 確認 style 是否可用**
As a **developer**,
I want to verify whether Telegram Bot API actually renders the `style` field on inline keyboard buttons,
So that we know if the feature works or should be removed.

## Scope

### Step 1: 驗證 Telegram Bot API 是否支援 style

1. 查 Telegram Bot API 文件（https://core.telegram.org/bots/api#inlinekeyboardbutton）
2. 確認 `style` 是否是 Bot API 9.4+ 的正式欄位
3. 如果不支援 → style 是自訂欄位，`_strip_unsupported_button_fields()` 正確，需移除 notifications.py 中所有 `style` 設定
4. 如果支援 → 修改 `_strip_unsupported_button_fields()` 保留 `style`

### Step 2A: 若 API 不支援 style（大機率）

- 移除 `notifications.py` 中所有 `'style': '...'` 設定（約 13 處）
- 移除 `_strip_unsupported_button_fields()` 函數
- 移除相關測試中的 style 驗證
- 在 Issue #60 標記為 "won't fix — API 不支援"

### Step 2B: 若 API 支援 style

- 修改 `_strip_unsupported_button_fields()` 保留 `style` key（移除 strip 邏輯或改為 allowlist）
- 驗證渲染效果

## Out of Scope

- 改用其他方式實現按鈕樣式（如 emoji prefix）
- 改用 Bot API 以外的方式（如 Webapp buttons）

## Test Plan

### Unit Tests

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_strip_unsupported_fields_updated` | 根據方案 2A/2B 更新行為 |
| T2 | `test_keyboard_no_style_field` (2A) 或 `test_keyboard_has_style_field` (2B) | 確認最終 JSON |

### Manual Verification

- 發送一個有 `reply_markup` 的測試訊息，確認 Telegram 客戶端是否渲染 style

## Acceptance Criteria

- [ ] 確認 Bot API style 欄位支援狀態（有文件佐證）
- [ ] 根據結果清理 `_strip_unsupported_button_fields()` 和 `notifications.py` 中的 style 設定
- [ ] 所有既有測試通過
- [ ] Issue #60 標記結論
