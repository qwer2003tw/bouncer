# Sprint 10-004: Plan — 按鈕英文 + style 顏色

> Generated: 2026-03-03

---

## Technical Context

### 現狀分析

`src/notifications.py` 中所有 inline keyboard button 位置（已掃描）：

| 行號 | 函數 | 按鈕 |
|------|------|------|
| L216 | `send_approval_request` (dangerous) | ⚠️ 確認執行, ❌ 拒絕 |
| L235-237 | `send_approval_request` (normal) | ✅ 批准, 🔓 信任10分鐘, ❌ 拒絕 |
| L278-279 | `send_account_approval_request` | ✅ 批准, ❌ 拒絕 |
| L319 | trust status notification | 🛑 結束信任 |
| L406-413 | `send_grant_request_notification` | ✅ 全部批准, ✅ 只批准安全的, ❌ 拒絕 |
| L459 | grant active notification | 🛑 撤銷 Grant |
| L539 | trust notification (another) | 🛑 結束信任 |
| L605-609 | `send_batch_upload_notification` | 📁 批准上傳, ❌ 拒絕, 🔓 批准 + 信任10分鐘 |
| L844-845 | deploy notification | ✅ 批准部署, ❌ 拒絕 |

總共 11 個按鈕 text 需修改，同時加入 `style` 欄位。

### 影響範圍

- `src/notifications.py` — 唯一修改檔案（純 text + style 欄位修改）
- `callback_data` 不變 → `callbacks.py` 不受影響
- 測試如有 mock button text 的 assertion → 需同步更新

## Implementation Phases

### Phase 1: 按鈕文字替換

逐一替換所有 `'text'` 值：

```python
# 範例（L235-237）
# Before:
{'text': '✅ 批准', 'callback_data': f'approve:{request_id}'},
{'text': '🔓 信任10分鐘', 'callback_data': f'approve_trust:{request_id}'},
{'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}

# After:
{'text': '✅ Approve', 'callback_data': f'approve:{request_id}', 'style': 'positive'},
{'text': '🔓 Trust 10min', 'callback_data': f'approve_trust:{request_id}', 'style': 'secondary'},
{'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'destructive'}
```

### Phase 2: 確認 Bot API style 值

實作前需確認 Bot API 9.4 的 `InlineKeyboardButton.style` 實際接受的值。可能是：
- `positive` / `destructive` / `secondary`
- 或 `success` / `danger` / `primary`

需查 https://core.telegram.org/bots/api#inlinekeyboardbutton 確認。

### Phase 3: 測試更新

1. grep 測試中 mock 或 assert button text 的地方，同步更新
2. 部署後目視確認 Telegram 按鈕顯示正確
