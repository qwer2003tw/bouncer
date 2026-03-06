# Sprint 12-007: Tasks — MarkdownV2 → entities Phase 1

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | telegram_entities.py（新）、telegram.py、notifications.py |
| D2 Cross-module | 1 | entities builder ↔ telegram ↔ notifications |
| D3 Testing | 3 | MessageBuilder 單元 + UTF-16 + send_approval 整合 |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 1 | Telegram API entities 格式（新 API 用法） |
| **Total TCS** | **8** | ✅ 不需拆分 |

## Task List

```
[007-T1] [P0] [US-1] 新建 telegram_entities.py: MessageBuilder class — text/bold/code/italic/pre/newline methods
[007-T2] [P0] [US-1] telegram_entities.py: _utf16_len() helper + build() 方法（正確計算 offset/length）
[007-T3] [P0] [US-3] telegram.py: send_telegram_message() 增加 entities 參數，entities 存在時不設 parse_mode
[007-T4] [P0] [US-3] telegram.py: update_message() 增加 entities 參數（同上模式）
[007-T5] [P0] [US-2] notifications.py: send_approval_request() 改用 MessageBuilder（非危險命令版本）
[007-T6] [P1] [US-2] notifications.py: send_approval_request() 改用 MessageBuilder（危險命令版本）
[007-T7] [P1] 測試: MessageBuilder — bold("hello") → offset=0, length=5, type=bold
[007-T8] [P1] 測試: MessageBuilder — text("a") + bold("b") → 兩個 segment，bold offset=1
[007-T9] [P1] 測試: MessageBuilder — UTF-16 emoji（🔐 = 2 units）offset 正確
[007-T10] [P1] 測試: MessageBuilder — CJK 字元（中文 = 1 UTF-16 unit each）offset 正確
[007-T11] [P1] 測試: send_telegram_message(entities=[...]) → API data 無 parse_mode，有 entities
[007-T12] [P1] 測試: send_telegram_message() 無 entities → API data 有 parse_mode（向後相容）
[007-T13] [P1] 測試: send_approval_request() 用 entities 版本 → text 含預期內容、entities 含 bold/code
[007-T14] [P2] 測試: MessageBuilder.build() 空 → ("", [])
```
