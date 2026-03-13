# Bouncer — Claude Code Guidelines

## Project Overview
AWS CLI command approval system. Python 3.12 Lambda (ARM64) + SAM + DynamoDB.
All AWS ops go through Bouncer itself — no direct AWS CLI.

## Critical Rules

### Testing
- **DO NOT run `bash scripts/run-tests.sh --all`** — it triggers 60s+ silence → acpx timeout
- Run lint only: `ruff check src/ mcp_server/`
- Commit with `git commit --no-verify` (skip pre-commit hooks)
- Full tests run via GitHub CI after push

### CI
- After every push: check `gh run list --limit 1`
- CI failure = P0 blocker — fix immediately, never leave CI red
- CI runs full suite: pytest + coverage + typos + security

### Commits
- Branch: `feat/{name}`, `fix/{desc}`, `refactor/{scope}`
- Never commit to master directly
- Format: `feat|fix|refactor|test|docs[(scope)]: description`
- Bug fix: must include `test_regression_<description>`

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
