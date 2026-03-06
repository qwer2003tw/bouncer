# Sprint 9-007: Tasks — 批次信任執行後 Telegram 摘要

> Generated: 2026-03-02

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 5 | trust.py, callbacks.py, notifications.py, scheduler_service.py, app.py (handler) |
| D2 Cross-module | 4 | trust ← callbacks, trust ← scheduler, notifications ← callbacks（interface change: 新函數 + DDB schema） |
| D3 Testing | 2 | 補測試（命令追蹤 + 摘要生成） |
| D4 Infrastructure | 0 | EventBridge scheduler 已存在基礎（scheduler_service.py） |
| D5 External | 2 | EventBridge Scheduler（已知 AWS service） |
| **Total TCS** | **13** | ⚠️ 剛好 13，建議拆分為 Phase A (命令追蹤 + revoke 摘要) 和 Phase B (到期摘要) |

## Task List

### Phase A: 命令追蹤 + Revoke 摘要（可獨立交付）
```
[007-T1] [P2] [US-1] trust.py: trust session DDB item 新增 commands_executed / uploads_executed list
[007-T2] [P2] [US-1] trust.py: should_trust_approve() 成功後 append 命令摘要到 list（DDB list_append）
[007-T3] [P2] [US-1] trust.py: should_trust_approve_upload() 成功後 append 上傳摘要
[007-T4] [P2] [US-1] notifications.py: 新增 send_trust_session_summary() 函數
[007-T5] [P2] [US-1] callbacks.py: revoke_trust callback 中呼叫 send_trust_session_summary()
```

### Phase B: 到期摘要（依賴 Phase A）
```
[007-T6] [P2] [US-1] trust.py: create_trust_session() 中註冊 EventBridge scheduled event
[007-T7] [P2] [US-1] app.py: 新增 scheduled event handler → 讀取 session → 發摘要
[007-T8] [P2] [US-1] 防重複：已 revoke 的 session 跳過到期摘要
```

### Phase C: 測試
```
[007-T9]  [P2] [US-2] 測試：命令追蹤 list_append 正確性
[007-T10] [P2] [US-1] 測試：send_trust_session_summary() 格式和截斷
[007-T11] [P2] [US-1] 測試：revoke 路徑端到端
```
