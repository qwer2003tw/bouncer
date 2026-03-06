# Sprint 12-003: Plan — CLEANUP handler message_id in schedule payload

> Generated: 2026-03-05

---

## Technical Context

### 現狀分析

1. **`SchedulerService.create_expiry_schedule()`** (`scheduler_service.py:100-145`):
   - 簽章：`create_expiry_schedule(self, request_id: str, expires_at: int) -> bool`
   - Target Input：`{"source": "bouncer-scheduler", "action": "cleanup_expired", "request_id": request_id}`
   - 沒有 `message_id` 欄位

2. **`post_notification_setup()`** (`notifications.py:68-118`):
   - 呼叫 `svc.create_expiry_schedule(request_id=request_id, expires_at=expires_at)`
   - 已持有 `telegram_message_id` 但未傳給 scheduler

3. **`handle_cleanup_expired()`** (`app.py:91-178`):
   - `request_id = event.get('request_id')` — 從 event 取
   - `telegram_message_id = item.get('telegram_message_id')` — 從 DDB 取
   - 如果 DDB 無 `telegram_message_id` → `_mark_request_timeout()` + skip message update

### Design

#### Step 1: 擴展 `create_expiry_schedule()` 簽章

```python
def create_expiry_schedule(self, request_id: str, expires_at: int,
                           message_id: int | None = None) -> bool:
```

Target Input 增加 `message_id`（可選）：
```json
{
  "source": "bouncer-scheduler",
  "action": "cleanup_expired",
  "request_id": "req-xxx",
  "message_id": 12345
}
```

**向後相容**：`message_id` 是 optional，舊的 schedule（沒有此欄位）不受影響。
只在 `message_id is not None` 時加入 payload，避免傳 null。

#### Step 2: `post_notification_setup()` 傳遞 `message_id`

```python
svc.create_expiry_schedule(
    request_id=request_id,
    expires_at=expires_at,
    message_id=telegram_message_id,
)
```

#### Step 3: `handle_cleanup_expired()` fallback 邏輯

```python
# 優先 DDB（最新值），fallback event payload
telegram_message_id = item.get('telegram_message_id') or event.get('message_id')
```

DDB 優先是因為：如果 request 已被 approve 後 callback 更新了 DDB record，DDB 值更可靠。

## Risk Analysis

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| 舊 schedule 無 `message_id` | 確定（過渡期） | 低 | fallback 到 DDB（現有行為） |
| `message_id` 在 event 中但 DDB 也有 | 確定 | 無 | 優先用 DDB |
| Schedule payload 大小限制 | 極低 | 無 | 只增一個 int，遠低於 256KB 限制 |

## Testing Strategy

- 單元測試：`create_expiry_schedule()` 有無 `message_id` 兩種情況
- 單元測試：`handle_cleanup_expired()` DDB 有 message_id、DDB 無但 event 有、兩者都無
- 整合：確認 `post_notification_setup()` 正確傳遞 `message_id`
