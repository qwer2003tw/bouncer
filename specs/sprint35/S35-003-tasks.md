# Sprint 35-003: Tasks — Deploy Approval Timeout Notification

> Generated: 2026-03-13

## TCS Score: 12 (Medium)

## Task List

### Phase 1: Research
```
[003-T1] [P0] Read src/scheduler_service.py — 確認現有 API
[003-T2] [P0] Read src/mcp_execute.py _submit_for_approval() — 確認 TTL 計算
[003-T3] [P0] Check template.yaml SchedulerManageExpiry policy — 確認涵蓋 delete
[003-T4] [P0] Check if NotifierLambda can handle expiry_warning action (or need new lambda)
```

### Phase 2: Infrastructure (template.yaml)
```
[003-T5] [P1] Confirm/add scheduler:DeleteSchedule to SchedulerManageExpiry IAM policy
[003-T6] [P1] Add IAM for expiry notification handler if needed
```

### Phase 3: Scheduler Integration (mcp_execute.py + scheduler_service.py)
```
[003-T7]  [P0] src/scheduler_service.py: create_expiry_schedule(request_id, trigger_at, payload)
[003-T8]  [P0] src/scheduler_service.py: delete_expiry_schedule(request_id) — best-effort
[003-T9]  [P0] src/mcp_execute.py _submit_for_approval(): call create_expiry_schedule after DDB write
[003-T10] [P0] src/callbacks.py approve/deny paths: call delete_expiry_schedule(request_id)
```

### Phase 4: Notification (notifications.py)
```
[003-T11] [P0] src/notifications.py: send_expiry_warning_notification(request_id, command_preview, source)
[003-T12] [P0] deployer/notifier/app.py OR src/app.py: handle action="expiry_warning" from scheduler
```

### Phase 5: Tests
```
[003-T13] [P0] Test: _submit_for_approval creates schedule after DDB write
[003-T14] [P0] Test: approve callback deletes schedule
[003-T15] [P0] Test: deny callback deletes schedule
[003-T16] [P1] Test: send_expiry_warning_notification format + content
[003-T17] [P1] Test: schedule delete failure is best-effort (no exception propagation)
```

### Phase 6: Integration
```
[003-T18] [P1] Manual test: submit request → wait ~4min → verify Telegram ⏰ notification
[003-T19] [P1] Manual test: submit + approve → verify NO notification sent
```

## Critical Path
```
T1-T4 [Research]
  → T7-T8 [Scheduler service]
  → T9-T10 [mcp_execute + callbacks integration]
  → T11-T12 [Notification handler]
  → T13-T17 [Tests]
  → T18-T19 [Integration]
```

T5-T6 (IAM) can be done in parallel with T7-T8.

## Success Metrics
- ✅ Expiry schedule created on every approval request submit
- ✅ Schedule deleted on approve/deny (best-effort)
- ✅ ⏰ Telegram notification sent 60s before TTL expiry
- ✅ No notification sent if already approved/denied
- ✅ Coverage ≥ 75%
- ✅ Security: payload contains preview only, not full command

## Estimated Effort: ~6 hours
