# Tasks: deploy_status 區分 expired vs pending

[T001] [P] add TTL expiry check in `mcp_tool_status` (src/mcp_admin.py) — mirror deployer.py pattern | TCS=3 (Simple)
  D1=1 (1 file), D2=0, D3=2 (補測試), D4=0, D5=0 → TCS=1+0+2+0+0=3

[T002] 新增 test_sprint31_004_status_expired.py — 5 test cases | TCS=3 (Simple)
  D1=1 (1 test file), D2=0, D3=2, D4=0, D5=0 → TCS=1+0+2+0+0=3
