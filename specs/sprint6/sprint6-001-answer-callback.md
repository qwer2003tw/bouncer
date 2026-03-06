# Sprint 6-001: 按鈕無即時響應修復

## Summary

將各 callback handler 的 `answer_callback` 呼叫前移到 parse/validate 完成後立刻呼叫，消除按鈕 spinner 等待 5~10 秒的問題。

## Root Cause / Background

### 問題

用戶在 Telegram 點擊審批按鈕（✅ 批准 / ❌ 拒絕）後，按鈕 spinner 持續旋轉 5~10 秒才消失。Telegram 對 `answerCallbackQuery` 有 30 秒超時，但用戶體驗要求應在 1 秒內響應。

### 根因

`answer_callback` 被放在整個處理流程（DynamoDB read + S3 upload + editMessage）完成後才呼叫。Telegram 按鈕的 spinner 直到收到 `answerCallbackQuery` 回應才停止。

### 受影響的 Handler 分析

以下逐一分析 `src/callbacks.py` 中所有 handler 的 `answer_callback` 位置：

#### 1. `handle_command_callback` (行 ~151-250)
- **approve/approve_trust 路徑**：`answer_callback` 在 `update_and_answer` 內，位於 `execute_command` + `store_paged_output` + DynamoDB update + `create_trust_session` + `_auto_execute_pending_requests` 全部完成之後。**需要前移。**
- **deny 路徑**：`answer_callback` 在 `update_and_answer` 內，位於 `_update_request_status` DynamoDB update 完成之後。**需要前移。**

#### 2. `handle_account_add_callback` (行 ~260-310)
- **approve 路徑**：先執行 `accounts_table.put_item` + `_update_request_status` + `_send_status_update`，最後才 `answer_callback(callback_id, '✅ 帳號已新增')`（行 ~290）。**需要前移。**
- **deny 路徑**：先執行 `_update_request_status` + `_send_status_update`，最後才 `answer_callback`（行 ~300）。**需要前移。**

#### 3. `handle_account_remove_callback` (行 ~320-365)
- 結構與 `handle_account_add_callback` 相同。**approve 和 deny 路徑都需要前移。**

#### 4. `handle_deploy_callback` (行 ~375-450)
- **approve 路徑**：先執行 `_update_request_status` + `start_deploy`，再 `update_message`，最後 `answer_callback`（行 ~415 或 ~430）。**需要前移。**
- **deny 路徑**：先 `_update_request_status` + `update_message`，最後 `answer_callback`（行 ~445）。**需要前移。**

#### 5. `handle_upload_callback` (行 ~455-520)
- **approve 路徑**：先執行 `execute_upload`（S3 上傳）+ `update_message`，最後 `answer_callback`（行 ~490 或 ~505）。**需要前移。**
- **deny 路徑**：先 `_update_request_status` + `update_message`，最後 `answer_callback`（行 ~518）。**需要前移。**

#### 6. `handle_upload_batch_callback` (行 ~528-680)
- **approve/approve_trust 路徑**：這是唯一已正確實作的 handler！行 ~555 先 `update_message`（移除按鈕），緊接行 ~556 `answer_callback(callback_id, '⏳ 上傳中...')`，然後才開始批量上傳。✅ **不需要修改。**
- **deny 路徑**：先 `_update_request_status` + `update_message`，最後 `answer_callback`（行 ~672）。**需要前移。**

#### 7. Grant Handlers（`handle_grant_approve`、`handle_grant_deny`）
- `handle_grant_approve`（行 ~30-70）：使用 `update_and_answer`，在 `approve_grant`（DynamoDB update）完成後才呼叫。**需要前移。**
- `handle_grant_deny`（行 ~80-110）：使用 `update_and_answer`，在 `deny_grant`（DynamoDB update）完成後才呼叫。**需要前移。**

### 已在 `app.py` 正確處理的 Callbacks

以下 callback 在 `app.py` `handle_telegram_webhook` 中直接處理，`answer_callback` 位置合理（輕量操作後立刻呼叫）：

- `revoke_trust`（行 ~225-234）：`revoke_trust_session` + `update_message` + `answer_callback`。操作輕量，可接受。
- `grant_revoke`（行 ~245-253）：同上。
- 過期請求處理（行 ~270-290）：DynamoDB update + `update_message` + `answer_callback`。輕量，可接受。
- 已處理 fallback（行 ~265）：只有 `answer_callback`，無問題。

## Acceptance Criteria

### AC-1: 按鈕即時響應
- **Given** 用戶點擊 Telegram 審批按鈕
- **When** 系統收到 callback_query
- **Then** `answer_callback` 在 DynamoDB get_item + parse/validate 完成後、任何重型操作（command 執行、S3 上傳、deploy 啟動）之前被呼叫
- **And** 按鈕 spinner 在 1 秒內消失

### AC-2: 處理結果仍正確顯示
- **Given** `answer_callback` 已提前呼叫
- **When** 後續處理（execute_command / S3 upload / start_deploy）完成
- **Then** Telegram 訊息仍正確更新為最終結果（✅ / ❌）
- **And** DynamoDB 狀態正確更新

### AC-3: 錯誤處理不影響
- **Given** `answer_callback` 已提前呼叫
- **When** 後續處理發生錯誤
- **Then** Telegram 訊息更新為錯誤訊息
- **And** 不會重複呼叫 `answer_callback`（Telegram API 允許但無意義）

### AC-4: upload_batch approve 路徑不受影響
- **Given** `handle_upload_batch_callback` 的 approve 路徑已正確實作
- **When** 本次修改完成
- **Then** 其行為不變

## Implementation Plan

### 修法方向：方案 A — answer_callback 前移（最小改動）

每個 handler 在完成 parse/validate 後、進入重型操作前，立刻呼叫 `answer_callback`。對於使用 `update_and_answer` 的 handler，改為先 `answer_callback` 再 `update_message`。

### 修改清單

#### 檔案：`src/callbacks.py`

**1. `handle_grant_approve`（~行 50-65）**

改前：
```python
grant = approve_grant(grant_id, user_id, mode=mode)
if not grant:
    answer_callback(callback_id, '❌ Grant 不存在或已處理')
    return ...
# ... 組裝訊息 ...
update_and_answer(message_id, text, callback_id, cb_text)
```

改後：
```python
grant = approve_grant(grant_id, user_id, mode=mode)
if not grant:
    answer_callback(callback_id, '❌ Grant 不存在或已處理')
    return ...
answer_callback(callback_id, f'✅ 已批准 {len(granted)} 個{cb_suffix}')
# ... 組裝訊息 ...
update_message(message_id, text)
```

**2. `handle_grant_deny`（~行 90-108）**

改前：
```python
success = deny_grant(grant_id)
if not success:
    answer_callback(callback_id, '❌ 拒絕失敗')
    return ...
update_and_answer(message_id, text, callback_id, '❌ 已拒絕')
```

改後：
```python
success = deny_grant(grant_id)
if not success:
    answer_callback(callback_id, '❌ 拒絕失敗')
    return ...
answer_callback(callback_id, '❌ 已拒絕')
update_message(message_id, text)
```

**3. `handle_command_callback` — approve/approve_trust 路徑（~行 170）**

改前：
```python
if action in ('approve', 'approve_trust'):
    result = execute_command(command, assume_role)
    # ... 大量處理 ...
    update_and_answer(message_id, text, callback_id, cb_text)
```

改後：
```python
if action in ('approve', 'approve_trust'):
    cb_text = '✅ 執行中...' if action != 'approve_trust' else '✅ 執行中 + 🔓 信任啟動'
    answer_callback(callback_id, cb_text)
    result = execute_command(command, assume_role)
    # ... 大量處理 ...
    update_message(message_id, text)
```

**4. `handle_command_callback` — deny 路徑（~行 230）**

改前：
```python
elif action == 'deny':
    # ... DynamoDB update ...
    update_and_answer(message_id, text, callback_id, '❌ 已拒絕')
```

改後：
```python
elif action == 'deny':
    answer_callback(callback_id, '❌ 已拒絕')
    # ... DynamoDB update ...
    update_message(message_id, text)
```

**5. `handle_account_add_callback` — approve 路徑（~行 275-290）**

改前：
```python
if action == 'approve':
    try:
        accounts_table.put_item(...)
        _update_request_status(...)
        _send_status_update(...)
        answer_callback(callback_id, '✅ 帳號已新增')
```

改後：
```python
if action == 'approve':
    answer_callback(callback_id, '✅ 處理中...')
    try:
        accounts_table.put_item(...)
        _update_request_status(...)
        _send_status_update(...)
```

**6. `handle_account_add_callback` — deny 路徑（~行 295-300）**

改前：
```python
elif action == 'deny':
    _update_request_status(...)
    _send_status_update(...)
    answer_callback(callback_id, '❌ 已拒絕')
```

改後：
```python
elif action == 'deny':
    answer_callback(callback_id, '❌ 已拒絕')
    _update_request_status(...)
    _send_status_update(...)
```

**7. `handle_account_remove_callback`**

同 `handle_account_add_callback` 的改法，approve 和 deny 路徑都前移。

**8. `handle_deploy_callback` — approve 路徑（~行 390-430）**

改前：
```python
if action == 'approve':
    _update_request_status(...)
    result = start_deploy(...)
    # ... 組裝訊息 ...
    update_message(...)
    answer_callback(callback_id, '🚀 部署已啟動' 或 '❌ 部署啟動失敗')
```

改後：
```python
if action == 'approve':
    answer_callback(callback_id, '🚀 啟動部署中...')
    _update_request_status(...)
    result = start_deploy(...)
    # ... 組裝訊息 ...
    update_message(...)
```

**9. `handle_deploy_callback` — deny 路徑（~行 435-445）**

改後：先 `answer_callback`，再 `_update_request_status` + `update_message`。

**10. `handle_upload_callback` — approve 路徑（~行 470-505）**

改前：
```python
if action == 'approve':
    result = execute_upload(request_id, user_id)
    # ... 組裝訊息 ...
    update_message(...)
    answer_callback(callback_id, '✅ 已上傳' 或 '❌ 上傳失敗')
```

改後：
```python
if action == 'approve':
    answer_callback(callback_id, '📤 上傳中...')
    result = execute_upload(request_id, user_id)
    # ... 組裝訊息 ...
    update_message(...)
```

**11. `handle_upload_callback` — deny 路徑**

改後：先 `answer_callback`，再 `_update_request_status` + `update_message`。

**12. `handle_upload_batch_callback` — deny 路徑（~行 668-672）**

目前 deny 路徑的 `answer_callback` 在 `update_message` 之後。改為前移。

### 不修改

- `handle_upload_batch_callback` 的 **approve/approve_trust** 路徑 — 已正確在行 ~555-556 先 answer + 移除按鈕。
- `app.py` 中 `handle_telegram_webhook` 的直接 callback 處理（revoke_trust, grant_revoke, 過期處理）— 操作輕量，不需前移。

### 注意事項

- `update_and_answer` 是一個便利函數，同時做 `update_message` + `answer_callback`。前移後改為分別呼叫 `answer_callback` 和 `update_message`。
- `answer_callback` 的 toast 文字可以用「處理中」風格（如「✅ 執行中...」），讓用戶知道操作已被接受。
- Telegram 的 `answerCallbackQuery` 是冪等的，重複呼叫不會出錯但第二次無效果（第一次已消除 spinner）。

## Test Plan

### 新增測試

1. **`tests/test_callback_answer_timing.py`**（新增）
   - Mock `answer_callback` 和 `execute_command`
   - 驗證 `answer_callback` 在 `execute_command` 之前被呼叫（用 `call_args_list` 順序）
   - 覆蓋所有 handler 的 approve 和 deny 路徑

### 修改現有測試

2. **`tests/test_callbacks.py`**
   - 更新所有使用 `update_and_answer` mock 的測試，改為分別 mock `answer_callback` 和 `update_message`
   - 驗證呼叫順序正確

### 手動驗證

3. 在 Telegram 中測試：
   - 點擊「✅ 批准」按鈕 → spinner 應在 1 秒內消失
   - 點擊「❌ 拒絕」按鈕 → spinner 應在 1 秒內消失
   - 對 `execute`、`deploy`、`upload`、`upload_batch`、`add_account`、`grant` 各類型按鈕都測試

## Out of Scope

- 不修改 `update_and_answer` 函數本身（保留供其他場景使用）
- 不修改 `app.py` 中 `handle_telegram_webhook` 的直接 callback 處理邏輯
- 不修改 `handle_upload_batch_callback` 的 approve/approve_trust 路徑
- 不加入背景任務 / 異步處理機制（本 sprint 只做最小改動）
- 不修改 Telegram API 呼叫方式（仍用同步 HTTP 呼叫）
