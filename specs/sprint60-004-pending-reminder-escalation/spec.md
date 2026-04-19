# Feature Specification: Pending Reminder Escalation

Feature Branch: feat/sprint60-004-pending-reminder-escalation
Sprint: 60
Task ID: bouncer-s60-004

## Problem Statement

Sprint 59 實作的 pending reminder（`create_pending_reminder_schedule`）只發一次提醒。如果 Steven 錯過第一次提醒（例如在開會或睡覺），該請求可能到期而無人審批，agent 端也無法繼續工作。

### 現行行為

1. 命令提交後，`mcp_execute.py` line 913 呼叫 `create_pending_reminder_schedule(reminder_minutes=10)`
2. 10 分鐘後 EventBridge Scheduler 觸發 Lambda
3. Lambda 檢查請求狀態，若仍 `pending_approval` → 發 Telegram silent message
4. **Schedule 設定 `ActionAfterCompletion="DELETE"`** → 執行一次後自動刪除
5. 如果 Steven 沒看到 → 再也不提醒

### 現行 constants

```python
# src/constants.py line 102
PENDING_REMINDER_MINUTES = 10  # 請求發出後 N 分鐘未審批 → 自動提醒
```

### 現行 scheduler_service.py interface

```python
def create_pending_reminder_schedule(
    self,
    request_id: str,
    expires_at: int,
    *,
    reminder_minutes: int = 10,
    command_preview: str = '',
    source: str = '',
) -> bool:
```

---

## User Scenarios & Testing

### User Story 1：Escalation reminder（第二次提醒）

> 作為 Bouncer 使用者，我需要在第一次提醒後若仍未審批，系統再發一次 escalation 提醒，以確保重要請求不被遺漏。

**Given** 一個 pending_approval 請求在 10 分鐘後觸發第一次 reminder
**And** Steven 未在第一次 reminder 後採取行動
**When** 又過了 20 分鐘（請求提交後 30 分鐘）
**Then** 系統發送第二次 escalation reminder（訊息標記為 escalation）
**And** 第二次提醒的訊息格式包含 `🔴 第 2 次提醒` 標記
**And** 不再發送更多提醒（最多 2 次）

**Given** 第一次 reminder 發送後 Steven 已審批
**When** 第二次 reminder 觸發
**Then** 檢查到狀態不再是 `pending_approval` → 跳過（不發送）

### User Story 2：可配置的提醒間隔

> 作為管理員，我可以透過環境變數配置提醒間隔，不需要改 code。

**Given** 環境變數 `PENDING_REMINDER_MINUTES` 設為 `15`
**When** 新請求提交
**Then** 第一次提醒在 15 分鐘後，第二次在 30 分鐘後（= 15 × 2）

**Given** 環境變數未設定
**When** 新請求提交
**Then** 使用預設值 10 分鐘

---

## Requirements

### FR-001：Escalation schedule 建立
- 在 `create_pending_reminder_schedule()` 成功後，**額外建立**一個 escalation schedule
- Escalation 時間 = `now + (reminder_minutes * 3)`（第一次 10min，escalation 30min）
- Schedule name 格式：`bouncer-escalation-{request_id[:12]}`
- 同樣設定 `ActionAfterCompletion="DELETE"`

### FR-002：最多 2 次提醒
- 第一次：`bouncer-reminder-{request_id[:12]}`（現有）
- 第二次：`bouncer-escalation-{request_id[:12]}`（新增）
- **不建立第三次** — 避免 notification spam
- Escalation payload 新增 `"escalation": true` 欄位

### FR-003：Escalation 訊息格式
- 在 `app.py` 的 `pending_reminder` handler 中區分 escalation
- 若 payload 包含 `"escalation": true`：
  ```
  🔴 *第 2 次提醒 — 尚未審批的請求*
  ```
- 若否（第一次提醒）保持現有格式：
  ```
  ⏰ *尚未審批的請求*
  ```

### FR-004：`PENDING_REMINDER_MINUTES` 環境變數
- `src/constants.py` 的 `PENDING_REMINDER_MINUTES` 改為讀取 `os.environ.get('PENDING_REMINDER_MINUTES', '10')`
- 加入 int() 轉換和 fallback

### FR-005：Escalation 不超過 expires_at
- 若 escalation 時間 >= expires_at → 不建立 escalation schedule
- 與現有 reminder 的 `if reminder_time >= expires_at: return False` 邏輯一致

### FR-006：審批後清理 escalation schedule
- 現有的 `delete_schedule()` 在審批後刪除 reminder schedule
- 新增：也刪除 escalation schedule（`bouncer-escalation-{request_id[:12]}`）
- 使用 best-effort 刪除（`ActionAfterCompletion=DELETE` 已處理大多數情況）

---

## Interface Contract

### scheduler_service.py 變更

```python
def create_pending_reminder_schedule(
    self,
    request_id: str,
    expires_at: int,
    *,
    reminder_minutes: int = 10,
    command_preview: str = '',
    source: str = '',
) -> bool:
    """Create reminder + escalation schedules.

    Creates two one-time schedules:
    1. Reminder: fires at now + reminder_minutes
    2. Escalation: fires at now + (reminder_minutes * 3)

    Both auto-delete after execution (ActionAfterCompletion=DELETE).
    """
    # ... existing reminder logic ...
    
    # NEW: Create escalation schedule
    escalation_time = now + (reminder_minutes * 3 * 60)
    if escalation_time < expires_at:
        # Create escalation schedule with "escalation": true in payload
        ...
```

### constants.py 變更

```python
# 原：
PENDING_REMINDER_MINUTES = 10

# 改為：
import os
PENDING_REMINDER_MINUTES = int(os.environ.get('PENDING_REMINDER_MINUTES', '10'))
```

### app.py handler 變更

```python
# 在 pending_reminder handler 中（line ~477）：
is_escalation = event.get('escalation', False)
emoji = "🔴" if is_escalation else "⏰"
header = "第 2 次提醒 — 尚未審批的請求" if is_escalation else "尚未審批的請求"
text = f"{emoji} *{header}*\n\n..."
```

### template.yaml 變更

- 若 `PENDING_REMINDER_MINUTES` 需透過 Lambda env var 傳入：
  ```yaml
  Environment:
    Variables:
      PENDING_REMINDER_MINUTES: !Ref PendingReminderMinutes  # 或直接硬編 "10"
  ```
- **建議**：先用 constants.py 的 `os.environ.get()` + Lambda env var default，不加 Parameter（簡化）

---

## TCS 計算

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| D1 Files | 2/5 | 3 files：`scheduler_service.py`、`constants.py`、`app.py`（+ template.yaml 若加 env var） |
| D2 Cross-module | 1/4 | scheduler_service ↔ app.py 透過 event payload 溝通，加一個 field |
| D3 Testing | 2/4 | 測試 escalation schedule 建立、escalation message format、env var 配置 |
| D4 Infrastructure | 1/4 | template.yaml 可能需加 env var（optional）|
| D5 External | 1/4 | EventBridge Scheduler API 呼叫（create_schedule）、Telegram API |

**Total TCS: 7 (Simple)**
→ Sub-agent strategy: 1 agent timeout 600s

---

## Cost Analysis

### EventBridge Scheduler 成本

- EventBridge Scheduler: 前 14,000,000 次 schedule invocations/month 免費
- 每個 pending 請求最多建 2 個 one-time schedules
- Bouncer 日均請求量：~50-100 requests
- 最多 200 schedules/day × 30 = 6,000 schedules/month → **免費**
- Schedule 在執行後自動刪除（`ActionAfterCompletion=DELETE`），不佔用 schedule 配額

### Telegram API 成本

- 無額外成本（已在使用中）

### 總新增成本

**$0/month**（全在免費額度內）

---

## Success Criteria

- SC-001：第一次 reminder 後若未審批，30 分鐘後發 escalation
- SC-002：Escalation 訊息包含 `🔴 第 2 次提醒` 標記
- SC-003：已審批的請求不發 escalation
- SC-004：最多 2 次提醒（不會 spam）
- SC-005：`PENDING_REMINDER_MINUTES` env var 可配置
- SC-006：Escalation 時間不超過 expires_at
- SC-007：所有既有 tests 通過
- SC-008：test_sprint59.py 中的 reminder tests 不受影響
