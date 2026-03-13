# Sprint 34-002: Tasks — Immediate feedback after deploy approval

> Generated: 2026-03-12

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | callbacks.py (1個檔案) |
| D2 Cross-module | 0 | 僅修改 callbacks.py 內部 |
| D3 Testing | 1 | 補測試（如需實作） |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 0 | 無外部服務 |
| **Total TCS** | **2** | ✅ Simple — 不需拆分 |

## Task List

### Research (必須先執行)

```
[002-T1] [P0] Read src/callbacks.py: locate handle_deploy_callback()
[002-T2] [P0] Analyze handle_deploy_callback() immediate feedback implementation
[002-T3] [P0] Read src/callbacks.py: locate handle_execute_callback()
[002-T4] [P0] Check if handle_execute_callback() has immediate feedback
[002-T5] [P0] DECISION: Does execute callback need implementation? (YES/NO)
```

### Core Implementation (Conditional — only if T5 = YES)

```
[002-T6] [P0] [US-1] Add update_message() call to handle_execute_callback()
[002-T7] [P0] [US-2] Align message format with deploy callback (icon + text)
[002-T8] [P1] [US-1] Verify call order: update_message → execute_command
```

### Testing (Conditional — only if T5 = YES)

```
[002-T9]  [P0] [AC-1] Add unit test: mock update_message, verify call in execute callback
[002-T10] [P0] [AC-1] Add unit test: verify call order (update before execute)
[002-T11] [P1] [AC-2] Run existing deploy callback tests: pytest -k "deploy" -v
[002-T12] [P2] [AC-3] Integration test: manual approval flow verification
```

### No-op Path (if T5 = NO)

```
[002-T13] [P0] Update s34-002-spec.md: mark as "已實作，無需變更"
[002-T14] [P1] Run existing callback tests to verify functionality
[002-T15] [P2] Document findings in spec
```

## Execution Order

### Research Phase (Always)
```
T1 → T2 → T3 → T4 → T5
```

### If T5 = YES (Implementation needed)
```
T6 → T7 → T8 → T9 → T10 → T11 → T12
```

### If T5 = NO (No-op)
```
T13 → T14 → T15 → DONE
```

## Critical Path

```
T1-T5 [Research] → Decision Point
  ├─ YES → T6-T12 [Implement + Test]
  └─ NO  → T13-T15 [Document + Close]
```

## Estimated Effort

### Research Phase (always)
- T1-T5: 10 min

### Implementation Path (if needed)
- T6-T8: 15 min
- T9-T12: 25 min
- **Total**: ~50 min

### No-op Path (if not needed)
- T13-T15: 5 min
- **Total**: ~15 min

**Expected**: 15-65 min (depending on research outcome)

## Success Metrics

### If implementation needed:
- ✅ Execute callback has immediate feedback
- ✅ Unit tests pass
- ✅ Integration test confirms UX improvement
- ✅ Deploy callback not affected (regression tests pass)

### If no-op:
- ✅ Research confirms feature already exists
- ✅ Existing tests pass
- ✅ Spec updated to reflect findings
