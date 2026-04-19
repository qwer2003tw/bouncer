# Tasks: mcp_execute.py 拆分 Phase 1

Sprint: 60 | Task: bouncer-s60-002

## Phase 1: Setup

```bash
cd /home/ec2-user/projects/bouncer
git worktree add /tmp/s60-002-mcp-execute-split feat/sprint60-002-mcp-execute-split -b feat/sprint60-002-mcp-execute-split
cd /tmp/s60-002-mcp-execute-split
```

## Phase 2: Analysis（必做）

### Task 2.1：盤點所有需更新的 mock paths

```bash
# Grant tool mocks
grep -rn "patch.*mcp_execute.*grant\|mock.*mcp_execute.*grant" tests/ --include="*.py" | head -50

# Chain risk mocks
grep -rn "patch.*mcp_execute.*chain\|mock.*mcp_execute.*chain" tests/ --include="*.py" | head -50

# App.py tool dispatch
grep -n "mcp_tool_request_grant\|mcp_tool_grant_status\|mcp_tool_revoke_grant\|mcp_tool_grant_execute" src/app.py | head -20

# _check_chain_risks 呼叫點
grep -n "_check_chain_risks\|_split_chain" src/mcp_execute.py | head -10
```

### Task 2.2：確認 grant functions 的 import 依賴

```bash
# 確認每個 grant function 用到哪些 imports
sed -n '1110,1510p' src/mcp_execute.py | grep -E "^from |^import " | sort -u
# 加上 function 內部的 lazy imports
grep -n "from.*import\|import " src/mcp_execute.py | sed -n '1110,1510p'
```

## Phase 3: Implementation

### Task 3.1：建立 `src/mcp_grant.py`

1. 建立新檔案，加入模組 docstring
2. 從 `mcp_execute.py` 複製以下 functions（保持原樣不改邏輯）：
   - `mcp_tool_request_grant`（line 1110-1201）
   - `mcp_tool_grant_status`（line 1203-1233）
   - `mcp_tool_revoke_grant`（line 1235-1261）
   - `mcp_tool_grant_execute`（line 1263-1507）
3. 加入所有必要 imports（從 mcp_execute.py 頂部複製相關 import）
4. 確認 `mcp_result`, `mcp_error`, `emit_metric`, `execute_command`, `generate_request_id`, `store_paged_output`, `record_execution_error`, `log_decision`, `table`, `DEFAULT_ACCOUNT_ID`, `init_default_account`, `get_account`, `validate_account_id`, `_normalize_command` 等都正確 import

### Task 3.2：建立 `src/chain_analyzer.py`

1. 建立新檔案
2. 從 `mcp_execute.py` 移出：
   - `_check_chain_risks` → rename to `check_chain_risks`（line 945-1049）
   - `_split_chain` → rename to `split_chain`
3. 加入必要 imports：`emit_metric`, `mcp_result`, `logger`, `ExecuteContext` type hint

### Task 3.3：更新 `src/mcp_execute.py`

1. **刪除** 已移出的 functions（lines 945-1049 chain、lines 1110-1507 grant）
2. **新增** import：`from chain_analyzer import check_chain_risks`
3. **更新** `mcp_tool_execute` 中的呼叫：`_check_chain_risks(ctx)` → `check_chain_risks(ctx)`
4. **刪除** 不再需要的 imports（若只有 grant functions 使用）

### Task 3.4：更新 `src/app.py` tool routing

```bash
# 找到 tool dispatch 的位置
grep -n "mcp_tool_request_grant\|mcp_tool_grant" src/app.py | head -10
```

更新 import 路徑：
```python
# 原：from mcp_execute import mcp_tool_request_grant, mcp_tool_grant_status, mcp_tool_revoke_grant, mcp_tool_grant_execute
# 改：from mcp_grant import mcp_tool_request_grant, mcp_tool_grant_status, mcp_tool_revoke_grant, mcp_tool_grant_execute
```

### Task 3.5：共用 helper functions 處理

- `_normalize_command` 被 `mcp_grant.py` 的 `mcp_tool_grant_execute` 使用
- 選項 A：在 `mcp_grant.py` 中 `from mcp_execute import _normalize_command`（循環 import 風險）
- 選項 B：將 `_normalize_command` 移至 `utils.py` 或 `commands.py`（推薦）
- **決策：** 暫時用選項 A，Phase 2 再重構 utils

## Phase 4: Mock Path Updates

### Task 4.1：更新 grant test files 的 mock paths

```bash
# 列出所有需要更新的檔案和行號
grep -rn "patch.*mcp_execute.*grant\|from.*mcp_execute.*import.*grant" tests/ --include="*.py"
```

對每個找到的檔案，執行：
```bash
sed -i 's/mcp_execute\.mcp_tool_request_grant/mcp_grant.mcp_tool_request_grant/g' tests/{file}
sed -i 's/mcp_execute\.mcp_tool_grant_status/mcp_grant.mcp_tool_grant_status/g' tests/{file}
sed -i 's/mcp_execute\.mcp_tool_revoke_grant/mcp_grant.mcp_tool_revoke_grant/g' tests/{file}
sed -i 's/mcp_execute\.mcp_tool_grant_execute/mcp_grant.mcp_tool_grant_execute/g' tests/{file}
```

### Task 4.2：更新 chain analyzer test mock paths

```bash
grep -rn "patch.*mcp_execute.*chain" tests/ --include="*.py"
# 更新：_check_chain_risks → chain_analyzer.check_chain_risks
```

## Phase 5: Tests

### Task 5.1：跑全部 tests

```bash
cd /tmp/s60-002-mcp-execute-split
python -m pytest tests/ -x --tb=short 2>&1 | tail -30
```

### Task 5.2：特別跑 grant 和 chain 相關 tests

```bash
python -m pytest tests/test_grant.py tests/test_grant_execute_tool.py -v
python -m pytest tests/ -k "chain" -v
```

### Task 5.3：確認無殘留的舊 mock paths

```bash
# 應該回傳 0 結果
grep -rn "patch.*src\.mcp_execute.*mcp_tool_grant\|patch.*src\.mcp_execute.*mcp_tool_request_grant" tests/ --include="*.py"
```

## Phase 6: Lint & Commit

```bash
ruff check src/mcp_grant.py src/chain_analyzer.py src/mcp_execute.py src/app.py
git add src/mcp_grant.py src/chain_analyzer.py src/mcp_execute.py src/app.py tests/
git commit -m "refactor: extract grant tools + chain analyzer from mcp_execute (#s60-002)

Phase 1 of mcp_execute.py split (1507 → ~1000 lines):
- Extract mcp_tool_request_grant/grant_status/revoke_grant/grant_execute → mcp_grant.py
- Extract _check_chain_risks/_split_chain → chain_analyzer.py
- Update app.py tool dispatch routing
- Update all test mock paths
- No logic changes, API behavior identical
"
```

## TCS Summary

TCS=8 → 1 agent timeout 600s

⚠️ **關鍵風險點**：
1. Mock path 更新遺漏 → tests fail（用 grep 驗證）
2. 循環 import（mcp_grant.py ← mcp_execute.py）→ 注意 `_normalize_command` 的 import 方向
3. 共用 globals（`table`, `DEFAULT_ACCOUNT_ID`）→ 確認都從正確的 source import
