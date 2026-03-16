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
- Update `CHANGELOG.md`
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

## Repo
https://github.com/qwer2003tw/bouncer
