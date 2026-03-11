# sprint30-003: Implementation Plan

## Overview

新增 `bouncer_grant_execute` MCP tool，涉及 3 個檔案修改 + 1 個新測試檔案。

核心設計：**不複用 `_check_grant_session`（它是 bouncer_execute pipeline 的 fallthrough layer），而是寫一個獨立的 `mcp_tool_grant_execute` function**，直接呼叫相同的底層函數（`get_grant_session`, `is_command_in_grant`, `try_use_grant_command`, `execute_command`），但改為 **fail-fast 模式**（明確錯誤而非 fallthrough）。

## Architecture Decision

### 為什麼不複用 `_check_grant_session`？

`_check_grant_session` 是 `bouncer_execute` pipeline 的一個 layer：
- 設計為 **fallthrough**（return None = 繼續下一層）
- 所有檢查失敗都靜默 return None
- 依賴 `ExecuteContext` dataclass（含 `trust_scope`, `smart_decision` 等 grant_execute 不需要的欄位）

`bouncer_grant_execute` 需要：
- **Fail-fast**：每個失敗都回傳明確錯誤
- 輕量 input（不需 `trust_scope`, `sync`, `context`）
- 明確的 error taxonomy（`grant_expired`, `command_not_in_grant` 等）

### 複用的部分

| 函數 | 來源 | 用途 |
|------|------|------|
| `get_grant_session(grant_id)` | grant.py | 取得 grant 資料 |
| `normalize_command(cmd)` | grant.py | 命令正規化 |
| `is_command_in_grant(cmd, grant)` | grant.py | 檢查命令在授權清單 |
| `try_use_grant_command(id, cmd, repeat)` | grant.py | 原子性標記使用 |
| `execute_command(cmd, role)` | commands.py | 實際執行 AWS CLI |
| `check_compliance(cmd)` | compliance_checker.py | 合規檢查 |
| `store_paged_output(id, result)` | mcp_execute.py | 分頁輸出 |
| `send_grant_execute_notification(...)` | notifications.py | Telegram 通知 |
| `log_decision(...)` | mcp_execute.py | DynamoDB audit log |

## Implementation Steps

### Step 1: tool_schema.py — 新增 schema

在 `MCP_TOOLS` dict 中 `bouncer_revoke_grant` 之後加入：

```python
'bouncer_grant_execute': {
    'description': '在已核准的 Grant Session 內執行命令。命令必須在 grant 授權清單中。不在清單的命令會被拒絕（不會 fallthrough 到一般審批流程）。',
    'parameters': {
        'type': 'object',
        'properties': {
            'grant_id': {
                'type': 'string',
                'description': 'Grant Session ID'
            },
            'command': {
                'type': 'string',
                'description': 'AWS CLI 命令（必須精確匹配 grant 已授權的命令之一）'
            },
            'source': {
                'type': 'string',
                'description': '請求來源標識（必須與 grant 建立時的 source 一致）'
            },
            'account': {
                'type': 'string',
                'description': '目標 AWS 帳號 ID（不填則使用預設帳號，必須與 grant 一致）'
            },
            'reason': {
                'type': 'string',
                'description': '執行原因（用於 audit log）',
                'default': 'Grant execute'
            }
        },
        'required': ['grant_id', 'command', 'source']
    }
},
```

**位置：** `bouncer_revoke_grant` entry 之後（~line 352）

### Step 2: mcp_execute.py — 新增 `mcp_tool_grant_execute`

在 `mcp_tool_revoke_grant` 函數之後（~line 1194）新增：

```python
def mcp_tool_grant_execute(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_grant_execute — 在 Grant Session 內執行命令（fail-fast 模式）"""
```

**函數結構（pseudo-code）：**

```
1. 參數解析 + 必填驗證（grant_id, command, source）
2. command = _normalize_command(command)  # SEC-003 unicode normalize
3. 帳號解析（init_default_account, validate_account_id, get_account）
4. grant = get_grant_session(grant_id)
   → 不存在: return grant_not_found (不區分 source mismatch 以免洩漏)
5. source 匹配 → grant['source'] != source: return grant_not_found
6. status 檢查 → != 'active': return grant_not_active
7. TTL 檢查 → time.time() > expires_at: return grant_expired
8. account 匹配 → grant['account_id'] != resolved_account_id: return account_mismatch
9. compliance_checker → check_compliance(command)
   → 不通過: return compliance_violation
10. is_command_in_grant(normalized_cmd, grant) → False: return command_not_in_grant
11. total_executions 檢查 → >= max: return total_executions_exceeded
12. try_use_grant_command(grant_id, normalized_cmd, allow_repeat)
    → False: return command_already_used 或 command_repeat_limit
13. execute_command(command, assume_role)
14. store_paged_output(request_id, result)
15. 計算 remaining info + commands_remaining
16. send_grant_execute_notification(...)
17. log_decision(..., decision_type='grant_approved')
18. record_execution_error (if failed)
19. 回傳成功 response
```

**關鍵設計：**
- Step 4-5 合併為同一個 error status（`grant_not_found`），不洩漏 grant 是否存在
- Step 12 需要區分「已使用」和「危險命令上限」— 目前 `try_use_grant_command` 只回傳 bool，無法區分。**方案：** 在呼叫前先檢查 `used_commands` 和 `is_dangerous` 來判斷具體原因，`try_use_grant_command` 仍用於原子性確認

### Step 3: app.py — 加入 TOOL_HANDLERS

```python
# Line 41: import
from mcp_execute import (
    mcp_tool_execute, mcp_tool_request_grant, mcp_tool_grant_status,
    mcp_tool_revoke_grant, mcp_tool_grant_execute,  # ← 新增
)

# Line ~539: TOOL_HANDLERS dict
'bouncer_grant_execute': mcp_tool_grant_execute,  # ← 新增
```

**位置：** `bouncer_revoke_grant` entry 之後

### Step 4: tests — 新增測試

新增 `tests/test_grant_execute_tool.py`：

測試案例（對應 spec 的 Acceptance Scenarios）：

| Test | Scenario | 驗證重點 |
|------|----------|---------|
| `test_happy_path` | S1 | 成功執行，response 結構正確 |
| `test_allow_repeat` | S2 | 重複執行成功 |
| `test_paged_output` | S3 | 大輸出分頁 |
| `test_grant_not_found` | S4 | grant 不存在 |
| `test_source_mismatch` | S5 | source 不匹配 → grant_not_found |
| `test_grant_expired` | S6 | TTL 過期 |
| `test_grant_not_active` | S7 | status ≠ active |
| `test_command_not_in_grant` | S8 | 命令不在清單 |
| `test_command_already_used` | S9 | 已使用（no repeat） |
| `test_dangerous_repeat_limit` | S10 | SEC-009 上限 |
| `test_total_executions_exceeded` | S11 | 總次數超限 |
| `test_compliance_violation` | S12 | compliance 攔截 |
| `test_account_mismatch` | S13 | account 不一致 |
| `test_account_invalid` | S14 | account 不存在 |
| `test_missing_params` | S15 | 缺少必填參數 |
| `test_command_failed` | S17 | 命令執行失敗但仍記錄 |

**測試策略：** Mock `get_grant_session`, `try_use_grant_command`, `execute_command`, `check_compliance` 等。
沿用 `tests/test_grant.py` 的 fixture 模式。

## File Change Summary

| File | Action | Lines (估) | 說明 |
|------|--------|-----------|------|
| `src/tool_schema.py` | 修改 | +25 | 新增 `bouncer_grant_execute` schema |
| `src/mcp_execute.py` | 修改 | +120~150 | 新增 `mcp_tool_grant_execute` function |
| `src/app.py` | 修改 | +2 | import + TOOL_HANDLERS entry |
| `tests/test_grant_execute_tool.py` | 新增 | +300~350 | 16+ 測試案例 |
| **Total** | | **~500** | |

## Risk Assessment

| 風險 | 等級 | 緩解 |
|------|------|------|
| `try_use_grant_command` 無法區分失敗原因 | Low | 在呼叫前先檢查狀態（pre-check + atomic confirm） |
| 與 `bouncer_execute` + `grant_id` 路徑重複 | Low | 兩者並存，`bouncer_execute` 的 `_check_grant_session` 不動 |
| compliance_checker 誤攔 grant 命令 | Low | By design — compliance 永遠最高優先 |
| Cold start 增加（新增 import） | Negligible | grant.py 已在 lambda 內，不增加新 dependency |
