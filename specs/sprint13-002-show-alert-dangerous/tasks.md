# Sprint 13-002: Tasks — show_alert for DANGEROUS

> Generated: 2026-03-05

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 1 | telegram.py、callbacks.py |
| D2 Cross-module | 1 | telegram ← callbacks（+ commands import） |
| D3 Testing | 1 | 4 個測試 case |
| D4 Infrastructure | 0 | 無 |
| D5 External | 0 | Telegram API 已有支援 |
| **Total TCS** | **3** | ✅ 不需拆分 |

## Task List

```
[002-T1] [P0] [US-1] telegram.py: answer_callback() 加 show_alert: bool = False 參數
[002-T2] [P0] [US-1] callbacks.py: handle_command_callback approve 路徑 — DANGEROUS 命令用 show_alert=True
[002-T3] [P1] [US-1] callbacks.py: 確認 is_dangerous import 存在（from commands import is_dangerous）
[002-T4] [P1] 測試: answer_callback(show_alert=True) → API data 含 show_alert
[002-T5] [P1] 測試: answer_callback() 預設 → API data 不含 show_alert
[002-T6] [P1] 測試: approve dangerous command → answer_callback called with show_alert=True
[002-T7] [P2] 測試: approve non-dangerous command → answer_callback called without show_alert
```

## Execution Order

```
T1 → T2 + T3 → T4-T7
```
