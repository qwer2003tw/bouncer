# Tasks: Deploy Pin → Notifier Progress Msg + Auto Unpin

[T001] [P] add `pin_message(message_id)` call in `handle_deploy_callback` after update_message (src/callbacks.py) | TCS=3 (Simple)
  D1=1 (1 file), D2=2 (cross-module: telegram.pin_message), D3=0, D4=0, D5=0 → TCS=1+2+0+0+0=3

[T002] verify + fix Notifier Lambda message_id flow: handle_start updates existing message, add thread support (deployer/notifier/app.py) | TCS=7 (Medium)
  D1=1 (1 file), D2=2 (DDB read from history table), D3=2 (補測試), D4=0, D5=2 (已知 Telegram API) → TCS=1+2+2+0+2=7

[T003] check/add MESSAGE_THREAD_ID env var in deployer/template.yaml | TCS=4 (Simple)
  D1=1 (1 file), D2=0, D3=0, D4=4 (template.yaml), D5=0 → TCS=1+0+0+4+0=5 → round to Simple=5

[T004] 新增/擴充 test_pin_unpin_deploy.py + test_sprint31_003_deploy_pin.py — 5 test cases | TCS=5 (Simple)
  D1=3 (2-4 test files), D2=0, D3=2, D4=0, D5=0 → TCS=3+0+2+0+0=5
