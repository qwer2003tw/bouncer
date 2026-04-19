# Feature Specification: mcp_execute.py 拆分 Phase 1

Feature Branch: feat/sprint60-002-mcp-execute-split
Sprint: 60
Task ID: bouncer-s60-002

## Problem Statement

`src/mcp_execute.py` 長達 1507 行，混合了多種職責：
1. **命令執行核心**（parse、risk check、approval flow）
2. **Grant session 管理**（request_grant、grant_status、revoke_grant、grant_execute）
3. **Chain risk analysis**（_check_chain_risks、_split_chain）

Code review 困難，模組邊界不清晰。Phase 1 目標是將 grant tools 相關 functions extract 到獨立模組。

### 現行結構分析

| Function | Line | 職責 | Extract to |
|----------|------|------|-----------|
| `_check_grant_session` | 492-615 | Grant layer 在 execute pipeline 中 | 保留（pipeline 一部分）|
| `mcp_tool_request_grant` | 1110-1201 | MCP tool entry point | `mcp_grant.py` |
| `mcp_tool_grant_status` | 1203-1233 | MCP tool entry point | `mcp_grant.py` |
| `mcp_tool_revoke_grant` | 1235-1261 | MCP tool entry point | `mcp_grant.py` |
| `mcp_tool_grant_execute` | 1263-1507 | MCP tool entry point | `mcp_grant.py` |
| `_check_chain_risks` | 945-1049 | Chain risk analysis | `chain_analyzer.py` |

---

## User Scenarios & Testing

### User Story 1：Extract grant MCP tools 到 `mcp_grant.py`

> 作為開發者，我需要 grant 相關的 MCP tool functions 在獨立模組中，以便 code review 時只看 grant 邏輯。

**Given** 現行 `mcp_execute.py` 包含 4 個 grant MCP tool functions（lines 1110-1507）
**When** 將它們移至 `src/mcp_grant.py`
**Then** `mcp_grant.py` 包含 `mcp_tool_request_grant`, `mcp_tool_grant_status`, `mcp_tool_revoke_grant`, `mcp_tool_grant_execute`
**And** 所有 import dependencies 正確解析
**And** `mcp_execute.py` 減少約 400 行
**And** MCP tool routing（`app.py` 中的 tool dispatch）更新指向新模組

### User Story 2：Extract chain risk analysis 到 `chain_analyzer.py`

> 作為開發者，chain risk analysis 邏輯與 execute 主流程分離，便於獨立測試和修改。

**Given** `_check_chain_risks`（lines 945-1049）和 `_split_chain` 是獨立的分析邏輯
**When** 將它們移至 `src/chain_analyzer.py`
**Then** `chain_analyzer.py` 包含 `check_chain_risks` 和 `split_chain`（去除 `_` prefix 成為 public API）
**And** `mcp_execute.py` 中的 `mcp_tool_execute` 改為 `from chain_analyzer import check_chain_risks`
**And** 原有行為完全不變

---

## Requirements

### FR-001：建立 `src/mcp_grant.py`
- 移動 4 個 grant MCP tool functions
- 保留所有 import 依賴（`grant`, `mcp_result`, `mcp_error`, `emit_metric` 等）
- 不改任何邏輯，只移動

### FR-002：建立 `src/chain_analyzer.py`
- 移動 `_check_chain_risks` → `check_chain_risks`（public function）
- 移動 `_split_chain` → `split_chain`
- 保留所有 import 依賴
- `mcp_execute.py` 中的呼叫改為 `from chain_analyzer import check_chain_risks`

### FR-003：更新 MCP tool routing
- `src/app.py` 中的 tool dispatch 表需更新 grant tools 的 import 路徑
- 確認所有 MCP tool name → function mapping 正確

### FR-004：`_check_grant_session` 保留在 `mcp_execute.py`
- 這是 execute pipeline 的一個 layer，不移動
- 它 import `from grant import ...` 的部分不受影響

### FR-005：API 行為完全不變
- 不改任何邏輯、不改任何 response format
- 不改任何 error handling 策略
- 只移動 code

### FR-006：Mock path 更新計畫
- 所有 `patch('src.mcp_execute.mcp_tool_request_grant')` → `patch('src.mcp_grant.mcp_tool_request_grant')`
- 所有 `patch('src.mcp_execute.mcp_tool_grant_status')` → `patch('src.mcp_grant.mcp_tool_grant_status')`
- 類似更新 chain_analyzer 的 mock paths
- 需搜索所有 test files 並更新

---

## Interface Contract

### `src/mcp_grant.py` 公開 API

```python
# 從 mcp_execute.py 移出，簽名完全不變
def mcp_tool_request_grant(req_id: str, arguments: dict) -> dict: ...
def mcp_tool_grant_status(req_id: str, arguments: dict) -> dict: ...
def mcp_tool_revoke_grant(req_id: str, arguments: dict) -> dict: ...
def mcp_tool_grant_execute(req_id: str, arguments: dict) -> dict: ...
```

### `src/chain_analyzer.py` 公開 API

```python
def check_chain_risks(ctx: 'ExecuteContext') -> Optional[dict]: ...
def split_chain(command: str) -> List[str]: ...
```

### `src/mcp_execute.py` 中的 import 變更

```python
# 新增
from chain_analyzer import check_chain_risks

# mcp_tool_execute 中原本呼叫 _check_chain_risks(ctx) 改為 check_chain_risks(ctx)
```

### `src/app.py` tool dispatch 變更

```python
# 原：from mcp_execute import mcp_tool_request_grant, ...
# 改：from mcp_grant import mcp_tool_request_grant, ...
```

---

## TCS 計算

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| D1 Files | 3/5 | 4 files 改動：`mcp_execute.py`（刪除）、`mcp_grant.py`（新建）、`chain_analyzer.py`（新建）、`app.py`（routing） |
| D2 Cross-module | 2/4 | 新模組需從多處 import，app.py routing 更新 |
| D3 Testing | 3/4 | 大量 mock path 更新（所有 grant test files）、chain risk tests 更新 |
| D4 Infrastructure | 0/4 | 無 template.yaml 改動 |
| D5 External | 0/4 | 無外部整合 |

**Total TCS: 8 (Simple)**
→ Sub-agent strategy: 1 agent timeout 600s

⚠️ **風險評估**：TCS 分數低但 mock path 更新範圍大。需仔細搜索所有 test files 中對 `mcp_execute` 模組的 patch 路徑。建議 agent 先用 `grep -rn "patch.*mcp_execute.*grant\|patch.*mcp_execute.*chain"` 確認全部需更新的位置。

---

## Cost Analysis

- 無額外 infra 或 runtime 成本
- 純 code 重構，不影響 Lambda 執行效能

---

## Success Criteria

- SC-001：`mcp_execute.py` 行數從 1507 減少至 ~1000（刪除 ~500 行 grant + chain code）
- SC-002：`mcp_grant.py` 包含 4 個 grant MCP tool functions
- SC-003：`chain_analyzer.py` 包含 chain risk analysis functions
- SC-004：所有既有 tests 通過（0 failures）
- SC-005：所有 mock paths 更新完畢（grep 確認無殘留 `patch('src.mcp_execute.mcp_tool_grant`）
- SC-006：MCP tool routing 正確（grant tools 可正常呼叫）
- SC-007：`ruff check` 無新增 lint errors
