# Spec: Bouncer Security Sprint

**Tasks:** SEC-003/004/006/007/008/009/011/013 + bouncer-already-handled-ui + bouncer-upload-regression-guard
**Priority:** high/medium

---

## 1. SEC-003: Unicode 正規化（`mcp_execute.py`）

**問題：** 命令注入防護基於字串匹配，但 Unicode 空白（`\u2003`、`\u00a0` 等）和不可見字元可能繞過。

**修法：** Pipeline 入口加正規化函數，在任何 check 前先清理：
```python
def _normalize_command(cmd: str) -> str:
    # 替換所有 Unicode 空白為普通空白
    # 移除不可見字元（零寬字元等）
    # 折疊多餘空白
```

**測試：** regression test 各類 Unicode bypass 嘗試

---

## 2. SEC-006: Rate limit fail-close（`rate_limit.py`）

**問題：** DynamoDB 故障時 `check_rate_limit` 拋例外，被 caller 的 `except Exception` 忽略，等於放行。

**修法：** 
```python
try:
    check_rate_limit(source)
except RateLimitExceeded:
    raise  # 正常拒絕
except Exception:
    raise RateLimitExceeded("Rate limit unavailable, failing closed")  # 故障時拒絕
```

---

## 3. SEC-007: Trust Session 命令計數原子性（`trust.py`）

**問題：** `should_trust_approve` 讀計數，`increment_trust_command_count` 寫計數，兩步非原子，並發可超限。

**修法：** 合併為單一 conditional update（參考現有 `increment_trust_upload_count`）：
```python
table.update_item(
    UpdateExpression='SET command_count = command_count + :one',
    ConditionExpression='command_count < :max AND #status = :active',
    ...
)
```
如果 `ConditionalCheckFailedException` → 拒絕。

---

## 4. SEC-008: Compliance 正則強化（`compliance_checker.py`）

**問題：** JSON payload 中的 key 順序不同或格式變化（多空白、換行）可能繞過正則。

**修法：**
- 在 scan 前先 JSON normalize（解析後重新序列化）
- 補充測試不同格式的 payload

---

## 5. SEC-009: Grant allow_repeat 破壞性命令確認（`grant.py`）

**問題：** `allow_repeat=True` 的 grant 可以無限次執行同一命令，包含破壞性命令（delete/terminate）。

**修法：**
- 破壞性命令（is_dangerous）即使在 grant 中也限制最多 3 次執行
- 或：allow_repeat grant 中的危險命令每次都需要人工確認（不走自動執行）

---

## 6. SEC-011: REST API 補 compliance check（`app.py`）

**問題：** REST API（`/execute` endpoint）沒有走 MCP pipeline，缺少 compliance check 和 risk scoring。

**修法：** REST handler 呼叫 `compliance_checker.check_compliance()` 和 `risk_scorer.score()`，不合規直接拒絕。

---

## 7. SEC-013: auto_execute_pending 補 compliance（`callbacks.py`）

**問題：** Trust session 開啟後，`_auto_execute_pending_requests()` 會執行積壓的 pending 命令，但沒有重跑 compliance check。

**修法：** 執行前重跑 compliance check，不合規的 pending 命令直接拒絕（不執行）。

---

## 8. SEC-004: Presigned URL ContentLength + Lifecycle（`mcp_presigned.py` + CFN）

**問題：** Presigned URL 沒有大小限制（可上傳任意大的檔案），staging bucket 沒有自動清理。

**修法：**
- Presigned URL 加 `ContentLengthRange` condition（max 10MB）
- `template.yaml` staging bucket 加 lifecycle rule：7 天後刪除 `pending/` 和過期的檔案

---

## 9. bouncer-already-handled-ui（`app.py`）

**問題：** 重複按審批按鈕時，原本完整的訊息被「✅ 已處理」覆蓋。

**修法：**
```python
# 已處理時只彈 toast，不覆蓋原訊息
answer_callback(callback_id, '⚠️ 此請求已處理過')
return response(200, {'ok': True})
# 移除 update_message(...)
```

---

## 10. bouncer-upload-regression-guard（`mcp_upload.py` + tests）

**問題：** 跨帳號 upload 路徑（`account=dev_id`）缺整合測試，命名混用 `staging_account_id` vs `target_account_id`。

**修法：**
- 補 regression test：`account=992382394211` 的 upload 路徑
- 變數改名：staging 路徑明確用 `DEFAULT_ACCOUNT_ID`，加 comment 說明原因

---

## 影響範圍

| 檔案 | 變更 |
|------|------|
| `src/mcp_execute.py` | SEC-003 正規化 |
| `src/rate_limit.py` | SEC-006 fail-close |
| `src/trust.py` | SEC-007 原子性 |
| `src/compliance_checker.py` | SEC-008 正則強化 |
| `src/grant.py` | SEC-009 破壞性限制 |
| `src/app.py` | SEC-011 REST compliance + SEC-already-handled |
| `src/callbacks.py` | SEC-013 pending compliance |
| `src/mcp_presigned.py` | SEC-004 ContentLength |
| `template.yaml` | SEC-004 lifecycle |
| `src/mcp_upload.py` | upload regression 命名重構 |
| `tests/` | 各項新測試 |

## 測試要求
- 每個修復都有對應 regression test
- 整體 coverage ≥ 75%
- 無新增 ruff 警告
