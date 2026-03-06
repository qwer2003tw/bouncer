# Sprint 13-003: Plan — On-Demand Pagination

> Generated: 2026-03-05

---

## Technical Context

### 現狀流程

```
callbacks.py handle_command_callback (approve)
  → execute_command(command, assume_role)
  → paged = store_paged_output(request_id, result)
  → update DDB with result + paged metadata
  → update_message() 顯示結果（page 1）
  → if paged.get('paged'):
        send_remaining_pages(request_id, paged['total_pages'])  ← 自動推 page 2..N
```

### 目標流程

```
callbacks.py handle_command_callback (approve)
  → execute_command(command, assume_role)
  → paged = store_paged_output(request_id, result)
  → update DDB with result + paged metadata
  → update_message() 顯示結果（page 1）+ 分頁提示
  → if paged.get('paged') and paged['total_pages'] > 1:
        # 加 inline button: "📄 Show Page 2 / N"
        # 不呼叫 send_remaining_pages
```

### callback 路由

`app.py` 的 callback 路由目前支援：
- `approve:`, `deny:`, `approve_trust:`, `revoke_trust:`
- `grant_approve_all:`, `grant_approve_safe:`, `grant_deny:`, `grant_revoke:`

需新增：`show_page:{request_id}:{page_num}`

```python
# app.py callback router
elif cb_data.startswith('show_page:'):
    handle_show_page_callback(callback_id, cb_data)
```

### show_page callback 設計

```python
def handle_show_page_callback(callback_id: str, cb_data: str):
    """Handle on-demand page display."""
    # Parse: show_page:{request_id}:{page_num}
    parts = cb_data.split(':', 2)
    request_id = parts[1]
    page_num = int(parts[2])

    page_id = f"{request_id}:page:{page_num}"
    result = get_paged_output(page_id)

    if 'error' in result:
        answer_callback(callback_id, f"❌ Page not found (expired?)")
        return

    content = result.get('result', '')
    total_pages = result.get('total_pages', 1)

    # Send page content
    text = f"📄 第 {page_num}/{total_pages} 頁\n\n```\n{content}\n```"

    keyboard = None
    if page_num < total_pages:
        next_page = page_num + 1
        keyboard = {
            'inline_keyboard': [[
                {'text': f'📄 Show Page {next_page}/{total_pages}',
                 'callback_data': f'show_page:{request_id}:{next_page}'}
            ]]
        }

    _send_message_silent(text, keyboard)
    answer_callback(callback_id, f'📄 Page {page_num}/{total_pages}')
```

### 結果訊息中的分頁提示（update_message 部分）

```python
# callbacks.py — approve path, after update_message
if paged.get('paged') and paged['total_pages'] > 1:
    truncate_notice = f"\n\n📄 共 {paged['total_pages']} 頁"
    # 加 inline button
    page_keyboard = {
        'inline_keyboard': [[
            {'text': f'📄 Show Page 2/{paged["total_pages"]}',
             'callback_data': f'show_page:{request_id}:2'}
        ]]
    }
    # 注意：update_message 後的按鈕可能會被覆蓋
    # 需確認 update_and_answer 是否支援 reply_markup
```

**注意**：`update_and_answer()` 目前在 `editMessageText` 中不帶 `reply_markup`。需要確認是否可以在結果訊息中帶 inline button。

**替代方案**：結果訊息不帶按鈕，而是**另發一條訊息**提示「有更多頁」+ 按鈕。

```python
if paged.get('paged') and paged['total_pages'] > 1:
    _send_message_silent(
        f"📄 輸出共 {paged['total_pages']} 頁（{paged['output_length']} 字元），按鈕查看更多",
        {'inline_keyboard': [[
            {'text': f'📄 Show Page 2/{paged["total_pages"]}',
             'callback_data': f'show_page:{request_id}:2'}
        ]]}
    )
```

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| DDB page TTL 過期時按鈕失效 | 中 | 低 | answer_callback 提示 "Page expired" |
| callback_data 長度超過 Telegram 64 bytes 限制 | 低 | 高 | request_id 通常 ≤ 36 chars，`show_page:{id}:{n}` ≈ 50 chars，OK |
| update_message 後原有 approve/reject 按鈕消失 | 確定 | 低 | 正常行為，approve 後按鈕本來就該消失 |
| send_remaining_pages 移除後 breaks existing tests | 中 | 低 | 調整相關測試 |

## Testing Strategy

- 單元測試：`handle_show_page_callback` → 正確 page_id 查詢 + 訊息發送
- 單元測試：page expired → answer_callback error
- 單元測試：last page → 無 "Show next" 按鈕
- 單元測試：approve path — paged output → 不呼叫 send_remaining_pages
- 整合測試：approve paged output → 發分頁提示訊息 + 按鈕
