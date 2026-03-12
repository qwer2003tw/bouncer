# Tasks: Logging [TAG] Prefix → Structured Extra Fields 收尾

[T001] convert logger calls in app.py (21 calls): remove [TAG] prefix + add extra={} | TCS=3 (Simple)
  D1=1 (1 file), D2=0, D3=0 (no test assertions on log format), D4=0, D5=0 → TCS=1+0+0+0+0=1 → with volume complexity: 3

[T002] convert logger calls in mcp_execute.py (20 calls) | TCS=3 (Simple)
  D1=1 (1 file), D2=0, D3=0, D4=0, D5=0 → TCS=3

[T003] convert logger calls in notifications.py + telegram.py + callbacks.py (28 calls total) | TCS=5 (Simple)
  D1=3 (2-4 files), D2=0, D3=0, D4=0, D5=0 → TCS=3+0+0+0+0=3 → with volume: 5

[T004] convert logger calls in mcp_deploy_frontend.py + mcp_upload.py + deployer.py (19 calls) | TCS=5 (Simple)
  D1=3 (2-4 files), D2=0, D3=0, D4=0, D5=0 → TCS=3+0+0+0+0=3 → with volume: 5

[T005] convert logger calls in risk_scorer.py + scheduler_service.py + mcp_history.py + paging.py + utils.py + sequence_analyzer.py + mcp_presigned.py + mcp_confirm.py (remaining ~20 calls) | TCS=5 (Simple)
  D1=5 (>4 files), D2=0, D3=0, D4=0, D5=0 → TCS=5+0+0+0+0=5

[T006] verify: run `grep -rn "logger\." src/ | grep -v "extra=" | wc -l` target < 10 | TCS=1 (Simple)
  D1=1, D2=0, D3=0, D4=0, D5=0 → TCS=1
