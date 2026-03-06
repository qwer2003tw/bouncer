# Sprint 11-008: Tasks — button style urlencode → JSON body

> Generated: 2026-03-04

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | telegram.py (1 file) |
| D2 Cross-module | 0 | 無跨模組 — 改動限於 telegram.py |
| D3 Testing | 2 | 新行為測試 + 回歸測試 |
| D4 Infrastructure | 0 | 無 |
| D5 External | 0 | Telegram API 行為不變 |
| **Total TCS** | **3** | ✅ 不需拆分 |

## Task List

```
[008-T1] [P0] [US-1] _strip_button_style() helper: 移除 inline_keyboard 按鈕中的 style field
[008-T2] [P0] [US-1] send_telegram_message(): reply_markup 存在時用 json_body=True，不再手動 json.dumps
[008-T3] [P0] [US-1] send_telegram_message_silent(): 同上，reply_markup 存在時用 json_body=True
[008-T4] [P1] 測試: reply_markup 存在 → _telegram_request 收到 json_body=True
[008-T5] [P1] 測試: style field 被 strip（不出現在送出的 data 中）
[008-T6] [P1] 測試: 無 reply_markup → json_body=False（不影響純文字訊息）
[008-T7] [P2] 回歸: 現有 notification 測試不 break
```
