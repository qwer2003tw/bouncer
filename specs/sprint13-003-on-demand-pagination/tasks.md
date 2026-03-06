# Sprint 13-003: Tasks — On-Demand Pagination

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 2 | callbacks.py、app.py（callback 路由） |
| D2 Cross-module | 1 | callbacks ↔ paging ↔ app（callback router） |
| D3 Testing | 2 | handle_show_page + approve path 測試 |
| D4 Infrastructure | 0 | 無 template.yaml 變更 |
| D5 External | 1 | Telegram inline button callback（已有經驗） |
| **Total TCS** | **6** | ✅ 不需拆分 |

## Task List

### Core

```
[003-T1] [P0] [US-1] callbacks.py: 移除 send_remaining_pages() 呼叫（L344），改為分頁提示
[003-T2] [P0] [US-2] callbacks.py: approve path — paged output 時發額外訊息，帶 "Show Page 2" inline button
[003-T3] [P0] [US-2] callbacks.py: 新增 handle_show_page_callback() — 解析 show_page:{id}:{n}，拉 DDB page，發訊息
[003-T4] [P0] [US-2] app.py: callback router 新增 show_page: 路由到 handle_show_page_callback
[003-T5] [P1] [US-2] handle_show_page_callback: page_num < total_pages 時附帶 "Show Page N+1" 按鈕
[003-T6] [P1] [US-2] handle_show_page_callback: page expired (DDB TTL) → answer_callback error 提示
```

### Cleanup

```
[003-T7] [P2] [US-1] 考慮移除 paging.py send_remaining_pages() 函數（確認無其他 caller 後）
[003-T8] [P2] [US-1] callbacks.py: 移除 send_remaining_pages import（如 T7 完成）
```

### 測試

```
[003-T9]  [P0] 測試: approve paged output → 不呼叫 send_remaining_pages，發分頁提示訊息
[003-T10] [P1] 測試: handle_show_page_callback — 正常頁面 → 發訊息 + 下一頁按鈕
[003-T11] [P1] 測試: handle_show_page_callback — 最後一頁 → 無按鈕
[003-T12] [P1] 測試: handle_show_page_callback — page expired → error callback
[003-T13] [P2] 測試: callback_data 格式解析 edge case（malformed data）
```

## Execution Order

```
T1 → T2 → T3-T4 → T5-T6 → T7-T8 → T9-T13
```
