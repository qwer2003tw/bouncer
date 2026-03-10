# Sprint 17-004: 完整 Audit Trail — source_ip / user_agent / approved_by / duration_ms

> GitHub Issue: #74
> Priority: P1
> TCS: 8
> Generated: 2026-03-08

---

## Problem Statement

Bouncer 是安全審批系統，但 audit log 缺乏客觀可驗證的欄位。現有記錄的 `source`/`reason` 全部是 caller 自填，可以任意偽造，不符合嚴格審計要求。

### 缺少的欄位

| 欄位 | 優先 | 現況 | 目標 |
|------|------|------|------|
| `source_ip` | 🔴 高 | 不記錄 | API Gateway `requestContext.identity.sourceIp` |
| `user_agent` | 🟡 中 | 不記錄 | request header `User-Agent` |
| `approved_by` | 🔴 高 | 部分記錄（`approver` 欄位已存在） | 統一格式，確保所有路徑都記錄 |
| `approved_at` | 🔴 高 | 已有（callbacks.py line ~154） | ✅ 已實作，驗證覆蓋率 |
| `exit_code` | 🟡 中 | 部分記錄（auto_approved 路徑有） | 統一所有路徑 |
| `duration_ms` | 🟢 低 | 不記錄 | 執行前後 time.time() 差值 |

## Root Cause

各功能（execute、upload、deploy、grant）各自開發，audit logging 沒有統一 schema。`log_decision()` 已記錄基本欄位，但呼叫端沒有統一傳入 source_ip/user_agent 等 request context。

## Scope

### 變更 1: 從 API Gateway event 提取 request context

**檔案：** `src/app.py`（`lambda_handler` 入口）

在 MCP/REST 路由前，統一提取 request context：

```python
def _extract_request_context(event: dict) -> dict:
    """從 API Gateway event 提取客觀 request metadata。"""
    request_ctx = event.get('requestContext', {})
    headers = event.get('headers', {})
    
    # REST API (v1) vs HTTP API (v2) 格式不同
    source_ip = (
        request_ctx.get('http', {}).get('sourceIp') or
        request_ctx.get('identity', {}).get('sourceIp') or
        headers.get('x-forwarded-for', '').split(',')[0].strip() or
        ''
    )
    user_agent = headers.get('user-agent', '')
    
    return {
        'source_ip': source_ip[:45],       # IPv6 max 45 chars
        'user_agent': user_agent[:200],     # truncate
    }
```

將 `request_context` dict 傳入各 MCP tool handler。

### 變更 2: log_decision() 接受 request context

**檔案：** `src/utils.py`

`log_decision()` 已使用 `**kwargs`，所以 `source_ip` / `user_agent` 會自動存入 DDB item。不需要改 signature，只需確認呼叫端傳入。

驗證：`item.update({k: v for k, v in kwargs.items() if v is not None})` — ✅ 已支援。

### 變更 3: 各 MCP tool handler 傳入 request context

**檔案：** 需修改的呼叫點

| 檔案 | 函數 | 呼叫 log_decision 的位置 |
|------|------|--------------------------|
| `src/mcp_execute.py` | `mcp_tool_execute()` | line ~412, ~451, ~549, ~630, ~739, ~932 |
| `src/mcp_upload.py` | upload 相關 | 內部 log_decision 呼叫 |
| `src/deployer.py` | deploy 相關 | 內部 log_decision 呼叫 |

每個 `log_decision()` 呼叫加上 `source_ip=ctx.source_ip, user_agent=ctx.user_agent`。

**方案：** 擴充各 Context dataclass，加入 `source_ip` / `user_agent` 欄位。

`mcp_execute.py` 的 `ExecuteContext`：
```python
@dataclass
class ExecuteContext:
    # ... existing fields ...
    source_ip: str = ''
    user_agent: str = ''
```

### 變更 4: 統一 exit_code 記錄

**檔案：** `src/mcp_execute.py`

確認所有 `log_decision()` 呼叫路徑（auto_approved、trust_auto_approved、approved poll）都傳入 `exit_code`。

現況檢查：
- `auto_approved` 路徑：`record_execution_error()` 已記錄 exit_code ✅
- `trust_auto_approved` 路徑：需確認
- 手動 approved（poll callback）路徑：需確認

### 變更 5: 新增 duration_ms 計算

**檔案：** `src/mcp_execute.py`

在 `execute_command()` 前後計時：

```python
t0 = time.time()
result = execute_command(command, account_config)
duration_ms = int((time.time() - t0) * 1000)

log_decision(..., duration_ms=duration_ms)
```

### 變更 6: approved_by 格式統一

**檔案：** `src/callbacks.py`

現況：`approver` 欄位存 `str(query.from_user.id)`。

增強：新增 `approver_username` 欄位（如果有），格式 `{user_id}:{username}`。

```python
# line ~154 area
update_expr += ', approver_username = :aun'
values[':aun'] = getattr(query.from_user, 'username', '') or ''
```

## 設計決策

| 決策 | 選項 | 選擇 | 理由 |
|------|------|------|------|
| source_ip 傳遞方式 | 全域變數 vs Context 屬性 vs 函數參數 | Context 屬性 | 與既有架構一致，不引入 implicit state |
| user_agent 截斷長度 | 100 vs 200 vs 500 | 200 | DDB item 大小限制 400KB，200 足夠辨識 |
| duration_ms 精度 | ms vs seconds | ms | 更精確，方便統計分析 |
| 向後兼容 | 新舊欄位並存 vs 遷移 | 新舊並存 | DDB schemaless，舊 record 不受影響 |

## Out of Scope

- DynamoDB GSI 建立（按 source_ip 查詢）— 需要時再加
- API Gateway access log（#76，另一 task）
- Dashboard / 報表
- 現有 record 的 backfill

## Test Plan

### Unit Tests（新增 / 修改）

| # | 測試 | 驗證 |
|---|------|------|
| T1 | `test_extract_request_context_rest_api_v1` | REST API format → 正確提取 source_ip |
| T2 | `test_extract_request_context_http_api_v2` | HTTP API format → 正確提取 source_ip |
| T3 | `test_extract_request_context_x_forwarded_for` | 多 IP x-forwarded-for → 取第一個 |
| T4 | `test_extract_request_context_missing` | 空 event → 空字串，不 crash |
| T5 | `test_log_decision_with_source_ip` | log_decision 帶 source_ip → DDB item 含此欄位 |
| T6 | `test_log_decision_with_duration_ms` | log_decision 帶 duration_ms → DDB item 含此欄位 |
| T7 | `test_execute_records_duration_ms` | execute 成功 → record 含 duration_ms |
| T8 | `test_execute_records_source_ip` | execute request → record 含 source_ip |
| T9 | `test_approved_by_includes_username` | callback approve → record 含 approver_username |
| T10 | `test_exit_code_all_paths` | auto/trust/manual 三種路徑都記錄 exit_code |

### 回歸測試

- 所有既有 execute/upload/deploy tests 通過
- DDB mock 驗證新欄位不影響既有讀取

## Acceptance Criteria

- [ ] `source_ip` 記錄到所有 request 的 DDB record
- [ ] `user_agent` 記錄到所有 request 的 DDB record
- [ ] `duration_ms` 記錄到所有 execute 的 DDB record
- [ ] `approver_username` 記錄到 approved request 的 DDB record
- [ ] `exit_code` 在 auto/trust/manual 三種路徑都有記錄
- [ ] 新增 10 個測試，全部通過
- [ ] 所有既有測試通過
- [ ] 向後兼容：舊 record 不受影響，新舊欄位共存
