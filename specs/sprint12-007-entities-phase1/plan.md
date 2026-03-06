# Sprint 12-007: Plan — MarkdownV2 → entities Phase 1

> Generated: 2026-03-05

---

## Technical Context

### 現狀分析

1. **`telegram.py`** (`259 行`):
   - `_telegram_request()`: 統一 API 呼叫，支援 `json_body=True`
   - `send_telegram_message()`: 固定 `parse_mode: 'Markdown'`
   - `update_message()`: 固定 `parse_mode: 'Markdown'`
   - `escape_markdown()`: 只 escape `\ * _ \` [`

2. **`notifications.py`** (`859 行`):
   - 29 處 `_escape_markdown()` 呼叫
   - `send_approval_request()` (L124-246): 最複雜的訊息組裝，包含 source、account、command、reason、template scan、timeout
   - 使用 Markdown `*bold*` 和 `` `code` `` 格式

3. **Telegram entities API**:
   - `sendMessage` 和 `editMessageText` 都支援 `entities` 欄位
   - 使用 `entities` 時**不需要** `parse_mode`
   - offset/length 以 UTF-16 code units 計算（emoji = 2 units）

### Design

#### Part 1: `telegram_entities.py` — MessageBuilder

```python
class MessageBuilder:
    """Fluent builder for Telegram message text + entities."""

    def text(self, s: str) -> 'MessageBuilder': ...
    def bold(self, s: str) -> 'MessageBuilder': ...
    def code(self, s: str) -> 'MessageBuilder': ...
    def italic(self, s: str) -> 'MessageBuilder': ...
    def pre(self, s: str, language: str = '') -> 'MessageBuilder': ...
    def newline(self) -> 'MessageBuilder': ...

    def build(self) -> tuple[str, list[dict]]:
        """Return (text, entities) ready for Telegram API."""
        ...
```

**UTF-16 offset 計算**：
```python
def _utf16_len(s: str) -> int:
    """Calculate length in UTF-16 code units (Telegram's offset system)."""
    return len(s.encode('utf-16-le')) // 2
```

#### Part 2: `telegram.py` 擴展

```python
def send_telegram_message(text: str, reply_markup: dict = None,
                          entities: list[dict] = None) -> dict:
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
    }
    if entities is not None:
        data['entities'] = entities
        # 不設 parse_mode — entities 和 parse_mode 互斥
    else:
        data['parse_mode'] = 'Markdown'  # backward compatible
    ...
```

同樣模式套用到 `update_message()`。

#### Part 3: POC — `send_approval_request()` 遷移

Before（Markdown）：
```python
text = (
    f"🔐 *AWS 執行請求*\n\n"
    f"{source_line}"
    f"📋 *命令：*\n`{cmd_preview}`\n\n"
    ...
)
result = _send_message(text, keyboard)
```

After（entities）：
```python
mb = MessageBuilder()
mb.text("🔐 ").bold("AWS 執行請求").newline().newline()
if source:
    mb.text("🤖 ").bold("來源：").text(f" {source}").newline()
mb.text("📋 ").bold("命令：").newline().code(cmd_preview).newline().newline()
mb.text("💬 ").bold("原因：").text(f" {reason}").newline()
...
text, entities = mb.build()
result = _send_message(text, keyboard, entities=entities)
```

**不再需要** `_escape_markdown(reason)`、`_escape_markdown(source)` 等。

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| UTF-16 offset 計算錯誤 | 中 | 中 | 完整測試 emoji/CJK 字元 |
| entities 與 reply_markup 不相容 | 低 | 高 | Telegram docs 確認兩者可共存 |
| send_approval_request 遷移後訊息格式不一致 | 中 | 低 | 用 entities 可以完全重現現有格式 |
| 其他 functions 仍用 Markdown，風格不統一 | 確定 | 低 | Phase 1 接受，Phase 2 繼續遷移 |

## Testing Strategy

- 單元測試：MessageBuilder.bold/code/text → 正確 offset/length/type
- 單元測試：UTF-16 計算 — ASCII、CJK、emoji
- 單元測試：build() 空 builder → ("", [])
- 單元測試：send_telegram_message(entities=...) → 不帶 parse_mode
- 單元測試：send_telegram_message(entities=None) → 帶 parse_mode（向後相容）
- 整合測試：send_approval_request() 產出的 text/entities 符合預期格式
