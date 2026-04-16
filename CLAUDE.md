# Bouncer — Claude Code Guidelines

## Project Overview
AWS CLI command approval system. Python 3.12 Lambda (ARM64) + SAM + DynamoDB.
All AWS ops go through Bouncer itself — no direct AWS CLI.

## Critical Rules

### Testing — CRITICAL RULE

**DO NOT run `bash scripts/run-tests.sh --all` or any full test suite.**

This causes acpx 60-second no-output timeout → exit code 3.

Your job as Claude Code:
1. Write code
2. Run lint only: `ruff check src/ mcp_server/`
3. Optionally run a **single relevant test file** with `-v`: `python3 -m pytest -v tests/test_specific.py`
4. `git commit --no-verify`
5. Report commit hash

Full test suite is run by GitHub CI after push. Do NOT run it locally.

### UI Testing — Use Playwright (NOT browser tool)

The OpenClaw `browser` tool's snapshot/act/navigate commands timeout on this machine (CDP not responding). Use Python Playwright instead for all UI verification.

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path='/home/ec2-user/.cache/ms-playwright/chromium-1208/chrome-linux/chrome',
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    page = browser.new_page()
    page.goto('https://files-dev.ztp.one', timeout=15000)
    page.screenshot(path='/tmp/ui_test.png')
    browser.close()
```

Never use `browser(action="snapshot")`, `browser(action="act")`, `browser(action="navigate")` — they will timeout.

## CI
- After every push: check `gh run list --limit 1`
- CI failure = P0 blocker — fix immediately, never leave CI red
- CI runs full suite: pytest + coverage + typos + security

### Commits & Merge (PR Required)
- Branch: `feat/{name}`, `fix/{desc}`, `refactor/{scope}`
- **Never push to master directly** — branch protection enforced
- Format: `feat|fix|refactor|test|docs[(scope)]: description`
- Bug fix: must include `test_regression_<description>`

**After `git commit --no-verify` on feature branch:**
```bash
git push origin {branch}
gh pr create --title "description" --body "" --base master --head {branch}
# CI runs automatically. Check: gh pr checks {number} --watch
# After CI passes, merge:
gh pr merge {number} --squash --delete-branch
```

### Bouncer Dual-Stack Architecture

Bouncer has TWO separate Lambda stacks:
- **bouncer stack** (`src/`) — main approval Lambda, `bouncer_mcp.py`, `template.yaml`
- **bouncer-deployer stack** (`deployer/notifier/`, `deployer/template.yaml`) — NotifierLambda, CodeBuild workflow

**Some files exist in BOTH stacks** with independent copies:
- `changeset_analyzer.py` → `src/changeset_analyzer.py` AND `deployer/notifier/changeset_analyzer.py`

**When fixing a bug: always search BOTH `src/` AND `deployer/notifier/`** to avoid fixing only one copy.
```bash
grep -rn "the_function_or_pattern" src/ deployer/notifier/
```

---

### Adding a New MCP Tool — Checklist (MUST do all 4)

When adding a new MCP tool (e.g. `bouncer_foo`), you MUST update ALL 4 files:

1. **`src/mcp_*.py`** — implement the tool function `mcp_tool_foo(req_id, arguments)`
2. **`src/tool_schema.py`** — add `MCP_TOOLS['bouncer_foo'] = { 'description': ..., 'inputSchema': ... }`
3. **`src/app.py`** — import the function + add to `TOOL_HANDLERS = { 'bouncer_foo': mcp_tool_foo, ... }`
4. **`bouncer_mcp.py`** — add tool schema to `TOOLS` list + handler function + dispatcher `elif` case

Missing ANY of these = the tool will work locally (mcporter list) but fail on Lambda ("Unknown tool").


**NEVER do any of the following:**
- Version bump (`src/constants.py VERSION` or `pyproject.toml`)
- Update `README.md` version in title
- Update `CHANGELOG.md` (prepend to top, newest first)
- `git push` to remote
- Create git tags (`git tag`)
- Close sprint state
- Update `MEMORY.md`

Phase 5 is **main-session only**. Your job ends at `git commit --no-verify` + report commit hash.
If you see yourself about to do any of the above — **stop immediately and report to main session**.

### Hotfix Rule — No Shortcuts

Even for urgent hotfixes:
1. **Before pushing**: run `ruff check src/` + `typos src/` + `python3 -m pytest tests/test_app.py tests/test_mcp_execute.py -x -q`
2. **Before copying a code pattern**: read ONE existing entry in the same file to verify key names and structure match
3. **Time pressure ≠ skip verification** — a broken hotfix is worse than a delayed one

### Deploy
- Use `bouncer_deploy` MCP tool (not direct AWS)
- After deploy request: write `pendingDeployId` to sprint-state
- After approval: clear `pendingDeployId` from sprint-state
- Smoke test after every deploy

### Security
- Never bypass `compliance_checker`
- Never loosen safelist without explicit approval
- No hardcoded credentials

## Logging Standard（強制）

每個 PR 合併前必須確認：

### 必須有 INFO log 的情況
1. **MCP tool 入口**：`logger.info("Tool called", extra={"src_module": ..., "operation": "tool_called", "tool": tool_name})`
2. **審批決策**：approved/denied/auto_approved，必須含 `request_id`
3. **關鍵 DDB 操作結果**：put/update/delete 的成功結果
4. **Early return 路徑**：任何提前 return（包括 not found、skip）
5. **狀態轉換**：OTP created/validated, trust session created/expired, grant started/completed

### Structured logging 欄位（必填）
- `src_module`：檔案名（不含 .py）
- `operation`：函數名或操作名
- `request_id`（如有）：對應的審批請求 ID

### PR Checklist 新增
- [ ] 新增的關鍵路徑是否有 INFO log（含 `src_module`, `operation`, `request_id`）？
- [ ] Early return 路徑是否有 INFO log？

## Code Style
- Python 3.12, ruff for linting
- Entities pattern for Telegram messages (not raw `send_message`)
- Coverage ≥ 75%

## Project Structure
```
src/          # Lambda handlers
mcp_server/   # MCP server
deployer/     # SAM deployer (CodeBuild)
tests/        # pytest tests
scripts/      # run-tests.sh, close-sprint.sh
```

## CloudWatch Logs 查詢模組

新增模組 `src/mcp_query_logs.py` + `src/callbacks_query_logs.py`：
- `bouncer_query_logs`: 專用日誌查詢 tool
- `bouncer_logs_allowlist`: 允許名單管理 tool
- DDB key: `LOGS_ALLOWLIST#{account_id}#{log_group}`
- Callback actions: `approve_query_logs`, `approve_add_allowlist`, `deny_query_logs`
- filter_pattern 有 sanitize（只允許安全字元）
- allowlist 有 5min TTL cache

## bouncer_execute Deprecation 計劃

| 版本 | 行動 |
|------|------|
| v3.65+ | `bouncer_execute_native` 為推薦選項，`bouncer_execute` description 加入 deprecation 提示 |
| v3.67 | `bouncer_execute` 標記 deprecated（功能仍可用） |
| v3.70 | `bouncer_execute` 移除，所有呼叫方需遷移至 `bouncer_execute_native` |

**遷移方式：**
- `bouncer_execute(command="aws eks create-cluster --kubernetes-version 1.32 ...")`
- → `bouncer_execute_native({"aws": {"service": "eks", "operation": "create_cluster", "params": {"version": "1.32", ...}}, "bouncer": {...}})`

## Repo
https://github.com/qwer2003tw/bouncer

## Pagination（Sprint 83+）

- MCP response 回傳完整結果（不分頁）
- Telegram 通知用 `store_paged_output()` 分頁（3800 chars/page）
- `_write_all_pages()` 寫入所有頁面（含 page 1）到 DDB
- `bouncer_get_page` MCP tool 已移除（Sprint 83）

## Telegram 4096 Safety（強制）

所有送出 Telegram 訊息的路徑都必須確保 ≤ 4096 字元：
- `send_telegram_message_silent` / `send_message_with_entities` 有底層 safety net
- 上層函數（notification、callback）自己負責截斷
- **Notifier Lambda 也要遵守**（`handle_infra_approval_request` 曾因超長 changeset 導致 400 → deploy 永久卡住 #301）

## Deploy Mode（設計中 #249）

`deploy_mode` enum 取代舊的 `auto_approve_deploy` + `auto_approve_code_only`：
- `manual`：每次人工審批（預設）
- `auto_code`：code-only 自動，infra 要審批
- `auto_all`：全自動（僅 bootstrapper）

⚠️ `auto_approve_deploy` 從未生效過（Lambda 無 git binary #288）
