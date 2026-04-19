# Spec: Telegram date_time MessageEntity for Approval Expiry

**Feature ID:** s39-003
**Branch:** `feat/datetime-entity-s39`
**Epic:** Notification UX Improvements
**Story Points:** 3

---

## Overview

使用 Telegram Bot API 的 `date_time` MessageEntity 在 approval 通知中顯示絕對過期時間，讓 Telegram 客戶端自動轉換為使用者本地時區。

**Context:**
- 目前 approval 通知只顯示相對時間：「⏰ **30 分鐘後過期**」
- 使用者難以判斷實際過期時刻（特別是跨時區團隊）
- Telegram Bot API 9.x 支援 `date_time` MessageEntity，自動處理時區轉換
- 現有 `MessageBuilder` 已支援多種 entity (bold, italic, code)，可擴展支援 date_time

**Current behavior:**
```
⏰ 30 分鐘後過期
```

**Desired behavior:**
```
⏰ 過期時間：2026-03-14 15:30 (30 分鐘後)
            ^^^^^^^^^^^^^^^^^
            date_time entity（Telegram 自動轉本地時區）
```

---

## User Stories

### Story 1: MessageBuilder Supports date_time Entity
**As a** developer
**I want** to add date_time entities to Telegram messages
**So that** users see timestamps in their local timezone

**Acceptance Criteria:**
- [ ] `MessageBuilder` 新增 `def datetime(self, unix_ts: int) -> 'MessageBuilder'` method
- [ ] Method 自動格式化 Unix timestamp 為 ISO 8601 字串（顯示用）
- [ ] Method 記錄 `date_time` entity 到內部 entities 列表
- [ ] `to_dict()` 輸出包含正確的 entity offset, length, type
- [ ] Entity type 為 `"date_time"`（非 "custom_emoji"）

### Story 2: Approval Notification Shows Absolute Expiry Time
**As a** developer receiving approval request
**I want** to see the exact expiry time in my timezone
**So that** I can prioritize urgent approvals

**Acceptance Criteria:**
- [ ] `send_approval_request` 通知加入絕對過期時間
- [ ] 格式：`⏰ 過期時間：<date_time> (相對時間)`
- [ ] date_time entity 顯示格式：`YYYY-MM-DD HH:MM`（Telegram 自動轉時區）
- [ ] 相對時間保留（括號內）：`(30 分鐘後)`
- [ ] 若無 expires_at，不顯示此行

### Story 3: Other Time-Sensitive Notifications Use date_time
**As a** user
**I want** all time-sensitive notifications to show absolute times
**So that** I can better plan my actions

**Acceptance Criteria:**
- [ ] Survey: 識別所有顯示時間戳的通知
- [ ] Candidates:
  - Deploy start/completion timestamps
  - Approval granted/denied timestamps
  - Auto-approve timestamps
- [ ] 選擇 2-3 個高優先級通知更新為 date_time entity
- [ ] 其餘標記為 future enhancement

---

## Technical Design

### 1. Telegram Bot API date_time Entity Format

**Official API Spec (Bot API 9.x):**
```json
{
  "type": "date_time",
  "offset": 12,
  "length": 16
}
```

**Behavior:**
- Telegram client renders Unix timestamp as localized date/time
- Server sends ISO 8601 formatted string in message text
- Entity marks which substring is a datetime
- Client replaces with user's timezone equivalent

**Example:**
```
Text: "Meeting at 2026-03-14 15:30 UTC"
Entity: {"type": "date_time", "offset": 11, "length": 16}
Client shows: "Meeting at Mar 14, 3:30 PM" (if user in PST)
```

### 2. Update MessageBuilder Class

**Location:** `src/message_builder.py` (or equivalent)

**Add datetime method:**

```python
from datetime import datetime, timezone

class MessageBuilder:
    def __init__(self):
        self.text_content = ""
        self.entities = []

    def datetime(self, unix_ts: int, format: str = "%Y-%m-%d %H:%M") -> 'MessageBuilder':
        """
        Add a date_time entity to the message.

        Args:
            unix_ts: Unix timestamp (seconds since epoch)
            format: strftime format string for display text

        Returns:
            Self for chaining

        Example:
            mb.text("Expires at ").datetime(1710432600).text(" UTC")
            # Produces: "Expires at 2026-03-14 15:30 UTC"
            # With date_time entity on "2026-03-14 15:30"

        Telegram behavior:
            - Client shows timestamp in user's local timezone
            - Format is customized by client (not server-controlled)
        """
        # Convert Unix timestamp to datetime
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)

        # Format as display string
        formatted = dt.strftime(format)

        # Record entity
        offset = len(self.text_content)
        length = len(formatted)

        self.entities.append({
            'type': 'date_time',
            'offset': offset,
            'length': length,
        })

        # Append text
        self.text_content += formatted

        return self

    def to_dict(self) -> dict:
        """
        Convert to Telegram sendMessage API format.

        Returns:
            {
                "text": "...",
                "entities": [
                    {"type": "bold", "offset": 0, "length": 5},
                    {"type": "date_time", "offset": 10, "length": 16},
                    ...
                ]
            }
        """
        return {
            'text': self.text_content,
            'entities': self.entities,
        }
```

**Key implementation notes:**
- `date_time` entity does NOT include Unix timestamp in entity dict
- Timestamp is encoded in the display text (ISO 8601 format)
- Telegram client parses the text and converts to local timezone
- Server must use consistent datetime format for parsing to work

### 3. Update send_approval_request Notification

**Location:** `src/notifications.py:send_approval_request`

**Current implementation (excerpt):**
```python
def send_approval_request(
    project_id: str,
    deploy_id: str,
    changes: dict,
    timeout_minutes: int = 30,
    expires_at: Optional[int] = None,
) -> None:
    """Send approval request notification"""
    mb = MessageBuilder()

    # ... project, deploy info ...

    # Expiry info
    timeout_str = f"{timeout_minutes} 分鐘"
    mb.text("⏰ ").bold(f"{timeout_str}後過期").newline()

    # ... buttons ...
```

**Updated implementation:**
```python
def send_approval_request(
    project_id: str,
    deploy_id: str,
    changes: dict,
    timeout_minutes: int = 30,
    expires_at: Optional[int] = None,
) -> None:
    """
    Send approval request notification with absolute expiry time.

    Args:
        project_id: Project identifier
        deploy_id: Deploy identifier
        changes: Dict of infrastructure changes
        timeout_minutes: Relative timeout for display
        expires_at: Unix timestamp of expiry (if None, calculated from now + timeout)
    """
    mb = MessageBuilder()

    # ... project, deploy info ...

    # Calculate expires_at if not provided
    if expires_at is None:
        expires_at = int(time.time()) + (timeout_minutes * 60)

    # Expiry info with absolute + relative time
    mb.text("⏰ ").bold("過期時間：")
    mb.datetime(expires_at)  # date_time entity
    mb.text(f" ({timeout_minutes} 分鐘後)").newline()

    # ... buttons ...

    send_message(
        chat_id=get_notification_chat_id(project_id),
        message_builder=mb,
        reply_markup=build_approval_keyboard(deploy_id),
    )
```

**Visual result in Telegram:**
```
🚀 Deploy 批准請求

📦 專案：bouncer-api
🆔 Deploy ID：d-abc123
🔧 變更：Lambda: ApprovalFunction (Modify)
⏰ 過期時間：2026-03-14 15:30 (30 分鐘後)
            ^^^^^^^^^^^^^^^^^
            Telegram auto-converts to user's timezone

[✅ 批准] [❌ 拒絕]
```

### 4. Survey Other Notifications for date_time Usage

**Candidates:**

1. **Deploy completion notification** (`send_deploy_success`)
   - Current: 「✅ Deploy 完成於 2026-03-14 15:45」
   - Enhancement: Use date_time entity for timestamp
   - Priority: Medium

2. **Approval granted/denied** (`send_approval_result`)
   - Current: 「✅ 批准於 2026-03-14 15:32」
   - Enhancement: Use date_time entity
   - Priority: High (frequently viewed)

3. **Auto-approve notification** (`send_auto_approve_deploy_notification`)
   - Current: No timestamp (only shows Deploy ID)
   - Enhancement: Add "auto-approved at <date_time>"
   - Priority: Low (less time-sensitive)

4. **Deploy start notification** (`send_deploy_start`)
   - Current: No explicit timestamp
   - Enhancement: Add "started at <date_time>"
   - Priority: Low (Telegram message already has send time)

**Recommendation:**
- Implement date_time for #1 (deploy completion) and #2 (approval result) in this sprint
- Defer #3 and #4 to future sprints

### 5. Update send_approval_result Notification

**Location:** `src/notifications.py:send_approval_result`

**Current:**
```python
def send_approval_result(
    project_id: str,
    deploy_id: str,
    approved: bool,
    approved_by: str,
) -> None:
    mb = MessageBuilder()
    status = "✅ 批准" if approved else "❌ 拒絕"
    mb.text(f"{status} by {approved_by}").newline()
    # ... rest of message ...
```

**Updated:**
```python
def send_approval_result(
    project_id: str,
    deploy_id: str,
    approved: bool,
    approved_by: str,
    timestamp: Optional[int] = None,
) -> None:
    """
    Send approval result notification.

    Args:
        timestamp: Unix timestamp of approval action (default: now)
    """
    if timestamp is None:
        timestamp = int(time.time())

    mb = MessageBuilder()
    status = "✅ 批准" if approved else "❌ 拒絕"
    mb.text(f"{status} by {approved_by} at ")
    mb.datetime(timestamp)
    mb.newline()
    # ... rest of message ...
```

### 6. Update send_deploy_success Notification

**Location:** `src/notifications.py:send_deploy_success`

**Current:**
```python
def send_deploy_success(
    project_id: str,
    deploy_id: str,
    duration_seconds: int,
) -> None:
    mb = MessageBuilder()
    mb.text("✅ ").bold("Deploy 完成").newline()
    # ... deploy info ...
    mb.text(f"⏱ 耗時：{duration_seconds}s").newline()
```

**Updated:**
```python
def send_deploy_success(
    project_id: str,
    deploy_id: str,
    duration_seconds: int,
    completed_at: Optional[int] = None,
) -> None:
    """
    Send deploy success notification.

    Args:
        completed_at: Unix timestamp of completion (default: now)
    """
    if completed_at is None:
        completed_at = int(time.time())

    mb = MessageBuilder()
    mb.text("✅ ").bold("Deploy 完成").newline()
    # ... deploy info ...
    mb.text(f"⏱ 耗時：{duration_seconds}s").newline()
    mb.text("🕒 完成於：")
    mb.datetime(completed_at)
    mb.newline()
```

---

## Test Strategy

### Unit Tests

**File:** `tests/test_message_builder.py`

```python
def test_datetime_entity_basic():
    """Test datetime method adds correct entity"""
    mb = MessageBuilder()
    unix_ts = 1710432600  # 2026-03-14 15:30:00 UTC

    mb.text("Expires at ").datetime(unix_ts).text(" UTC")

    result = mb.to_dict()
    assert result['text'] == "Expires at 2026-03-14 15:30 UTC"
    assert len(result['entities']) == 1
    assert result['entities'][0] == {
        'type': 'date_time',
        'offset': 11,
        'length': 16,
    }

def test_datetime_entity_with_other_entities():
    """Test datetime entity works with bold/italic"""
    mb = MessageBuilder()
    mb.bold("Warning: ").text("Expires ").datetime(1710432600)

    result = mb.to_dict()
    # Should have both bold and date_time entities
    assert len(result['entities']) == 2
    assert result['entities'][0]['type'] == 'bold'
    assert result['entities'][1]['type'] == 'date_time'

def test_datetime_custom_format():
    """Test datetime with custom strftime format"""
    mb = MessageBuilder()
    mb.datetime(1710432600, format="%B %d, %Y at %I:%M %p")

    result = mb.to_dict()
    assert "March 14, 2026 at 03:30 PM" in result['text']
    assert result['entities'][0]['type'] == 'date_time'
```

**File:** `tests/test_notifications.py`

```python
def test_send_approval_request_includes_absolute_expiry():
    """Test approval notification shows absolute expiry time"""
    expires_at = 1710432600
    send_approval_request(
        project_id='test',
        deploy_id='d-123',
        changes={},
        timeout_minutes=30,
        expires_at=expires_at,
    )

    # Assert message contains date_time entity
    # Assert text includes "過期時間：2026-03-14 15:30 (30 分鐘後)"

def test_send_approval_request_calculates_expires_at():
    """Test approval notification calculates expires_at if not provided"""
    send_approval_request(
        project_id='test',
        deploy_id='d-123',
        changes={},
        timeout_minutes=30,
        expires_at=None,  # Should calculate
    )

    # Assert message contains date_time entity
    # Assert expires_at ~= now + 30min

def test_send_approval_result_includes_timestamp():
    """Test approval result shows timestamp as date_time entity"""
    timestamp = 1710432600
    send_approval_result(
        project_id='test',
        deploy_id='d-123',
        approved=True,
        approved_by='alice',
        timestamp=timestamp,
    )

    # Assert message contains "at 2026-03-14 15:30" with date_time entity

def test_send_deploy_success_includes_completion_time():
    """Test deploy success shows completion timestamp"""
    completed_at = 1710432600
    send_deploy_success(
        project_id='test',
        deploy_id='d-123',
        duration_seconds=120,
        completed_at=completed_at,
    )

    # Assert message contains "完成於：2026-03-14 15:30" with date_time entity
```

### Integration Tests

**File:** `tests/integration/test_telegram_entities.py`

```python
@pytest.mark.integration
def test_telegram_renders_datetime_entity(telegram_bot_token):
    """
    Test Telegram Bot API correctly renders date_time entity.

    This test sends a real message to a test chat and verifies:
    1. API accepts date_time entity without error
    2. Message is delivered successfully
    3. Entity offset/length are correct

    Note: Cannot verify client-side rendering without manual testing
    """
    mb = MessageBuilder()
    mb.text("Test message with ").datetime(int(time.time())).text(" timestamp")

    # Send to test chat
    response = send_message(
        chat_id=TEST_CHAT_ID,
        message_builder=mb,
    )

    assert response['ok'] is True
    assert 'message_id' in response['result']

    # Verify entities in sent message
    sent_entities = response['result']['entities']
    assert any(e['type'] == 'date_time' for e in sent_entities)
```

### Manual Testing Checklist

- [ ] Send approval request, verify absolute time displays correctly
- [ ] Check Telegram client shows time in local timezone (test with different timezone users)
- [ ] Approve request, verify approval result shows timestamp
- [ ] Complete deploy, verify success message shows completion time
- [ ] Verify date_time entity doesn't break on Telegram clients without support (should show raw text)
- [ ] Test with various timezones: UTC, PST, JST
- [ ] Verify relative time (括號內) still accurate

---

## Risk Assessment

**Medium Risk:**
- Telegram Bot API `date_time` entity is relatively new (Bot API 9.x)
- Older Telegram clients may not support it → **Mitigation:** Graceful degradation (shows raw text)
- Entity offset calculation must be precise → **Mitigation:** Comprehensive unit tests

**Low Risk:**
- Changes only affect notification display (no business logic)
- Backward compatible (new optional parameters)

**Telegram Client Compatibility:**
- Desktop: Telegram 4.x+ (supports date_time)
- Mobile: Telegram iOS 9.x+, Android 9.x+
- Web: Telegram Web A/K (supports date_time)
- **Fallback:** Clients without support show ISO 8601 text (still readable)

---

## Open Questions

1. **Should we show UTC or server timezone in display text?**
   - Answer: Always use UTC in text, Telegram client handles conversion
   - Format: `2026-03-14 15:30` (assume UTC, no explicit "UTC" suffix needed)

2. **What if user's Telegram client doesn't support date_time?**
   - Answer: Graceful degradation - shows ISO 8601 text
   - Still readable, just not localized

3. **Should we remove relative time (括號內) after date_time is shown?**
   - Answer: Keep both for redundancy
   - Relative time helps users quickly assess urgency
   - Absolute time helps with planning

4. **Performance impact of datetime() method?**
   - Answer: Negligible (simple timestamp conversion)
   - No API calls, just string formatting

---

## Future Enhancements

1. **Relative timestamp in date_time entity**
   - Telegram supports "2 hours ago" style entities
   - Requires different entity type: `text_mention` with date context

2. **Interactive time picker for approval timeout**
   - Inline keyboard button: "Extend timeout by 30m"
   - Updates message with new date_time entity

3. **Timezone preference per user**
   - Store user timezone in DynamoDB
   - Display text in preferred timezone (in addition to Telegram's auto-conversion)

---

## Related Issues

- Issue #42: Date-time MessageEntity (this implements it)
- Spec s39-001: Code-only diff summary (separate notification enhancement)
- Spec s39-002: Deploy progress checklist (separate progress enhancement)

---

## References

- [Telegram Bot API: MessageEntity](https://core.telegram.org/bots/api#messageentity)
- [Telegram Bot API: Formatting options](https://core.telegram.org/bots/api#formatting-options)
- [Python datetime strftime formats](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes)
