# Sprint 6-002: 請求缺少有效期限顯示（upload_batch + grant）

## Summary

為 `upload_batch` 和 `grant` 請求加入 `expires_at` 欄位，在 Telegram 審批通知中顯示到期時間，並在過期時回覆「⏰ 此請求已過期」。

## Root Cause / Background

### 問題

1. **upload_batch 請求沒有 `expires_at`**：`mcp_tool_upload_batch`（`src/mcp_upload.py` 行 ~390）建立 DynamoDB item 時設定了 `ttl`（DynamoDB auto-delete 用），但沒有顯式的 `expires_at` 欄位，且 Telegram 通知中沒有顯示到期時間。

2. **grant 審批通知不顯示期限**：`send_grant_request_notification`（`src/notifications.py` 行 ~190-260）只顯示「TTL：{ttl_minutes} 分鐘」，沒有顯示具體的到期時間（如「⏰ 期限：16:30 UTC」）。

3. **過期處理已有部分實現**：`handle_telegram_webhook`（`src/app.py` 行 ~270-290）檢查 `ttl` 欄位，過期時回覆「⏰ 此請求已過期」。但這個邏輯只對有 `ttl` 的請求生效。

### 參考：execute 的實現

`bouncer_execute`（`src/mcp_execute.py` `_submit_for_approval` 行 ~385-395）：
```python
ttl = int(time.time()) + ctx.timeout + APPROVAL_TTL_BUFFER
item = {
    ...
    'ttl': ttl,    # DynamoDB TTL + 過期檢查用
    ...
}
```

`send_approval_request`（`src/notifications.py` 行 ~30-95）通知中顯示「⏰ {timeout_str}後過期」。

### 現有 TTL 常數（`src/constants.py`）

| 常數 | 值 | 用途 |
|------|----|------|
| `APPROVAL_TIMEOUT_DEFAULT` | 300（5 分鐘） | 帳號/上傳/部署審批超時 |
| `APPROVAL_TTL_BUFFER` | 60（1 分鐘） | TTL 額外緩衝 |
| `UPLOAD_TIMEOUT` | 300（5 分鐘） | 上傳審批超時 |
| `GRANT_APPROVAL_TIMEOUT` | 300（5 分鐘） | Grant 審批超時 |
| `COMMAND_APPROVAL_TIMEOUT` | 30（MCP_MAX_WAIT） | 命令審批超時 |

### upload_batch 的 TTL 設定

`src/mcp_upload.py` `mcp_tool_upload_batch` 行 ~362：
```python
ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER
```
這表示 `ttl` 欄位已正確設定為 `now + 300 + 60 = now + 360`。問題不在 DynamoDB TTL，而在：
1. Telegram 通知沒有顯示到期時間
2. `upload_batch` callback handler 沒有做過期檢查（`app.py` 的通用過期檢查在行 ~268-290 已覆蓋）

### grant 的 TTL 設定

`src/grant.py` `create_grant_request` 行 ~184：
```python
approval_timeout_at = now + GRANT_APPROVAL_TIMEOUT
item = {
    ...
    'ttl': approval_timeout_at,   # 審批超時用的 DynamoDB TTL
}
```
Grant 的 `ttl` 是審批階段的超時。批准後在 `approve_grant` 行 ~265 更新為 `expires_at`：
```python
expires_at = now + ttl_minutes * 60
# ...
':ttl_val': expires_at,  # DynamoDB TTL: 到期自動清理
```

但 **審批通知中沒有顯示「5 分鐘後過期」**。

## Acceptance Criteria

### AC-1: upload_batch 通知顯示到期時間
- **Given** Agent 發起 `bouncer_upload_batch` 請求
- **When** Telegram 收到審批通知
- **Then** 通知中包含「⏰ *5 分鐘後過期*」文字

### AC-2: grant 通知顯示到期時間
- **Given** Agent 發起 `bouncer_request_grant` 請求
- **When** Telegram 收到審批通知
- **Then** 通知中包含「⏰ *5 分鐘後過期*」文字

### AC-3: 過期 upload_batch 點擊按鈕顯示過期提示
- **Given** upload_batch 請求已過期（超過 TTL）
- **When** 用戶點擊按鈕
- **Then** answer_callback 回覆「⏰ 此請求已過期」
- **And** Telegram 訊息更新為過期樣式，按鈕移除

### AC-4: 過期 grant 點擊按鈕顯示過期提示
- **Given** grant 請求已過期（超過審批 TTL）
- **When** 用戶點擊按鈕
- **Then** answer_callback 回覆「⏰ 此請求已過期」
- **And** Telegram 訊息更新為過期樣式，按鈕移除

### AC-5: upload 單檔通知也顯示到期時間
- **Given** Agent 發起 `bouncer_upload` 請求
- **When** Telegram 收到審批通知
- **Then** 通知中包含「⏰ *5 分鐘後過期*」文字

## Implementation Plan

### 1. 通知加入到期時間顯示

#### 檔案：`src/notifications.py`

**修改 `send_batch_upload_notification`（行 ~305-360）：**

在函數參數中加入 `timeout` 參數（預設 `UPLOAD_TIMEOUT`）：

```python
def send_batch_upload_notification(
    batch_id: str,
    file_count: int,
    total_size: int,
    ext_counts: dict,
    reason: str,
    source: str = '',
    account_name: str = '',
    trust_scope: str = '',
    timeout: int = None,       # 新增
) -> None:
```

在通知文字末尾加上：
```python
from constants import UPLOAD_TIMEOUT
timeout_val = timeout or UPLOAD_TIMEOUT
if timeout_val < 60:
    timeout_str = f"{timeout_val} 秒"
elif timeout_val < 3600:
    timeout_str = f"{timeout_val // 60} 分鐘"
else:
    timeout_str = f"{timeout_val // 3600} 小時"

# 在 text 組裝中加入：
f"⏰ *{timeout_str}後過期*"
```

**修改 `send_grant_request_notification`（行 ~190-260）：**

在通知文字末尾加上到期時間。目前已有 `ttl_minutes` 參數，只需在 text 中加入：
```python
f"⏰ *{GRANT_APPROVAL_TIMEOUT // 60} 分鐘後過期*"
```

注意：這裡的 timeout 是審批超時（5 分鐘），不是 grant TTL（30-60 分鐘）。要區分。

建議改為：
```python
f"⏰ *審批期限：{GRANT_APPROVAL_TIMEOUT // 60} 分鐘*"
```

**修改 upload 單檔通知（`_submit_upload_for_approval` in `src/mcp_upload.py` 行 ~230-260）：**

在通知 message 組裝中加入：
```python
timeout_str = f"{UPLOAD_TIMEOUT // 60} 分鐘"
# 在 message 字串中加入：
f"\n⏰ *{timeout_str}後過期*"
```

### 2. 過期處理（已有，確認覆蓋）

`src/app.py` `handle_telegram_webhook` 行 ~268-290 的通用過期檢查：

```python
# 檢查是否過期
ttl = item.get('ttl', 0)
if ttl and int(time.time()) > ttl:
    answer_callback(callback['id'], '⏰ 此請求已過期')
    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression='SET #s = :s',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'timeout'}
    )
    ...
```

此邏輯在 callback routing **之前**執行，且 `upload_batch` 和 `grant` 的 DynamoDB item 都有 `ttl` 欄位，因此 **已覆蓋**。

**但 grant callbacks 走特殊路徑**（`app.py` 行 ~236-253）：`grant_approve_all`、`grant_approve_safe`、`grant_deny` 在通用過期檢查之前就 return 了。

需要在 grant callback 路徑中加入過期檢查：

```python
# 在 handle_grant_approve_all / grant_approve_safe / grant_deny 之前
if action.startswith('grant_'):
    # 讀取 grant item 檢查 TTL
    try:
        grant_item = table.get_item(Key={'request_id': request_id}).get('Item')
        if grant_item:
            grant_ttl = int(grant_item.get('ttl', 0))
            if grant_ttl and int(time.time()) > grant_ttl:
                answer_callback(callback['id'], '⏰ 此請求已過期')
                message_id = callback.get('message', {}).get('message_id')
                if message_id:
                    update_message(message_id, f"⏰ *Grant 審批已過期*\n\n🆔 `{request_id}`", remove_buttons=True)
                return response(200, {'ok': True})
    except Exception:
        pass
```

或者更簡單的做法：**將 grant callbacks 移到通用過期檢查之後**（需要重構 callback routing 順序）。

### 3. Grant Approve 後的通知加入期限

`callbacks.py` `handle_grant_approve` 已在審批通過後顯示 `⏱ *有效時間：* {ttl_minutes} 分鐘`。這是 grant 使用期限（非審批期限），已足夠。不需額外修改。

### 修改檔案清單

| 檔案 | 修改內容 |
|------|----------|
| `src/notifications.py` | `send_batch_upload_notification` 加 timeout 顯示；`send_grant_request_notification` 加審批期限顯示 |
| `src/mcp_upload.py` | `_submit_upload_for_approval` 的通知 message 加到期時間 |
| `src/app.py` | Grant callback 路徑加入過期檢查（行 ~236-253 之前） |

### 不需修改

| 檔案 | 原因 |
|------|------|
| `src/constants.py` | TTL 常數已存在，不需新增 |
| `src/grant.py` | `create_grant_request` 已正確設定 `ttl` |
| `src/mcp_upload.py` `mcp_tool_upload_batch` | DynamoDB item 的 `ttl` 已正確設定 |
| `src/callbacks.py` | 過期檢查在 `app.py` 處理，不需在 callback handler 中重複 |

## Test Plan

### 新增測試

1. **`tests/test_expiry_display.py`**（新增）
   - 測試 `send_batch_upload_notification` 通知文字包含「過期」相關字串
   - 測試 `send_grant_request_notification` 通知文字包含「過期」相關字串
   - 測試 upload 單檔通知包含過期字串

2. **`tests/test_grant_expiry.py`**（新增或合併到現有 grant 測試）
   - 測試過期 grant 的 callback 處理：
     - Mock DynamoDB get_item 回傳 ttl < current_time 的 grant item
     - 驗證 `answer_callback` 被呼叫，toast 為「⏰ 此請求已過期」
     - 驗證 `update_message` 移除按鈕

### 修改現有測試

3. **`tests/test_notifications.py`**（若存在）
   - 更新 `send_batch_upload_notification` 的 mock 驗證，確認 timeout 參數傳遞正確

### 手動驗證

4. 發起 upload_batch → 確認通知有「⏰ 5 分鐘後過期」
5. 發起 grant → 確認通知有「⏰ 審批期限：5 分鐘」
6. 等待過期後點擊按鈕 → 確認看到「⏰ 此請求已過期」

## Out of Scope

- 不修改 `bouncer_execute` 的過期顯示（已正常工作）
- 不修改 TTL 時長（仍為 5 分鐘 + 1 分鐘 buffer）
- 不加入倒計時更新（Telegram 訊息不動態更新時間）
- 不修改 DynamoDB TTL 機制（仍用 `ttl` attribute）
- 不實作 EventBridge 自動清除（Sprint 6-003 處理）
- 不修改 Grant 批准後的使用期限顯示（已有）
