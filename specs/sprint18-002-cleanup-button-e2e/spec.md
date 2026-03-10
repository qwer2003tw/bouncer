# Sprint 18-002: CLEANUP button end-to-end 驗證 + 修補

> GitHub Issue: #75
> Priority: P0
> TCS: 5
> Generated: 2026-03-08
> Depends on: Sprint 14-001 (deploy-expiry-schedule)

---

## Problem Statement

Sprint 14-001 新增了 deploy 請求的 EventBridge expiry schedule + CLEANUP handler，讓過期按鈕自動清除。但端到端流程從未完整測試過：

1. **deployer.py (L946-952)**：`post_notification_setup()` 將 `telegram_message_id` 存入 DDB 並建立 EventBridge schedule
2. **scheduler_service.py (L95-141)**：`create_expiry_schedule()` 建立一次性 schedule，payload 含 `request_id` + `telegram_message_id`
3. **app.py (L92-160)**：`handle_cleanup_expired()` 接收 schedule trigger，查 DDB → 更新 Telegram 訊息為「⏰ 已過期」並移除按鈕

### 已知問題

- `handle_cleanup_expired()` 在 DDB item 不存在時有 fallback（用 event 中的 `telegram_message_id`），但 **deployer 的 `post_notification_setup()` 只在 deploy 請求中呼叫**——普通 `execute` 請求的 expiry 由 `notifications.py` 的 `post_notification_setup()` 處理，兩者路徑不同
- EventBridge schedule 的 `at()` 表達式時區是否正確未驗證
- 從 schedule 到 Lambda invocation 到 Telegram API 的完整鏈路缺少 integration test

## Scope

### 變更 1: 端到端 integration test — deploy 路徑

**檔案：** `tests/test_cleanup_e2e.py`（新建）

測試 deployer.py `send_deploy_notification()` 呼叫 `post_notification_setup()` 後：
1. DDB record 有 `telegram_message_id` 欄位
2. `SchedulerService.create_expiry_schedule()` 被呼叫，參數含正確的 `request_id` + `telegram_message_id`
3. 模擬 schedule 觸發，呼叫 `handle_cleanup_expired()`，驗證：
   - status=pending → 更新訊息 + 移除按鈕 + 標記 timeout
   - status=approved → no-op
   - DDB item 不存在 + fallback message_id → 嘗試清除按鈕

### 變更 2: 端到端 integration test — execute 路徑

**檔案：** `tests/test_cleanup_e2e.py`

測試 notifications.py `send_approval_request()` → `post_notification_setup()` → schedule → cleanup 的完整鏈路。

### 變更 3: 端到端 integration test — 邊界情況

**檔案：** `tests/test_cleanup_e2e.py`

| Case | 描述 | 預期 |
|------|------|------|
| schedule payload 缺 `telegram_message_id` | DDB 也沒有 | 優雅 skip，不 crash |
| `update_message` Telegram API 失敗 | 網路錯誤 | log warning，仍回 200 |
| schedule 在按鈕已被手動點擊後觸發 | status 已非 pending | no-op |
| `expires_at` 為過去時間 | 立即觸發 | schedule 仍建立成功 |

### 變更 4: 修補 `handle_cleanup_expired` 中 `escape_markdown` import

**檔案：** `src/app.py` (L156 附近)

`handle_cleanup_expired()` 使用 `escape_markdown()` 構建過期訊息文字。確認：
- import 是否從 `telegram` 模組取得
- 是否該遷移為 entities 模式（若需要，建新 issue，此 task 只確認）

### 變更 5: Schedule 時區驗證

**檔案：** `tests/test_cleanup_e2e.py`

驗證 `_format_schedule_time(expires_at)` 的 `at()` 表達式格式正確：
- Unix timestamp → `at(YYYY-MM-DDTHH:MM:SS)` UTC
- 時區設定為 `"UTC"`

## Out of Scope

- EventBridge Scheduler 的真實 AWS 環境 integration test（用 mock）
- `handle_cleanup_expired` 遷移到 entities 模式（如需要，另開 issue）
- 非 deploy/execute 類型請求的 cleanup（upload、grant 等目前無 expiry schedule）

## Test Plan

| # | 測試 | 預期 |
|---|------|------|
| T1 | Deploy 路徑：`send_deploy_notification` → DDB → schedule → cleanup | 訊息更新為「⏰ 已過期」，按鈕移除 |
| T2 | Execute 路徑：`send_approval_request` → DDB → schedule → cleanup | 同上 |
| T3 | 已批准的請求觸發 cleanup | no-op，回 200 |
| T4 | DDB 無 record + fallback message_id | 嘗試清除按鈕 |
| T5 | DDB 無 record + 無 fallback | skip，回 200 |
| T6 | Telegram API 失敗 | log warning，回 200（不 crash） |
| T7 | `_format_schedule_time` UTC 格式 | `at(2026-03-08T17:00:00)` 格式 |

### 回歸測試

- 既有 cleanup 相關測試全部通過
- `test_notifications_main.py` 全部通過

## Acceptance Criteria

- [ ] 新增 `tests/test_cleanup_e2e.py`，≥ 7 個 test case
- [ ] Deploy 路徑端到端驗證通過
- [ ] Execute 路徑端到端驗證通過
- [ ] 所有邊界情況覆蓋
- [ ] 確認 `handle_cleanup_expired` 的 `escape_markdown` 使用是否需要遷移（記錄在 spec 或 issue）
- [ ] 既有測試全部通過
- [ ] Coverage ≥ 75%
