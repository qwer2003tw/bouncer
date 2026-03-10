# Sprint 18-001: entities Phase 4 — 剩餘函數遷移 + 清除 legacy helpers

> GitHub Issue: #52
> Priority: P0
> TCS: 8
> Generated: 2026-03-08
> Depends on: Sprint 13-001 (entities Phase 2)

---

## Problem Statement

`notifications.py`（872 行）的 entities 遷移已完成約 60%：
- **已遷移（使用 MessageBuilder）：** `send_approval_request`、`send_account_approval_request`、`send_trust_auto_approve_notification`、`send_grant_request_notification`、`send_grant_execute_notification`、`send_blocked_notification`、`send_batch_upload_notification`
- **未遷移（仍用 `_escape_markdown` + legacy Markdown）：** 6 個函數，共 16 處 `_escape_markdown()` 呼叫

未遷移函數清單：

| # | 函數 | `_escape_markdown` 次數 | 使用 `_send_message` | 說明 |
|---|------|------------------------|---------------------|------|
| 1 | `send_grant_complete_notification` (L488) | 2 | `_send_message_silent` | Grant 結束通知 |
| 2 | `send_trust_upload_notification` (L530) | 1 | `_send_message_silent` | Trust Upload 通知 |
| 3 | `send_presigned_notification` (L643) | 4 | `_send_message_silent` | Presigned URL 通知 |
| 4 | `send_presigned_batch_notification` (L673) | 3 | `_send_message_silent` | Presigned Batch 通知 |
| 5 | `send_trust_session_summary` (L706) | 1 | `_send_message_silent` | Trust 摘要 |
| 6 | `send_deploy_frontend_notification` (L789) | 5 | `_send_message` | 前端部署審批 |

遷移完成後，`_escape_markdown()`、`_send_message()`、`_send_message_silent()` 三個 legacy helper 將無呼叫者，可安全移除。

## Root Cause

Phase 2（Sprint 13-001）遷移了高頻函數，低頻函數因 TCS 限制延後。現在這些殘留的 legacy 函數是唯一阻止完全移除 `_escape_markdown` 和 `parse_mode: Markdown` 的障礙。

## Scope

### 變更 1: 遷移 `send_grant_complete_notification`（L488）

**檔案：** `src/notifications.py`

將 `_escape_markdown(reason)` + `_send_message_silent()` 替換為 MessageBuilder + `_telegram.send_message_with_entities()`。

```python
# Before (legacy)
text = f"🔑 *Grant 已結束*\n\n🆔 `{grant_short}`\n💬 *原因：* {_escape_markdown(reason or '')}"
_send_message_silent(text)

# After (entities)
mb = MessageBuilder()
mb.text("🔑 ").bold("Grant 已結束").newline(2)
mb.text("🆔 ").code(grant_short).newline()
mb.text("💬 ").bold("原因：").text(f" {reason or ''}")
text, entities = mb.build()
_telegram.send_message_with_entities(text, entities, silent=True)
```

### 變更 2: 遷移 `send_trust_upload_notification`（L530）

**檔案：** `src/notifications.py`

移除 `source_line = f"🤖 {_escape_markdown(source)}\n"` 字串拼接，改用 MessageBuilder。保留 End Trust 按鈕 keyboard 不變。

### 變更 3: 遷移 `send_presigned_notification`（L643）

**檔案：** `src/notifications.py`

4 處 `_escape_markdown()` → MessageBuilder `.text()` 取代。`_send_message_silent()` → `_telegram.send_message_with_entities(..., silent=True)`。

### 變更 4: 遷移 `send_presigned_batch_notification`（L673）

**檔案：** `src/notifications.py`

3 處 `_escape_markdown()` → MessageBuilder。同上模式。

### 變更 5: 遷移 `send_trust_session_summary`（L706）

**檔案：** `src/notifications.py`

最複雜的遷移：
- 含動態 command list（max 10）、duration 計算、truncation
- 1 處 `_escape_markdown(cmd)` 在 loop 中
- 需要 MessageBuilder 的 loop-based 構建
- 手動 `"\\."` escape → MessageBuilder `.text()` 自動處理

```python
# Before
cmd_lines.append("  " + str(i) + "\\. " + ok_icon + " `" + _escape_markdown(cmd) + "`")

# After — within MessageBuilder loop
mb.text(f"  {i}. {ok_icon} ").code(cmd).newline()
```

### 變更 6: 遷移 `send_deploy_frontend_notification`（L789）

**檔案：** `src/notifications.py`

5 處 `_escape_markdown()` + `_send_message()` → MessageBuilder + `_telegram.send_message_with_entities()`。

⚠️ 此函數回傳 `NotificationResult`，需要從 `send_message_with_entities` response 提取 `message_id`（同 `send_batch_upload_notification` 的模式）。

### 變更 7: 移除 legacy helpers

**檔案：** `src/notifications.py`

遷移完成後移除：
1. `_escape_markdown()` 函數（L46-47）
2. `_send_message()` 函數（L50-56）
3. `_send_message_silent()` 函數（L59-60）

⚠️ 移除前必須 `grep -rn "_escape_markdown\|_send_message\b" src/` 確認無其他呼叫者。

## Out of Scope

- `telegram.py` 的 `escape_markdown()` 和 `send_telegram_message()` 保留（其他模組如 `deployer.py`、`app.py` 仍直接呼叫）
- `deployer.py` 中 `send_deploy_notification()` 的遷移（該函數在 deployer 模組，不在 notifications.py）
- `app.py` 中 `update_message()` 的遷移

## Test Plan

### 單元測試（必須）

每個遷移函數各一組測試，驗證：
1. **entities 格式正確**：mock `_telegram.send_message_with_entities`，驗證 `text` 和 `entities` 參數
2. **特殊字元不 escape**：傳入含 `*_[]()~>` 的 reason/source，驗證 text 原樣保留（不加 `\`）
3. **keyboard 保留**：有按鈕的函數驗證 `reply_markup` 傳遞正確
4. **silent 參數**：驗證靜默通知帶 `silent=True`

| # | 測試 | 預期 |
|---|------|------|
| T1 | `send_grant_complete_notification` 含特殊字元 reason | entities 格式，無 escape |
| T2 | `send_trust_upload_notification` 含 source | source 在 MessageBuilder text 中 |
| T3 | `send_presigned_notification` 所有欄位 | 4 個 `_escape_markdown` 消除 |
| T4 | `send_presigned_batch_notification` | 3 個 `_escape_markdown` 消除 |
| T5 | `send_trust_session_summary` 10+ commands | command list truncation 正確 |
| T6 | `send_trust_session_summary` 0 commands | 簡短通知 |
| T7 | `send_deploy_frontend_notification` 回傳 NotificationResult | message_id 正確提取 |
| T8 | 移除後 grep 確認無殘留 `_escape_markdown` 呼叫 | 0 hits in src/notifications.py |

### 回歸測試

- 既有 `test_notifications_main.py` 全部通過
- CI mock consistency check 通過

## Acceptance Criteria

- [ ] 6 個函數全部遷移到 MessageBuilder + `send_message_with_entities`
- [ ] `_escape_markdown()`、`_send_message()`、`_send_message_silent()` 從 notifications.py 移除
- [ ] `grep -c "_escape_markdown" src/notifications.py` → 0
- [ ] 所有現有測試通過
- [ ] 新增 ≥ 7 個單元測試
- [ ] Coverage ≥ 75%
