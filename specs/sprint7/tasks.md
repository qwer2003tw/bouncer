# Bouncer Sprint 7 — Task List

> Generated: 2026-03-01

---

## P0 — Must Do

```
[S7-001] [P0] [PARALLEL] fix: bouncer_execute && 串接命令靜默失敗
  Files: src/commands.py, src/mcp_execute.py
  Tests: tests/test_commands.py, tests/test_mcp_execute.py
  Estimate: Medium
  Notes: Add _split_chain() to commands.py; update execute_command()
         to iterate sub-commands; update risk checks in mcp_execute.py
         to evaluate each sub-command individually.

[S7-002] [P0] [SEQUENTIAL] fix: 過期請求按鈕未自動移除 (EventBridge Scheduler)
  Files: src/notifications.py, src/app.py, src/telegram.py, template.yaml
  Tests: tests/test_notifications_main.py, tests/test_app.py
  Estimate: High
  Depends: template.yaml changes — run after #003 or coordinate merge
  Notes: Store message_id from Telegram API response; create one-time
         EventBridge Scheduler schedule at request creation; add cleanup
         handler in app.py; add IAM permissions in template.yaml.

[S7-003] [P0] [SEQUENTIAL] fix: mcp_history/telegram_commands 改用 GSI Query
  Files: src/mcp_history.py, src/telegram_commands.py, template.yaml
  Tests: tests/test_history.py, tests/test_telegram_main.py
  Estimate: Medium
  Depends: template.yaml changes — coordinate with #002
  Notes: Convert 10+ scan() calls to query() using existing GSIs
         (status-created-index, source-created-index). May need to
         change source-created-index projection to ALL. Add new GSI
         for type-based queries (trust_session) if needed.
```

## P1 — Should Do

```
[S7-004] [P1] [PARALLEL] fix: CloudWatch Logs 輸出截斷
  Files: src/paging.py, src/constants.py, src/callbacks.py
  Tests: tests/test_paging.py (new/extend)
  Estimate: Medium
  Notes: Review store_paged_output() to ensure ALL content is paginated,
         not just first chunk. Consider increasing OUTPUT_MAX_INLINE.
         Add hard cap for total output size. Ensure send_remaining_pages()
         delivers all pages to Telegram.

[S7-005] [P1] [PARALLEL] feat: sam_deploy.py 自動 import 已存在 CFN resource
  Files: deployer/scripts/sam_deploy.py
  Tests: deployer/tests/test_sam_deploy.py (new)
  Estimate: Medium
  Notes: After failed sam deploy, parse CFN events for "already exists"
         errors. Build and execute IMPORT changeset. Retry deploy.
         Completely isolated in deployer/ subdirectory.

[S7-006] [P1] [PARALLEL] fix: trust scope 加入 source 綁定驗證
  Files: src/trust.py
  Tests: tests/test_trust.py
  Estimate: Medium
  Notes: Add bound_source field to trust sessions. Validate source
         matches in get_trust_session() and should_trust_approve().
         Backward compatible: skip check if bound_source is empty.
         Security-sensitive — need thorough test coverage.

[S7-007] [P1] [PARALLEL] refactor: 消除重複函數
  Files: src/telegram.py, src/telegram_commands.py, src/mcp_presigned.py,
         src/mcp_upload.py, src/utils.py
  Tests: tests/test_utils.py, existing tests (regression)
  Estimate: Low
  Notes: send_telegram_message_to → keep in telegram.py, import in
         telegram_commands.py. _sanitize_filename → move to utils.py
         as sanitize_filename(), import in mcp_presigned.py and
         mcp_upload.py. Verify both copies identical before removing.

[S7-008] [P1] [PARALLEL] refactor: DynamoDB table 初始化統一到 db.py
  Files: src/db.py, src/rate_limit.py, src/accounts.py, src/deployer.py,
         src/mcp_execute.py, src/mcp_history.py, src/paging.py,
         src/sequence_analyzer.py, src/trust.py
  Tests: tests/test_db.py (new), full test suite (regression)
  Estimate: Medium
  Notes: Add _LazyTable instances for all tables in db.py. Update
         reset_tables() to clear all. Replace local lazy init in each
         module with import from db.py. Preserve test injection patterns
         (deployer.py tests set tables directly). Best done after
         other changes stabilize.

[S7-009] [P1] [PARALLEL] ops: Lambda Memory 256MB → 512MB
  Files: template.yaml (line 52)
  Tests: sam validate; post-deploy verification
  Estimate: Low
  Notes: One-line change: MemorySize: 256 → 512. Coordinate with
         other template.yaml changes (#002, #003). Can be included
         in any template.yaml PR.
```

---

## Parallelization Summary

| Group | Tasks | Can Run Together | Shared Files |
|-------|-------|-----------------|-------------|
| A | #007 + #009 | ✅ Yes | template.yaml (009), utils.py (007) — no overlap |
| B | #001 | ✅ Yes | commands.py, mcp_execute.py |
| C | #006 | ✅ Yes | trust.py |
| D | #004 | ✅ Yes | paging.py, constants.py |
| E | #005 | ✅ Yes | deployer/scripts/sam_deploy.py (completely isolated) |
| F | #008 | ⚠️ After others | Touches 9 files — best after Phase 1 |
| G | #003 + #002 | 🔒 Sequential | Both touch template.yaml heavily |

**Max parallelism:** 5 agents in Phase 1 (Groups A–E)

---
