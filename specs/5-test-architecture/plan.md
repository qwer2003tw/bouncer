# Sprint 5: Test Architecture Improvement — Technical Plan

## Key Technical Decisions

### Decision 1: conftest.py Strategy → **Option A (Shared conftest.py)**

**Chosen:** Keep `app_module` in `tests/conftest.py`. All 14 new files import it implicitly via pytest fixture discovery.

**Rationale:**
- `app_module` is referenced 831 times across test_bouncer.py
- Rewriting each file to self-contain fixtures would be a massive, error-prone change
- conftest.py is pytest's standard mechanism for shared fixtures
- Risk: LOW — fixtures work identically when in conftest.py vs inline

**Implementation:**
1. Extract lines 1-199 from `test_bouncer.py` (imports, `mock_dynamodb`, `app_module`, `_cleanup_tables`, `_ALL_TABLE_KEYS`) into `tests/conftest.py`
2. Each target file gets only:
   ```python
   import json, time, pytest
   from unittest.mock import patch, MagicMock
   # No need to import app_module — conftest.py provides it
   ```
3. Tests reference `app_module` exactly as before (parameter injection)

**Fixture Scope Decision:**
- **Keep `scope="module"`** for `mock_dynamodb` and `app_module`
  - Changing to `function` scope would slow tests by 10-50× (DynamoDB table creation per test)
  - The `_cleanup_tables` autouse fixture already handles data isolation between tests
  - After splitting, "module" scope means per-file, not per-monolith — this is the correct behavior

### Decision 2: Deletion Strategy → **Delete After Full Verification**

**Process:**
1. Create all 14 files + conftest.py
2. Run `pytest tests/ -q` — all tests pass
3. Verify test count matches original
4. Delete `test_bouncer.py`
5. Run `pytest tests/ -q` again — confirm no regressions

**Why not gradual deletion:**
- Having duplicate test classes would cause pytest collection errors
- Clean cut is safer with proper verification

### Decision 3: Coverage Targets (Post-Split)

| Module | Current | Target | Gap Focus |
|---|---|---|---|
| notifications.py | 59% | 80%+ | All 15 send_* functions |
| mcp_execute.py | 72% | 80%+ | L882-1020 (grant tools) |
| callbacks.py | 79% | 85%+ | Grant callbacks, auto-execute |
| mcp_upload.py | — | maintain | Rate limit, trust checks |
| deployer.py | — | maintain | Lock concurrency, SFN |

---

## conftest.py Design

```python
# tests/conftest.py
"""
Shared fixtures for Bouncer test suite.
Extracted from the monolithic test_bouncer.py.
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3


@pytest.fixture(scope="module")
def mock_dynamodb():
    """Mock DynamoDB + S3 — module scope, created once per file."""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        # ... (exact copy of current mock_dynamodb fixture)
        yield dynamodb


@pytest.fixture(scope="module")
def app_module(mock_dynamodb):
    """Load app module with mock injection — module scope."""
    # ... (exact copy of current app_module fixture)
    yield app


_ALL_TABLE_KEYS = {
    'clawdbot-approval-requests': ['request_id'],
    'bouncer-projects': ['project_id'],
    'bouncer-deploy-history': ['deploy_id'],
    'bouncer-deploy-locks': ['project_id'],
    'bouncer-accounts': ['account_id'],
}


@pytest.fixture(autouse=True)
def _cleanup_tables(mock_dynamodb):
    """Clean all tables after each test."""
    # ... (exact copy of current _cleanup_tables fixture)
    yield
    # ... cleanup logic
```

### Important: conftest.py Must Be Exact Copy

The fixtures in conftest.py must be **byte-for-byte identical** to the originals in test_bouncer.py (lines 23-199). Any modification risks breaking the 400 existing tests.

---

## 14 Target Files — Module Mapping

### Batch A (Core MCP + App — highest test count)

| File | Classes | Tests | Source Module |
|---|---|---|---|
| `test_commands.py` (new) | 16 | ~87 | commands.py, help_command.py |
| `test_mcp_execute.py` (new) | 25 | ~49 | mcp_execute.py, mcp_tools.py |
| `test_deployer_main.py` | 16 | ~53 | deployer.py |
| `test_app.py` | 19 | ~45 | app.py (REST, Lambda, HMAC) |

### Batch B (Supporting modules)

| File | Classes | Tests | Source Module |
|---|---|---|---|
| `test_trust.py` (new) | 10 | ~27 | trust.py |
| `test_telegram_main.py` | 10 | ~25 | telegram.py, telegram_commands.py |
| `test_callbacks_main.py` | 8 | ~30 | callbacks.py |
| `test_notifications_main.py` | 2 | ~19 | notifications.py |

### Batch C (Smaller modules)

| File | Classes | Tests | Source Module |
|---|---|---|---|
| `test_mcp_upload_main.py` | 5 | ~16 | mcp_upload.py |
| `test_accounts_main.py` | 4 | ~13 | accounts.py |
| `test_paging.py` (new) | 6 | ~14 | paging.py |
| `test_rate_limit.py` (new) | 5 | ~9 | rate_limit.py |

### Batch D (Tiny files + verification)

| File | Classes | Tests | Source Module |
|---|---|---|---|
| `test_constants.py` (new) | 1 | ~3 | constants.py |
| `test_utils.py` (new) | 3 | ~10 | utils.py |

---

## Parallel Strategy

### Split (T2-T5): Multi-Agent Parallelizable

Each batch is independent once conftest.py (T1) exists. Each agent:
1. Receives: list of class names + line ranges to extract
2. Creates: target file with proper imports
3. Validates: `pytest tests/<target_file>.py -q` passes
4. Does NOT delete from test_bouncer.py (deferred to T5)

**Agent assignment (3 agents):**
- Agent A: Batch A (test_commands.py + test_mcp_execute.py + test_deployer_main.py + test_app.py)
- Agent B: Batch B (test_trust.py + test_telegram_main.py + test_callbacks_main.py + test_notifications_main.py)
- Agent C: Batch C + D (test_mcp_upload_main.py + test_accounts_main.py + test_paging.py + test_rate_limit.py + test_constants.py + test_utils.py)

### Coverage Fill (T6-T8): Fully Parallel

T6, T7, T8 are independent — different source modules, different test files.

### Flaky Fix (T9): Sequential After T1-T5

Must wait for split to complete so `freezegun` is applied to the new file structure, not the monolith.

---

## freezegun Integration Strategy

### Installation
```bash
# Add to test requirements
echo "freezegun>=1.2.0" >> tests/requirements.txt
# Or if using requirements-dev.txt
echo "freezegun>=1.2.0" >> requirements-dev.txt
```

### Usage Pattern

**Class-level freeze (recommended for most cases):**
```python
from freezegun import freeze_time

FROZEN_TIME = "2025-01-15T12:00:00Z"
FROZEN_EPOCH = 1736942400  # int(datetime(2025,1,15,12,0,0,tzinfo=UTC).timestamp())

@freeze_time(FROZEN_TIME)
class TestTrustSession:
    def test_trust_active(self, app_module):
        app_module.table.put_item(Item={
            'request_id': 'test-trust',
            'ttl': FROZEN_EPOCH + 300,
            'expires_at': FROZEN_EPOCH + 600,
        })
        # Assertions use FROZEN_EPOCH, not live time.time()
```

**Method-level freeze (for specific flaky tests):**
```python
class TestRateLimitMore:
    @freeze_time("2025-01-15T12:00:00Z")
    def test_rate_limit_window(self, app_module):
        source = 'repeat-source-fixed'  # No time.time() in ID
        # ...
```

### Compatibility Notes
- `freezegun` + `moto` + `mock_aws`: Compatible. `moto` uses its own internal clock for DynamoDB TTL; `freezegun` only affects Python-level `time.time()` and `datetime.now()`.
- `module`-scope fixtures: **Do NOT freeze at fixture level.** Freeze at class/method level only. The module-scope fixtures should use real time for table creation (which is fine — it's test data setup that needs freezing).
- `_cleanup_tables` fixture: Unaffected by freezegun (scans don't depend on time).

---

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| Fixture not found after split | conftest.py is exact copy; pytest auto-discovers |
| Import path issues | Each file adds `sys.path.insert(0, ...)` if needed; verify with individual run |
| Test count mismatch | Automated count verification before/after |
| OOM during split verification | Run files individually, not as full suite |
| freezegun breaks moto | Test compatibility first with 1 class before mass-applying |
| Existing test files conflict | Use `_main` suffix for new files that would collide |
