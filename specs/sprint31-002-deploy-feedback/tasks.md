# Tasks: Immediate Feedback After Deploy Approval

[T001] [P] add immediate `update_message` call in `handle_deploy_callback` before `start_deploy` (callbacks.py) | TCS=3 (Simple)
  D1=1 (1 file), D2=0, D3=2 (補測試), D4=0, D5=0 → TCS=1+0+2+0+0=3

[T002] same fix for `handle_deploy_frontend_callback` (callbacks.py) | TCS=1 (Simple)
  D1=1 (same file, already modified in T001), D2=0, D3=0, D4=0, D5=0 → TCS=1

[T003] 新增 test_sprint31_002_deploy_feedback.py — 4 test cases | TCS=3 (Simple)
  D1=1 (1 test file), D2=0, D3=2, D4=0, D5=0 → TCS=1+0+2+0+0=3
