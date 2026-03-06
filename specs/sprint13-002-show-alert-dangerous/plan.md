# Sprint 13-002: Plan — show_alert for DANGEROUS

> Generated: 2026-03-05

---

## Technical Context

### Telegram answerCallbackQuery API

```json
{
  "callback_query_id": "...",
  "text": "⚠️ 高危操作確認：正在執行...",
  "show_alert": true
}
```

- `show_alert: false`（預設）→ 螢幕頂部的 toast notification（2-3 秒自動消失）
- `show_alert: true` → 模態 dialog popup，使用者必須點 OK

### 影響範圍分析

`answer_callback()` 被 callbacks.py 呼叫 **24 次**。只有 `handle_command_callback()` 中的 approve 路徑需要改。

```
callbacks.py:230 — handle_command_callback approve 路徑
   → 這裡判斷 dangerous，加 show_alert=True
```

其他所有 `answer_callback()` 呼叫保持不變（`show_alert` 預設 `False`）。

### Design

```python
# telegram.py
def answer_callback(callback_id: str, text: str, show_alert: bool = False):
    data = {
        'callback_query_id': callback_id,
        'text': text,
    }
    if show_alert:
        data['show_alert'] = True
    _telegram_request('answerCallbackQuery', data)

# callbacks.py — handle_command_callback approve path
from commands import is_dangerous

dangerous = is_dangerous(command)
if action in ('approve', 'approve_trust'):
    if dangerous:
        cb_alert = '⚠️ 高危操作確認：正在執行...'
        if action == 'approve_trust':
            cb_alert = '⚠️ 高危操作 + 🔓 信任啟動'
        answer_callback(callback_id, cb_alert, show_alert=True)
    else:
        cb_text = '✅ 執行中 + 🔓 信任啟動' if action == 'approve_trust' else '✅ 執行中...'
        answer_callback(callback_id, cb_text)
```

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| `is_dangerous()` 在 callbacks.py import 失敗 | 低 | 中 | 確認 import path，commands.py 和 callbacks.py 同層 |
| show_alert text 太長（Telegram 限制 200 chars）| 低 | 低 | Alert text 控制在 50 chars 以內 |
| 改動影響其他 answer_callback 呼叫 | 無 | — | `show_alert` 預設 `False`，不影響現有呼叫 |

## Testing Strategy

- 單元測試：`answer_callback(id, text, show_alert=True)` → API data 含 `show_alert: True`
- 單元測試：`answer_callback(id, text)` → API data 不含 `show_alert`（向後相容）
- 單元測試：handle_command_callback approve dangerous → 呼叫 `answer_callback` with `show_alert=True`
- 單元測試：handle_command_callback approve non-dangerous → 呼叫 `answer_callback` without `show_alert`
