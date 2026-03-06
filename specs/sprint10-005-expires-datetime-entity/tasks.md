# Sprint 10-005: Tasks — expires_at date_time entity

> Generated: 2026-03-03

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 3 | telegram.py, notifications.py, + 呼叫端 (2-3 files) |
| D2 Cross-module | 4 | telegram ← notifications ← mcp_execute/callbacks（interface 變更：新增 entities 參數 + expires_at 傳遞） |
| D3 Testing | 2 | 補測試（entities offset 計算 + integration） |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 2 | Telegram Bot API 9.5（已知 service，新 feature） |
| **Total TCS** | **11** | ✅ 不需拆分（但需先做 R&D spike） |

## Task List

```
[005-T1] [P0] R&D Spike：查 Bot API 9.5 date_time entity 用法 + 確認 parse_mode/entities 互斥性
[005-T2] [P0] R&D Spike：發測試 Telegram 消息驗證 date_time entity 效果
[005-T3] [P1] [US-1] telegram.py — send_telegram_message() 支援 optional entities 參數
[005-T4] [P1] [US-1] notifications.py — send_approval_request() 傳入 expires_at + date_time entity
[005-T5] [P1] [US-1] notifications.py — send_account_approval_request() 同上
[005-T6] [P1] [US-1] notifications.py — send_grant_request_notification() 同上
[005-T7] [P1] [US-1] notifications.py — send_batch_upload_notification() 同上
[005-T8] [P1] [US-1] notifications.py — send_presigned_notification() + send_presigned_batch_notification() 同上
[005-T9] [P1] 呼叫端更新：確保 expires_at timestamp 傳入通知函數
[005-T10] [P2] 測試：entities offset/length 計算
[005-T11] [P2] 測試：integration — 實際 Telegram 消息確認
```

## ⚠️ 注意事項

- **T1-T2 為 prerequisite**：必須先完成 R&D spike 確認技術可行性，才能進行後續 tasks
- 若 `parse_mode` 和 `entities` 確認互斥，TCS 可能升至 13+（需要將所有 Markdown 格式轉為 entities）
- 若 API 有 HTML/Markdown 語法支援 date_time，TCS 會低很多（直接在 text 中嵌入）
