# Sprint 14-001: deploy/grant 請求過期後按鈕不清除 — 補 EventBridge schedule

> GitHub Issue: #75
> Priority: P0
> TCS: 7
> Generated: 2026-03-06

---

## Problem Statement

Execute/Upload 請求過期後，EventBridge Scheduler 會觸發 cleanup handler 清除 Telegram inline keyboard 按鈕。
但 **deploy**、**deploy_frontend**、**grant** 三種請求類型完全沒有建立 EventBridge schedule，過期後按鈕永遠不會自動清除。

### 正常運作的對照

| 請求類型 | `post_notification_setup` | 狀態 |
|---------|--------------------------|------|
| `bouncer_execute` | ✅ `mcp_execute.py:848` | 正常 |
| `bouncer_upload` | ✅ `mcp_upload.py:500` | 正常 |
| `bouncer_upload_batch` | ✅ `mcp_upload.py:968` | 正常 |

### 受影響

| 請求類型 | 問題 |
|---------|------|
| `bouncer_deploy` | `deployer.py:875` 呼叫 `send_telegram_message()` 但沒取回 message_id，沒呼叫 `post_notification_setup` |
| `bouncer_deploy_frontend` | 同上路徑 |
| `bouncer_request_grant` | `notifications.py:437` 用 `_send_message()` 但沒取回 message_id |

## Root Cause

1. **deployer.py `send_deploy_approval_request()`**（line 875）：呼叫 `send_telegram_message(text, reply_markup=keyboard)` 但沒有接收回傳值（回傳值含 `message_id`），也沒有呼叫 `post_notification_setup()`。
2. **notifications.py `send_grant_request_notification()`**（line 437）：呼叫 `_send_message(text, keyboard)` 但沒有接收回傳值，也沒有呼叫 `post_notification_setup()`。

## User Stories

**US-1: deploy 過期自動清除按鈕**
As the **admin (Steven)**,
I want expired deploy approval messages to have their buttons removed automatically,
So that I don't accidentally approve expired deploy requests.

**US-2: grant 過期自動清除按鈕**
As the **admin (Steven)**,
I want expired grant approval messages to have their buttons removed automatically,
So that stale grant requests don't clutter my chat with actionable buttons.

## Scope

### 變更 1: deployer.py — `send_deploy_approval_request()`

**檔案：** `src/deployer.py` (line ~834-875)

**現況：**
```python
send_telegram_message(text, reply_markup=keyboard)
# ← 沒有取回 message_id，沒有建 schedule
```

**修改：**
```python
from notifications import post_notification_setup

resp = send_telegram_message(text, reply_markup=keyboard)
telegram_message_id = resp.get('result', {}).get('message_id') if resp.get('ok') else None

if telegram_message_id:
    post_notification_setup(
        request_id=request_id,
        telegram_message_id=telegram_message_id,
        expires_at=expires_at,
    )
```

**注意：** `send_deploy_approval_request()` 目前沒有 TTL 參數。TTL 在 `mcp_tool_deploy()` 中計算（line ~726: `ttl = int(time.time()) + 300 + 60`）。需要：
- 方案 A：把 `ttl` 傳入 `send_deploy_approval_request()`（推薦，乾淨）
- 方案 B：在函數內自己算（不推薦，重複邏輯）

**推薦方案 A：** 修改簽名加 `expires_at: int` 參數，由 `mcp_tool_deploy()` 傳入。

### 變更 2: notifications.py — `send_grant_request_notification()`

**檔案：** `src/notifications.py` (line ~350-438)

**現況：**
```python
_send_message(text, keyboard)
# ← 沒有取回 message_id
```

**修改：**
```python
result = _send_message(text, keyboard)
telegram_message_id = result.get('result', {}).get('message_id') if result.get('ok') else None

if telegram_message_id:
    post_notification_setup(
        request_id=grant_id,
        telegram_message_id=telegram_message_id,
        expires_at=int(time.time()) + GRANT_APPROVAL_TIMEOUT + 60,
    )
```

**注意：** `_send_message()` 已回傳 API response dict（line 50-56），只需接收回傳值即可。

### 變更 3: mcp_tool_deploy() 傳入 TTL

**檔案：** `src/deployer.py` (line ~730)

在 `send_deploy_approval_request()` 呼叫時，把 `ttl` 傳入：
```python
send_deploy_approval_request(request_id, project, branch, reason, source, context=context, expires_at=ttl)
```

### deploy_frontend 路徑

需檢查 `mcp_upload.py` 中 deploy_frontend 的 approval 發送路徑，確認是否也缺少 `post_notification_setup`。若是，一併修復。

## Out of Scope

- 修改 EventBridge Scheduler 本身的邏輯（已正常運作）
- 修改 cleanup handler 邏輯

## Test Plan

### Unit Tests（新增）

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_send_deploy_approval_stores_message_id` | `send_deploy_approval_request()` 成功後呼叫 `post_notification_setup` |
| T2 | `test_send_deploy_approval_no_message_id` | API 回傳失敗時不呼叫 `post_notification_setup`（不 crash） |
| T3 | `test_grant_notification_stores_message_id` | `send_grant_request_notification()` 成功後呼叫 `post_notification_setup` |
| T4 | `test_grant_notification_no_message_id` | API 回傳失敗時不 crash |

### Integration Verification

- deploy 審批請求過期後，按鈕應自動消失
- grant 審批請求過期後，按鈕應自動消失

## Acceptance Criteria

- [ ] `send_deploy_approval_request()` 呼叫後，DDB record 有 `telegram_message_id`
- [ ] `send_deploy_approval_request()` 呼叫後，EventBridge schedule 已建立
- [ ] `send_grant_request_notification()` 呼叫後，DDB record 有 `telegram_message_id`
- [ ] `send_grant_request_notification()` 呼叫後，EventBridge schedule 已建立
- [ ] API 回傳失敗時不 crash，graceful fallback（只 log warning）
- [ ] 所有既有測試通過
