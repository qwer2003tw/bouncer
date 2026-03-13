# S36-001: Tasks

## TCS: 8 (Medium) — 1 agent, timeout 900s

### Phase 1: Research
```
[T1] 確認 deployer.py auto_approve 現有邏輯位置（line ~968）
[T2] 確認 DDB deploy history schema — 有沒有儲存 commit SHA
[T3] 確認 project config 有 git_repo + git_repo_owner 欄位
[T4] 確認 GitHub PAT secret name（GITHUB_PAT_SECRET env var）
```

### Phase 2: template_diff_analyzer.py
```
[T5] 新增 src/template_diff_analyzer.py
[T6] HIGH_RISK_PATTERNS 定義（8 個 pattern）
[T7] get_github_pat() — Secrets Manager 取 PAT
[T8] fetch_template_diff() — GitHub compare API
[T9] scan_diff_for_high_risk() — regex scan 新增行
[T10] analyze_template_diff() — main entry point + fail-safe
```

### Phase 3: deployer.py
```
[T11] 取得 head_sha（GitHub API 取最新 commit）
[T12] 取得 base_sha（DDB deploy history 上次成功 deploy）
[T13] 替換 changeset 分析邏輯 → template_diff_analyzer
[T14] auto-approve path：直接 start_deploy + notification
[T15] 高風險 path：context 附上 findings，走人工審批
```

### Phase 4: Tests
```
[T16] test: no template.yaml in diff → auto-approve
[T17] test: template changes, no high-risk → auto-approve
[T18] test: Principal:* added → human approval + finding in context
[T19] test: AuthType NONE added → human approval
[T20] test: GitHub API fails → fallback to human approval
[T21] test: auto_approve_deploy=false → skip analysis entirely
```

### Phase 5: Lint + CI
```
[T22] ruff check src/ mcp_server/
[T23] git commit --no-verify
[T24] push → wait for CI pass (gh run list)
```

## Success Metrics
- code-only push → bouncer_deploy 自動批准，Steven 不用按任何按鈕 ✅
- Principal:* 變更 → 人工審批，Telegram 顯示具體危險項目 ✅
- coverage ≥ 75% ✅
