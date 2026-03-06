# Sprint 11-011: Tasks — sendChatAction typing

> Generated: 2026-03-04

## TCS Score

| 維度 | 分數 | 說明 |
|------|------|------|
| D1 Files | 2 | telegram.py + app.py |
| D2 Cross-module | 0 | 簡單 import + call |
| D3 Testing | 1 | 基本測試（function exists + called） |
| D4 Infrastructure | 0 | 無 |
| D5 External | 0 | Telegram API（已有整合） |
| **Total TCS** | **3** | ✅ 不需拆分 |

## Task List

```
[011-T1] [P0] [US-1] telegram.py: 新增 send_chat_action(action='typing') 函數
[011-T2] [P0] [US-1] app.py: handle_mcp_tool_call() 開頭呼叫 send_chat_action()
[011-T3] [P1] send_chat_action() 失敗不影響主流程（fire-and-forget）
[011-T4] [P1] 測試: send_chat_action 呼叫 _telegram_request('sendChatAction', ...)
[011-T5] [P1] 測試: handle_mcp_tool_call 會觸發 typing action
[011-T6] [P2] 測試: send_chat_action 例外不拋出（try/except 覆蓋）
```
