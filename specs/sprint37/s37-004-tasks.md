# s37-004 Tasks — fix: auto_approved 通知補帳號欄位

TCS: 3 (Simple) → 1 sub-agent, timeout 600s

## Tasks

[T001] [P] [Story 1] 在 `mcp_execute.py` `_check_auto_approve()` 的 `_notif_text` 加入帳號行  | TCS=3 (Simple)
- 找到 `_notif_text` 建構位置
- 加入 `account_line`（含 `if ctx.account_id` guard）
- 插入 format string（來源行後、reason 行前）

[T002] [P] [Story 1] 補 regression tests  | TCS=3 (Simple)
- `test_auto_approved_notification_includes_account` — 有帳號 ID
- `test_auto_approved_notification_no_account_id` — 空 account_id 不顯示帳號行
- `test_auto_approved_notification_account_name_escaping` — 特殊字元 escape

## Verification

```bash
cd /tmp/bouncer-s37-004
ruff check src/mcp_execute.py
python3 -m pytest tests/test_mcp_execute.py -v -k "auto_approved" --tb=short
```
