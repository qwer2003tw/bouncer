# Sprint 13-002: show_alert for DANGEROUS Commands

> GitHub Issue: #62
> Priority: P1
> TCS: 3
> Generated: 2026-03-05

---

## Problem Statement

當使用者按下 DANGEROUS 操作的 ⚠️ Confirm 按鈕時，Telegram 僅顯示一個小型 toast notification（`answer_callback` 回傳的 `text`）。對於高危操作（如 `aws ec2 terminate-instances`、`aws iam delete-role`），這個回饋不夠醒目。

Telegram Bot API 的 `answerCallbackQuery` 支援 `show_alert: true` 參數，會顯示一個**模態對話框**（alert popup），使用者必須點確認才會關閉。這是 DANGEROUS 操作更適合的 UX。

### 現狀

```python
# callbacks.py — approve 路徑
cb_text = '✅ 執行中 + 🔓 信任啟動' if action == 'approve_trust' else '✅ 執行中...'
answer_callback(callback_id, cb_text)  # ← 普通 toast，不管 dangerous 與否
```

```python
# telegram.py:220
def answer_callback(callback_id: str, text: str):
    data = {
        'callback_query_id': callback_id,
        'text': text
    }
    _telegram_request('answerCallbackQuery', data)
```

**問題**：`answer_callback()` 不支援 `show_alert` 參數。

## Root Cause

初期實作 `answer_callback()` 時未加入 `show_alert` 參數支援。

## User Stories

**US-1: DANGEROUS alert popup**
As the **admin (Steven)**,
I want a modal alert popup when I approve a dangerous command,
So that I get clear visual confirmation that a high-risk operation is about to execute.

## Scope

### 改動

1. **`telegram.py: answer_callback()`** — 加入 `show_alert: bool = False` 參數
   ```python
   def answer_callback(callback_id: str, text: str, show_alert: bool = False):
       data = {
           'callback_query_id': callback_id,
           'text': text,
       }
       if show_alert:
           data['show_alert'] = True
       _telegram_request('answerCallbackQuery', data)
   ```

2. **`callbacks.py`** — approve DANGEROUS 命令時使用 `show_alert=True`
   - 在 `handle_command_callback()` 的 approve 路徑中，判斷 `is_dangerous(command)` 或讀取 DB item 中的 dangerous flag
   - 如果是 DANGEROUS：`answer_callback(callback_id, '⚠️ 高危操作確認：正在執行...', show_alert=True)`
   - 如果不是 DANGEROUS：維持現有行為 `answer_callback(callback_id, '✅ 執行中...')`

### 判斷 DANGEROUS 的方式

callbacks.py 在 approve 路徑拿到 `command`（從 DynamoDB item 讀取），可直接呼叫 `is_dangerous(command)`。

注意：`from commands import is_dangerous` — 需確認 callbacks.py 是否已 import。

## Out of Scope

- 不改 reject 路徑（reject 本身不危險，toast 即可）
- 不改 grant approve / trust revoke 的 answer_callback
- 不改 account add/remove 的 answer_callback

## Acceptance Criteria

1. `answer_callback()` 支援 `show_alert` 參數
2. DANGEROUS 命令 approve 時，Telegram 顯示 modal alert（非 toast）
3. 非 DANGEROUS 命令 approve 行為不變
4. 現有測試通過
5. 新增測試驗證 `show_alert=True` 時 API data 包含 `show_alert: True`
