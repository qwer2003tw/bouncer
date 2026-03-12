# Tasks: Emoji Based on Exit Code + command_status to DDB

[T001] [P] fix `_format_approval_response`: title emoji + `update_message` emoji based on `_is_execute_failed(result)` in callbacks.py | TCS=3 (Simple)
  D1=1 (1 file), D2=0, D3=0, D4=0, D5=0 → TCS=1+0+0+0+0=1 → after testing: +2 = 3

[T002] add `command_status` to DDB in `_execute_and_store_result` + trust callback inline update (callbacks.py) | TCS=5 (Simple)
  D1=1 (1 file), D2=2 (cross-module: DDB schema), D3=2 (補測試), D4=0, D5=0 → TCS=1+2+2+0+0=5

[T003] fix emoji判斷 in app.py auto_approve path + store `command_status` to DDB | TCS=3 (Simple)
  D1=1 (1 file), D2=2 (DDB schema), D3=0, D4=0, D5=0 → TCS=1+2+0+0+0=3

[T004] store `command_status` to DDB in mcp_execute.py (auto_approve + trust + grant paths) | TCS=5 (Simple)
  D1=1 (1 file), D2=2 (DDB schema), D3=2 (補測試), D4=0, D5=0 → TCS=1+2+2+0+0=5

[T005] 新增 test_sprint31_001_emoji_ddb.py — 5 test cases covering all paths | TCS=3 (Simple)
  D1=1 (1 test file), D2=0, D3=2, D4=0, D5=0 → TCS=1+0+2+0+0=3
