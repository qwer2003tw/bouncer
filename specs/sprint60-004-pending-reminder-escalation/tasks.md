# Tasks: Pending Reminder Escalation

Sprint: 60 | Task: bouncer-s60-004

## Phase 1: Setup

```bash
cd /home/ec2-user/projects/bouncer
git worktree add /tmp/s60-004-pending-reminder feat/sprint60-004-pending-reminder-escalation -b feat/sprint60-004-pending-reminder-escalation
cd /tmp/s60-004-pending-reminder
```

## Phase 2: Analysis

### Task 2.1：確認現行 reminder 機制

```bash
# 確認 create_pending_reminder_schedule 完整邏輯
sed -n '271,360p' src/scheduler_service.py

# 確認 app.py 的 reminder handler
sed -n '477,515p' src/app.py

# 確認 reminder_schedule_name helper
grep -n "def reminder_schedule_name\|def escalation_schedule_name" src/scheduler_service.py

# 確認 delete_schedule 是否刪除 reminder
grep -n "delete_schedule\|reminder" src/scheduler_service.py | head -20

# 確認 constants
grep -n "PENDING_REMINDER" src/constants.py
```

### Task 2.2：確認 Sprint 59 tests

```bash
grep -n "pending_reminder\|reminder" tests/test_sprint59.py | head -20
```

## Phase 3: Implementation

### Task 3.1：修改 `src/constants.py`（line 102）

```python
# 原：
PENDING_REMINDER_MINUTES = 10  # 請求發出後 N 分鐘未審批 → 自動提醒

# 改為：
import os  # 若尚未 import
PENDING_REMINDER_MINUTES = int(os.environ.get('PENDING_REMINDER_MINUTES', '10'))  # 請求發出後 N 分鐘未審批 → 自動提醒
```

⚠️ 確認 `os` 是否已在 constants.py 頂部 import。

### Task 3.2：修改 `src/scheduler_service.py`

**3.2a — 新增 escalation_schedule_name helper**

```python
def escalation_schedule_name(request_id: str) -> str:
    """Generate escalation schedule name from request_id."""
    return f"bouncer-escalation-{request_id[:12]}"
```

**3.2b — 在 `create_pending_reminder_schedule()` 成功建立 reminder 後，加入 escalation 邏輯**

在現有的 `logger.info("Created reminder schedule...")` 之後（line ~346），加入：

```python
            # Sprint 60: Create escalation schedule (2nd reminder)
            escalation_time = now + (reminder_minutes * 3 * 60)
            if escalation_time < expires_at:
                try:
                    esc_name = escalation_schedule_name(request_id)
                    esc_at_expr = _format_schedule_time(escalation_time)
                    esc_payload = {
                        "source": "bouncer-scheduler",
                        "action": "pending_reminder",
                        "request_id": request_id,
                        "command_preview": command_preview,
                        "source_field": source,
                        "escalation": True,
                    }
                    client.create_schedule(
                        Name=esc_name,
                        GroupName=self._group_name,
                        ScheduleExpression=esc_at_expr,
                        ScheduleExpressionTimezone="UTC",
                        FlexibleTimeWindow={"Mode": "OFF"},
                        ActionAfterCompletion="DELETE",
                        Target={
                            "Arn": self._lambda_arn,
                            "RoleArn": self._role_arn,
                            "Input": json.dumps(esc_payload),
                        },
                    )
                    logger.info("Created escalation schedule '%s' for request %s at %s", esc_name, request_id, esc_at_expr,
                                extra={"src_module": "scheduler", "operation": "create_escalation_schedule", "request_id": request_id})
                except ClientError as exc:
                    logger.warning("Failed to create escalation schedule for %s: %s", request_id, exc,
                                   extra={"src_module": "scheduler", "operation": "create_escalation_schedule", "request_id": request_id, "error": str(exc)})
```

**3.2c — 更新 `delete_schedule()` 或新增 escalation cleanup**

確認現有的 cleanup 邏輯（在審批 callback 中）是否需要額外刪除 escalation schedule。

```bash
grep -n "delete_schedule\|delete.*reminder\|delete.*escalation" src/ -r | head -20
```

若 `delete_schedule()` 只刪 expiry schedule，需新增 escalation schedule 的 best-effort 刪除：

```python
def delete_escalation_schedule(self, request_id: str) -> bool:
    """Delete escalation schedule (best-effort)."""
    try:
        client = self._get_client()
        name = escalation_schedule_name(request_id)
        client.delete_schedule(Name=name, GroupName=self._group_name)
        return True
    except ClientError:
        return True  # Schedule may not exist or already deleted
```

### Task 3.3：修改 `src/app.py` pending_reminder handler

在 `pending_reminder` handler（line ~477）中加入 escalation 判斷：

```python
    if event.get('source') == 'bouncer-scheduler' and event.get('action') == 'pending_reminder':
        request_id = event.get('request_id', '')
        is_escalation = event.get('escalation', False)  # NEW
        # ... existing status check ...

        try:
            # ... existing Telegram message logic ...
            
            # 修改訊息 header
            if is_escalation:
                header = "🔴 *第 2 次提醒 — 尚未審批的請求*"
            else:
                header = "⏰ *尚未審批的請求*"

            text = (
                f"{header}\n\n"
                f"📋 *命令：* `{escape_markdown(command_preview)}`\n"
                f"🤖 *來源：* {escape_markdown(source_field)}\n"
                f"🆔 `{request_id}`\n"
                f"⌛ *到期：* {expires_str}"
            )
```

### Task 3.4：在審批 callback 中清理 escalation schedule（可選）

```bash
# 找到 approval callback 中呼叫 delete_schedule 的位置
grep -n "delete.*schedule\|scheduler.*delete" src/callbacks*.py | head -10
```

在現有的 reminder schedule 刪除邏輯旁邊，加入 escalation schedule 刪除。

## Phase 4: Tests

### Task 4.1：新增 escalation tests

在 `tests/test_sprint59.py` 或新建 `tests/test_sprint60_reminder.py` 中：

```python
def test_escalation_schedule_created():
    """成功建立 reminder 後也建立 escalation schedule"""
    with patch.object(scheduler_service, '_get_client') as mock_client:
        svc = SchedulerService(enabled=True, lambda_arn='arn:...', role_arn='arn:...')
        result = svc.create_pending_reminder_schedule(
            request_id='req-123456789012',
            expires_at=int(time.time()) + 3600,  # 1 hour
            reminder_minutes=10,
        )
        assert result is True
        # create_schedule 應被呼叫 2 次（reminder + escalation）
        assert mock_client.return_value.create_schedule.call_count == 2
        
        # 第二次呼叫的 payload 包含 escalation: True
        second_call = mock_client.return_value.create_schedule.call_args_list[1]
        payload = json.loads(second_call[1]['Target']['Input'])
        assert payload['escalation'] is True

def test_escalation_skipped_if_exceeds_expiry():
    """escalation 時間超過 expires_at → 不建立"""
    with patch.object(scheduler_service, '_get_client') as mock_client:
        svc = SchedulerService(enabled=True, lambda_arn='arn:...', role_arn='arn:...')
        result = svc.create_pending_reminder_schedule(
            request_id='req-123456789012',
            expires_at=int(time.time()) + 900,  # 15 min（escalation 30min > 15min）
            reminder_minutes=10,
        )
        # 只建立 1 個 schedule（reminder only，escalation 跳過）
        assert mock_client.return_value.create_schedule.call_count == 1

def test_escalation_message_format():
    """escalation reminder 的訊息格式包含 🔴"""
    event = {
        'source': 'bouncer-scheduler',
        'action': 'pending_reminder',
        'request_id': 'req-123',
        'escalation': True,
        'command_preview': 'aws s3 ls',
        'source_field': 'Agent',
    }
    # ... invoke handler, check Telegram message contains '🔴'

def test_env_var_pending_reminder_minutes():
    """PENDING_REMINDER_MINUTES env var 生效"""
    with patch.dict(os.environ, {'PENDING_REMINDER_MINUTES': '15'}):
        import importlib
        import constants
        importlib.reload(constants)
        assert constants.PENDING_REMINDER_MINUTES == 15
```

### Task 4.2：跑既有 reminder tests

```bash
python -m pytest tests/test_sprint59.py -v -k "reminder"
python -m pytest tests/ -x --tb=short 2>&1 | tail -30
```

## Phase 5: Lint & Commit

```bash
ruff check src/constants.py src/scheduler_service.py src/app.py
git add src/constants.py src/scheduler_service.py src/app.py tests/
git commit -m "feat: pending reminder escalation — 2nd reminder after 30min (#s60-004)

- Add escalation schedule (fires at reminder_minutes * 3)
- Escalation message marked with 🔴 第 2 次提醒
- Max 2 reminders (no spam)
- PENDING_REMINDER_MINUTES configurable via env var
- Escalation skipped if exceeds expires_at
- Best-effort cleanup on approval
"
```

## TCS Summary

TCS=7 → 1 agent timeout 600s

⚠️ **注意事項**：
1. EventBridge Scheduler `create_schedule` 可能遇到 rate limit — 兩次呼叫間無需 delay（one-time schedule 不高頻）
2. `ActionAfterCompletion=DELETE` 確保 schedule 執行後自動刪除，不佔配額
3. 若 `constants.py` 使用 `os.environ.get()` 在 import time 讀取，Lambda cold start 時讀取環境變數，hot start 時使用 cached 值 — 這是預期行為
