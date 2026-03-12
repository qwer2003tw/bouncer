# Implementation Plan: Logging [TAG] Prefix → Structured Extra Fields 收尾

## Technical Context
- 影響檔案（依優先順序）：
  - P0: `src/app.py` (21 calls), `src/mcp_execute.py` (20 calls)
  - P1: `src/notifications.py` (10), `src/telegram.py` (9), `src/callbacks.py` (9)
  - P2: `src/mcp_deploy_frontend.py` (7), `src/mcp_upload.py` (6), `src/deployer.py` (6)
  - P3: `src/risk_scorer.py` (5), `src/scheduler_service.py` (4), `src/mcp_history.py` (4)
  - P4: `src/paging.py` (3), `src/utils.py` (2), `src/sequence_analyzer.py` (2), `src/mcp_presigned.py` (2), `src/mcp_confirm.py` (1)
- 影響測試：None (logging changes are non-functional; no test assertions on log format)
- 技術風險：
  - Mechanical but high-volume change — risk of typos or missed calls
  - Some logger calls interpolate exception objects — ensure `str(e)` or use `exc_info=True` correctly
  - `scheduler_service.py` uses `%s` format with positional args — convert carefully

## Constitution Check
- 安全影響：無
- 成本影響：無（CloudWatch log volume unchanged）
- 架構影響：低。Log schema change (additive) improves queryability

## Implementation Phases

### Phase 1: P0 files — app.py + mcp_execute.py (41 calls total)
- Convert all `[TAG]` and plain f-string loggers to structured format
- Establish `module` naming conventions:
  - app.py cleanup handlers → `"module": "cleanup"`
  - app.py trust-expiry handlers → `"module": "trust_expiry"`
  - mcp_execute.py → `"module": "execute"`

### Phase 2: P1 files — notifications.py + telegram.py + callbacks.py (28 calls)
- `telegram.py [TIMING]` → `extra={"module": "telegram", "operation": "api_call", "method": method, "elapsed_ms": elapsed}`
- callbacks.py remaining unstructured → add `extra=` with appropriate module/operation

### Phase 3: P2 files — mcp_deploy_frontend.py + mcp_upload.py + deployer.py (19 calls)

### Phase 4: P3+P4 files — remaining 20 calls
- risk_scorer.py [RiskScorer] → `"module": "risk_scorer"`
- scheduler_service.py [SCHEDULER] → `"module": "scheduler"`
- mcp_history.py [history] → `"module": "history"`
- paging.py → `"module": "paging"`

### Phase 5: Verification
```bash
grep -rn "logger\." src/ --include="*.py" | grep -v "extra=" | grep -v "#" | wc -l
# Target: < 10
```

## Note on Scope
This task is a mechanical cleanup. Sub-agent should process files in batches (one file per tool call when possible), verify count decreases, and confirm no functional code is changed.
