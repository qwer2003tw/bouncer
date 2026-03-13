# Sprint 34-004: Tasks — Expandable blockquote for long command output

> Generated: 2026-03-12

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 4 | telegram_entities.py, telegram.py, notifications.py, tests |
| D2 Cross-module | 4 | telegram_entities ↔ notifications ↔ telegram (interface changes) |
| D3 Testing | 3 | 需測試 entities builder + offset adjustment + integration |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | Telegram API（已知，不是新 service） |
| **Total TCS** | **11** | ✅ Medium — 不需拆分 |

## Task List

### Research Phase (必須先執行)

```
[004-T1] [P0] Verify Telegram Bot API supports expandable_blockquote entity type
[004-T2] [P0] Read src/telegram.py: check if send_telegram_message() supports entities parameter
[004-T3] [P0] Read src/notifications.py: locate command result notification paths
[004-T4] [P0] DECISION: Does send_telegram_message need modification? (YES/NO)
```

### Phase 1: Entities Builder

```
[004-T5] [P0] [US-1] src/telegram_entities.py: Add format_command_output() function
[004-T6] [P0] [AC-2] format_command_output(): Handle short output (≤50 lines) → no blockquote
[004-T7] [P0] [AC-1] format_command_output(): Handle long output (>50 lines) → expandable blockquote
[004-T8] [P0] [AC-3] format_command_output(): Handle empty output → "(no output)"
```

### Phase 2: Telegram Module (Conditional)

```
[004-T9]  [P0] [Conditional] src/telegram.py: Add entities parameter to send_telegram_message() (if T4=YES)
[004-T10] [P0] [Conditional] src/telegram.py: Pass entities to Telegram API payload (if T4=YES)
```

### Phase 3: Notifications Integration

```
[004-T11] [P0] [US-1] src/notifications.py: Import format_command_output
[004-T12] [P0] [AC-4] src/notifications.py: Integrate format_command_output in send_execute_result()
[004-T13] [P0] [AC-4] src/notifications.py: Adjust entity offsets for header text
[004-T14] [P1] [US-1] src/notifications.py: Integrate in send_auto_approved_notification() (if exists)
```

### Phase 4: Testing

```
[004-T15] [P0] [AC-2] Test: format_command_output with 10 lines → no blockquote
[004-T16] [P0] [AC-1] Test: format_command_output with 60 lines → expandable blockquote
[004-T17] [P0] [AC-3] Test: format_command_output with empty string → "(no output)"
[004-T18] [P0] Test: format_command_output with custom threshold
[004-T19] [P0] [AC-4] Test: send_execute_result long output → entities with correct offset
[004-T20] [P0] [AC-2] Test: send_execute_result short output → no entities
[004-T21] [P1] [AC-5] Regression test: other notifications unchanged (trust summary, deploy status)
```

### Phase 5: Integration Testing

```
[004-T22] [P1] Manual test: exec command with >50 lines output → Telegram expandable blockquote
[004-T23] [P1] Manual test: exec command with <50 lines output → Telegram normal text
[004-T24] [P2] Manual test: verify expand/collapse behavior in Telegram
[004-T25] [P2] Manual test: verify exit code visible outside blockquote
```

### Phase 6: Documentation

```
[004-T26] [P2] Add inline comments in format_command_output() explaining threshold logic
[004-T27] [P2] Update spec with verification results
```

## Execution Order

```
Research Phase (always):
T1 → T2 → T3 → T4

Phase 1: Entities
T5 → T6 → T7 → T8

Phase 2: Telegram (conditional):
if T4=YES: T9 → T10

Phase 3: Notifications:
T11 → T12 → T13 → T14

Phase 4: Testing:
T15 → T16 → T17 → T18 → T19 → T20 → T21

Phase 5: Integration:
T22 → T23 → T24 → T25

Phase 6: Documentation:
T26 → T27
```

## Critical Path

```
T1-T4 [Research]
  → T5-T8 [Entities builder]
  → T9-T10 [Telegram module, if needed]
  → T11-T13 [Notifications integration]
  → T15-T20 [Testing]
  → T22-T23 [Manual integration]
```

Non-critical: T14 (auto-approved path), T21 (regression), T24-T27 (docs)

## Estimated Effort

- Research: 15 min (T1-T4)
- Phase 1 (Entities): 25 min (T5-T8)
- Phase 2 (Telegram, if needed): 15 min (T9-T10)
- Phase 3 (Notifications): 30 min (T11-T14)
- Phase 4 (Testing): 50 min (T15-T21)
- Phase 5 (Integration): 25 min (T22-T25)
- Phase 6 (Documentation): 10 min (T26-T27)

**Total (if send_telegram_message needs modification)**: ~170 min (2.8 hrs)
**Total (if no modification needed)**: ~155 min (2.6 hrs)

## Success Metrics

- ✅ Telegram Bot API supports expandable_blockquote (verified)
- ✅ `format_command_output()` correctly handles short/long/empty output
- ✅ Entity offsets adjusted correctly for header text
- ✅ Long command output displays in expandable blockquote
- ✅ Short command output displays normally
- ✅ Exit code visible outside blockquote
- ✅ Other notifications unchanged (regression tests pass)
- ✅ Code coverage ≥ 75% for modified functions

## Risk Mitigation

| Task | Risk | Mitigation |
|------|------|------------|
| T1 | Telegram API 不支援 expandable_blockquote | Research phase 先驗證文件 |
| T4 | send_telegram_message 需大幅修改 | 先檢查現有實作，entities 參數簡單 |
| T13 | Entity offset 計算錯誤 | 詳細測試 + 手動驗證 Telegram UI |
| T21 | 影響其他 notifications | 只在 command result path 使用 |

## Complexity Notes

TCS=11 (Medium) 主要來自：
- **D2=4**: Cross-module interface changes (telegram_entities ↔ notifications ↔ telegram)
- **D1=4**: 多個檔案修改（entities builder + telegram + notifications + tests）

但整體設計清晰，不需拆分。可以單一 PR 完成。
