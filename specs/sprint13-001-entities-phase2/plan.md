# Sprint 13-001: Plan — entities Phase 2

> Generated: 2026-03-05

---

## Technical Context

### Phase 1 成果（Sprint 12-007）

1. `telegram_entities.py` — MessageBuilder 已穩定
   - `bold()`, `code()`, `text()`, `italic()`, `pre()`, `newline()`
   - `build()` → `(text, entities)` with correct UTF-16 offset
   - `from_parts()` class method for tuple-list style
2. `telegram.py` — `send_message_with_entities()` 已可用
   - 支援 `entities` + `reply_markup` + `silent`
3. `notifications.py` — `send_approval_request()` 已遷移（POC 驗證通過）

### 遷移策略

**Batch approach**：按複雜度分三批遷移。

#### Batch 1: 簡單函數（無動態列表）— 8 個

| 函數 | 特點 |
|------|------|
| `send_account_approval_request` | 靜態欄位 |
| `send_grant_execute_notification` | 結果用 pre block |
| `send_grant_complete_notification` | 2 行訊息 |
| `send_blocked_notification` | 3 行訊息 |
| `send_trust_upload_notification` | 靜態欄位 |
| `send_presigned_notification` | 靜態欄位 |
| `send_presigned_batch_notification` | 靜態欄位 |
| `_send_message` / `_send_message_silent` | 加 entities 參數 |

#### Batch 2: 中等複雜度（有條件分支 / info_lines）— 3 個

| 函數 | 特點 |
|------|------|
| `send_trust_auto_approve_notification` | 有條件的 source/reason/result |
| `send_batch_upload_notification` | 依賴 `build_info_lines()` |
| `build_info_lines()` (utils.py) | 需回傳 MessageBuilder segments 或改介面 |

#### Batch 3: 高複雜度（動態列表）— 3 個

| 函數 | 特點 |
|------|------|
| `send_grant_request_notification` | 命令分類列表（grantable/requires_individual/blocked） |
| `send_trust_session_summary` | 動態命令歷史列表，截斷邏輯 |
| `send_deploy_frontend_notification` | 檔案列表，per-file cache 標註 |

### `build_info_lines()` 重構

目前 `build_info_lines()` 回傳 Markdown 字串。Phase 2 需要改為回傳 entities-compatible 格式。

**Option A: 新增 `build_info_builder()` → MessageBuilder**（推薦）
```python
def build_info_builder(source=None, context=None) -> MessageBuilder:
    mb = MessageBuilder()
    if source:
        mb.text("🤖 ").bold("來源：").text(f" {source}").newline()
    if context:
        mb.text("📝 ").bold("備註：").text(f" {context}").newline()
    return mb
```

保留舊 `build_info_lines()` 向後相容（callbacks.py 仍在用）。

### _send_message 改造

```python
def _send_message(text, keyboard=None, entities=None) -> dict:
    """統一 send — entities 優先，fallback Markdown"""
    if entities is not None:
        return send_message_with_entities(text, entities, reply_markup=keyboard)
    return send_telegram_message(text, reply_markup=keyboard)

def _send_message_silent(text, keyboard=None, entities=None) -> dict:
    if entities is not None:
        return send_message_with_entities(text, entities, reply_markup=keyboard, silent=True)
    return send_telegram_message_silent(text, reply_markup=keyboard)
```

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| `build_info_lines` 有其他 caller，改介面影響範圍大 | 中 | 中 | 保留舊函數，新增 `build_info_builder()` |
| 遷移後訊息格式與原有不完全一致 | 低 | 低 | 每個函數對照遷移，逐一測試 |
| `send_trust_session_summary` 的 Unicode escape 字串複雜 | 中 | 低 | 用 MessageBuilder 替代手動字串拼接，清楚很多 |
| entities mode 的 `pre` block 和 Markdown ``` 效果不同 | 低 | 低 | Telegram `pre` entity 效果與 ``` 相同 |

## Testing Strategy

每個遷移函數需要：
1. **單元測試**：mock `send_message_with_entities`，驗證 `(text, entities)` 內容正確
2. **格式驗證**：text 包含所有預期欄位、entities 包含正確的 bold/code/pre types
3. **Edge cases**：空字串 source/reason、超長 command（>500 char 截斷）、emoji 在 reason 中

測試新檔案：`tests/test_notifications_entities.py`
