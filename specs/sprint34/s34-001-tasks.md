# Sprint 34-001: Tasks — Fix auto_approved notification bug

> Generated: 2026-03-12

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | notifications.py (1個主檔案) |
| D2 Cross-module | 0 | 僅修改 notifications.py 內部邏輯 |
| D3 Testing | 2 | 需補 regression test |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無外部服務 |
| **Total TCS** | **3** | ✅ Simple — 不需拆分 |

## Task List

### Research

```
[001-T1] [P0] Read src/notifications.py line 350-370 (trust path logic)
[001-T2] [P0] Read src/notifications.py line 490-510 (auto-approved path logic)
[001-T3] [P0] Locate extract_exit_code() function definition
[001-T4] [P1] Check if _is_execute_failed() exists and wraps extract_exit_code()
```

### Core Implementation

```
[001-T5] [P0] [US-1] Modify src/notifications.py ~line 500: replace startswith('❌') with extract_exit_code()
[001-T6] [P0] [US-2] Verify logic: exit_code != 0 → ❌, exit_code == 0 → ✅
```

### Testing

```
[001-T7]  [P0] [AC-3] Add test: auto-approved exit 0 → ✅ notification
[001-T8]  [P0] [AC-3] Add test: auto-approved exit 1 → ❌ notification
[001-T9]  [P1] [AC-3] Add test: auto-approved exit 127 → ❌ notification
[001-T10] [P1] [AC-4] Run existing trust path tests: pytest tests/test_trust*.py -v
[001-T11] [P2] [AC-4] Run existing notification tests: pytest tests/test_notifications.py -v
```

### Integration Testing (Manual)

```
[001-T12] [P1] Manual test: bouncer exec --auto-approve "exit 0" → Telegram ✅
[001-T13] [P1] Manual test: bouncer exec --auto-approve "exit 1" → Telegram ❌
[001-T14] [P2] Manual test: trust session auto-approve → no regression
```

### Documentation

```
[001-T15] [P2] Update commit message to reference #102, #116
[001-T16] [P2] Add inline comment explaining why extract_exit_code() is used
```

## Execution Order

```
Phase 1: Research
T1 → T2 → T3 → T4

Phase 2: Implementation
T5 → T6

Phase 3: Testing
T7 → T8 → T9 → T10 → T11

Phase 4: Integration
T12 → T13 → T14

Phase 5: Documentation
T15 → T16
```

## Critical Path

```
T1-T4 → T5-T6 → T7-T9 → T12-T13
```

所有其他 tasks (T10-T11, T14-T16) 是 non-blocking，可並行或延後。

## Estimated Effort

- Research: 15 min (T1-T4)
- Implementation: 10 min (T5-T6)
- Testing: 30 min (T7-T11)
- Integration: 15 min (T12-T14)
- Documentation: 5 min (T15-T16)

**Total**: ~75 min (1.25 hrs)

## Success Metrics

- ✅ All regression tests pass
- ✅ Manual integration test shows correct ❌/✅ for exit codes
- ✅ Trust path tests pass (no regression)
- ✅ Code coverage ≥ 75% for modified functions
