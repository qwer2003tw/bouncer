# Sprint 13-001: Tasks — entities Phase 2

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | notifications.py、utils.py（build_info_builder）、test 新檔 |
| D2 Cross-module | 2 | utils ↔ notifications ↔ telegram_entities ↔ telegram |
| D3 Testing | 3 | 14 個函數 × entities 驗證 + edge cases |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 1 | Telegram API entities（Phase 1 已驗證） |
| **Total TCS** | **9** | ✅ 不需拆分 |

## Task List

### Infrastructure（Batch 0）

```
[001-T01] [P0] [US-2] notifications.py: _send_message() 加 entities 參數，entities 存在時呼叫 send_message_with_entities
[001-T02] [P0] [US-2] notifications.py: _send_message_silent() 加 entities 參數（同上模式）
[001-T03] [P0] [US-2] utils.py: 新增 build_info_builder(source, context) → MessageBuilder（保留舊 build_info_lines 向後相容）
```

### Batch 1: 簡單函數遷移

```
[001-T04] [P0] [US-1] send_account_approval_request() → MessageBuilder，移除 _escape_markdown
[001-T05] [P0] [US-1] send_grant_execute_notification() → MessageBuilder，result 用 pre entity
[001-T06] [P0] [US-1] send_grant_complete_notification() → MessageBuilder
[001-T07] [P0] [US-1] send_blocked_notification() → MessageBuilder
[001-T08] [P0] [US-1] send_trust_upload_notification() → MessageBuilder
[001-T09] [P1] [US-1] send_presigned_notification() → MessageBuilder
[001-T10] [P1] [US-1] send_presigned_batch_notification() → MessageBuilder
```

### Batch 2: 中等複雜度

```
[001-T11] [P0] [US-1] send_trust_auto_approve_notification() → MessageBuilder（條件分支 source/reason/result）
[001-T12] [P0] [US-1] send_batch_upload_notification() → MessageBuilder + build_info_builder()
```

### Batch 3: 高複雜度（動態列表）

```
[001-T13] [P0] [US-1] send_grant_request_notification() → MessageBuilder（命令分類列表迴圈）
[001-T14] [P0] [US-1] send_trust_session_summary() → MessageBuilder（動態命令歷史 + 截斷 + Unicode 清理）
[001-T15] [P0] [US-1] send_deploy_frontend_notification() → MessageBuilder（per-file 列表 + cache 標註）
```

### 清理

```
[001-T16] [P1] [US-3] 確認 notifications.py 中 _escape_markdown 呼叫數 = 0，移除未使用的 import
```

### 測試

```
[001-T17] [P0] [US-2] 測試: _send_message(entities=[...]) → 呼叫 send_message_with_entities
[001-T18] [P0] [US-2] 測試: _send_message(entities=None) → 呼叫 send_telegram_message（向後相容）
[001-T19] [P1] [US-1] 測試: send_account_approval_request() entities 驗證（add + remove 兩種 action）
[001-T20] [P1] [US-1] 測試: send_grant_request_notification() entities 含 bold/code，命令列表正確
[001-T21] [P1] [US-1] 測試: send_trust_session_summary() — 命令歷史、截斷、空命令 edge case
[001-T22] [P1] [US-1] 測試: send_deploy_frontend_notification() — 檔案列表、> 10 files 截斷
[001-T23] [P1] [US-1] 測試: send_blocked_notification() — text 含命令預覽 + 原因
[001-T24] [P1] [US-1] 測試: send_trust_auto_approve_notification() — 有/無 result 兩種路徑
[001-T25] [P2] [US-1] 測試: build_info_builder() — source+context / source only / empty
[001-T26] [P2] [US-1] 測試: send_presigned_notification + send_presigned_batch_notification
```

## Execution Order

```
T01-T03 → T04-T10 → T11-T12 → T13-T15 → T16 → T17-T26
```

Tests 可和遷移並行（遷移一個就測一個）。
