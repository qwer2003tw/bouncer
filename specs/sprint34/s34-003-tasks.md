# Sprint 34-003: Tasks — Trust Session IP binding upgrade

> Generated: 2026-03-12

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | config.py, trust.py, test_trust_ip_binding.py |
| D2 Cross-module | 2 | config ↔ trust (import dependency) |
| D3 Testing | 3 | 需新增 3 種 mode 測試 + fallback 測試 |
| D4 Infrastructure | 0 | 無 template.yaml 變更（env var 可在部署時設定） |
| D5 External | 0 | 無外部服務 |
| **Total TCS** | **8** | ✅ Medium — 不需拆分 |

## Task List

### Phase 1: Config Module

```
[003-T1] [P0] [US-3] src/config.py: Add IP_BINDING_MODE config variable
[003-T2] [P0] [US-3] src/config.py: Read from BOUNCER_IP_BINDING_MODE env var (default: 'warn')
[003-T3] [P0] [AC-5] src/config.py: Validate mode in ['strict', 'warn', 'disabled']
[003-T4] [P0] [AC-5] src/config.py: Fallback to 'warn' if invalid + log warning
```

### Phase 2: Trust Module

```
[003-T5] [P0] [US-1] src/trust.py: Import IP_BINDING_MODE from config
[003-T6] [P0] [AC-3] src/trust.py: Add 'disabled' mode branch (skip IP check)
[003-T7] [P0] [AC-1] src/trust.py: Add 'strict' mode branch (return False on mismatch)
[003-T8] [P0] [AC-2] src/trust.py: Keep 'warn' mode behavior (log + metric, return True)
[003-T9] [P0] [AC-1] src/trust.py: Emit 'trust_session_ip_blocked' metric in strict mode
```

### Phase 3: Testing

```
[003-T10] [P0] [AC-1] Test: strict mode + IP match → True
[003-T11] [P0] [AC-1] Test: strict mode + IP mismatch → False + metric
[003-T12] [P0] [AC-2] Test: warn mode + IP mismatch → True + metric
[003-T13] [P0] [AC-3] Test: disabled mode + IP mismatch → True + no metric
[003-T14] [P1] [AC-4] Test: no env var set → default to 'warn'
[003-T15] [P1] [AC-5] Test: invalid mode → fallback to 'warn' + log
[003-T16] [P1] Regression test: run existing test_trust_ip_binding.py tests
```

### Phase 4: Documentation

```
[003-T17] [P2] Add inline comments explaining 3 modes in trust.py
[003-T18] [P2] Update spec with usage recommendations (when to use each mode)
```

### Phase 5: Integration Testing

```
[003-T19] [P1] Integration test: strict mode blocks real IP mismatch
[003-T20] [P1] Integration test: warn mode allows real IP mismatch
[003-T21] [P2] Integration test: disabled mode skips check
```

## Execution Order

```
Phase 1: Config
T1 → T2 → T3 → T4

Phase 2: Trust
T5 → T6 → T7 → T8 → T9

Phase 3: Testing
T10 → T11 → T12 → T13 → T14 → T15 → T16

Phase 4: Documentation (parallel with testing)
T17 → T18

Phase 5: Integration (after unit tests pass)
T19 → T20 → T21
```

## Critical Path

```
T1-T4 → T5-T9 → T10-T13 → T19-T20
```

Non-critical tasks: T14-T16 (fallback test), T17-T18 (docs), T21 (disabled mode integration)

## Estimated Effort

- Phase 1 (Config): 20 min (T1-T4)
- Phase 2 (Trust): 30 min (T5-T9)
- Phase 3 (Testing): 45 min (T10-T16)
- Phase 4 (Documentation): 15 min (T17-T18)
- Phase 5 (Integration): 25 min (T19-T21)

**Total**: ~135 min (2.25 hrs)

## Success Metrics

- ✅ All 3 modes tested and working
- ✅ Default mode is 'warn'
- ✅ Invalid mode falls back to 'warn'
- ✅ Strict mode blocks IP mismatch
- ✅ Warn mode allows IP mismatch (with log/metric)
- ✅ Disabled mode skips IP check
- ✅ Existing trust tests pass (no regression)
- ✅ Code coverage ≥ 75% for modified functions

## Risk Mitigation

| Task | Risk | Mitigation |
|------|------|------------|
| T7 | Strict mode breaks Telegram | 文件明確說明 strict 只適合單一 IP source |
| T11 | Test 誤判 | Mock config + trust module carefully |
| T16 | Regression | 執行完整 trust test suite |
