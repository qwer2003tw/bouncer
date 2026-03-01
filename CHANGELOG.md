# Changelog

All notable changes to this project will be documented in this file.

## [3.6.0] - 2026-02-28

### Fixed
- `callbacks.py` — `answer_callback` 前移至所有 handler 的處理流程最前端，按鈕按下後立即響應，消除 5~10 秒 spinner 延遲 (bouncer-sprint6-001, closes #25)
- `app.py` — grant callback 過期檢查新增於 `grant_approve_all` / `grant_approve_safe` / `grant_deny` 路徑，修補繞過通用 TTL check 的問題 (bouncer-sprint6-002)

### Added
- `notifications.py` — 批量上傳審批通知顯示「⏰ X 分鐘後過期」(bouncer-sprint6-002, closes #24)
- `notifications.py` — Grant 審批通知顯示「⏰ 審批期限：5 分鐘」(bouncer-sprint6-002, closes #26)
- `mcp_upload.py` — 單檔上傳通知加入過期時間顯示 (bouncer-sprint6-002)
- `template.yaml` — 新增 `LambdaLogGroup` resource，CloudWatch log retention 設為 30 天 (bouncer-sprint6-005)

### Changed
- 全 codebase `print()` 遷移至 `logging` 模組（21 個模組，95 處）；`app.py` 加 `logging.basicConfig`；保留 `metrics.py` EMF stdout 及 `mcp_server/server.py` stdio (bouncer-sprint6-004)

### Tests
- 新增 `tests/test_expires_at_display.py`（12 tests）：expires_at 顯示、grant callback 過期處理
- Backend: 1110 tests, coverage 85%

## [3.5.0] - 2026-02-27

### Added
- `tests/conftest.py` — 共用 fixtures（mock_dynamodb, app_module, _cleanup_tables），解決跨檔案 fixture isolation 問題
- 13 個模組對應測試檔，取代原本 7334 行的 test_bouncer.py

### Changed
- `test_bouncer.py`（7334行）拆分為 13 個模組測試檔：test_commands, test_mcp_execute, test_deployer_main, test_app, test_trust, test_telegram_main, test_callbacks_main, test_notifications_main, test_mcp_upload_main, test_accounts_main, test_paging, test_rate_limit, test_utils
- `conftest._cleanup_tables` 改為 optional mock_dynamodb，修復 test_ddb_400kb_fix flaky isolation

### Fixed
- OOM 根本原因：test_bouncer.py 單檔 import 整個 src/ → 現在每個測試檔只 import 對應模組
- `test_ddb_400kb_fix.py` flaky（ResourceInUseException）— conftest autouse fixture 不再強制注入無 mock_dynamodb 的測試

### Tests
- notifications.py: 59% → 100%（+53 tests）
- mcp_execute.py: 72% → 83%（+15 tests）
- callbacks.py: 79% → 85%（+10 tests）
- mcp_upload.py: 77% → 82%（+5 tests）
- deployer.py: 77% → 81%（+5 tests）
- Backend: 1098 tests, coverage 85%+

## [3.4.0] - 2026-02-27

### Security
- `execute_command()` 加 `threading.Lock`，確保 Lambda warm start 下 os.environ credential swap 是 atomic 的，防止 cross-request credential contamination (bouncer-sec-006)
- Presigned URL 生成時加入 Telegram silent 通知，提升可見性 (bouncer-sec-007)
- Grant pattern 加入 ReDoS 防護：長度 >256 chars、wildcard >10 個、`***` 連續 wildcard 全部攔截 (bouncer-sec-008)
- REST endpoint `handle_clawdbot_request` 補上 Unicode 正規化 (bouncer-sec-009)

### Fixed
- CloudWatch `LambdaDurationAlarm` 閾值從 600,000ms 修正為 50,000ms（原值永遠不會觸發）(bouncer-ops-001)
- `db.py` 改為 lazy init，import 時不再建立 DynamoDB client，降低冷啟動記憶體用量

### Added
- SNS Topic 加入 Email Subscription (`alerts@ztp.one`)，CloudWatch Alarm 觸發時會發通知 (bouncer-ops-003)
- `scripts/run-tests.sh`：分批測試腳本，搭配 cgroup `MemoryMax=2G` 防止 OOM
- `pytest.py` OOM Guard：攔截 `python3 -m pytest tests/` 全套呼叫，強制使用分批方式
- `~/.local/bin/pytest` wrapper：攔截直接呼叫 `pytest tests/`
- `requirements-dev.txt` 建立（pytest-xdist、pytest-memray 等）

### Changed
- pre-commit hook 移除全套 pytest，改為只跑 ruff lint（全套測試改由 `run-tests.sh` 手動執行）
- `execute_command()` 使用 `_execute_lock` 確保 thread safety

### Tests
- Backend: 566 passed (關鍵批次驗證)
- 全批次分段驗收：995 passed / 0 failed

## [3.3.0] - 2026-02-27

### Added
- `bouncer_confirm_upload` MCP tool — verify presigned batch upload results (S3 HeadObject check, no approval required)
- `bouncer_stats` now includes `top_sources`, `top_commands`, `approval_rate`, `avg_execution_time_seconds`
- `/stats [hours]` Telegram command for on-demand statistics
- Template scan notification integration (`template_hit_count` in approval messages)
- Trust session batch flow documentation in SKILL.md
- `STAGING_BUCKET` constant in `constants.py`

### Changed
- `bouncer_deploy` response now includes `commit_sha`, `commit_short`, `commit_message`
- Deploy approval and started notifications show commit info (`🔖 abc1234 — message`)
- Deploy conflict error is now structured: `status: conflict`, `running_deploy_id`, `started_at` (ISO 8601), `estimated_remaining`, `hint`
- Lambda env overwrite protection: `--environment Variables={}` → BLOCKED; `--environment Variables={...}` → DANGEROUS with warning
- `risk-rules.json` adds `lambda_env_overwrite` pattern (score: 80)

### Fixed
- Flaky test isolation: `test_upload_cross_account_staging_uses_default_account_id` (patch rate_limit.table in fixture)
- Decimal serialization in deploy conflict response (`started_at` now ISO 8601 string)
- GitHub Issue #13: presigned PUT silent failures now detectable via `bouncer_confirm_upload`
- GitHub Issue #17: lambda env var overwrite now blocked by compliance checker (B-LAMBDA-01)

### Tests
- 964 passed / coverage 81%+
- +41 new regression and unit tests

## [3.2.1] - 2026-02-26

### Fixed
- 自動執行通知（⚡ 自動執行）加入 `💬 原因` 欄位，方便審計 why a command ran

### Tests
- Backend: 911 passed / coverage 81.33%

## [3.2.0] - 2026-02-26

### Added
- `bouncer_stats` MCP tool — 過去 24 小時統計：top_sources、top_commands、approval_rate、avg_execution_time、hourly_breakdown（時段分布）
- `bouncer_help batch-deploy` — in-tool 批次部署流程說明
- `docs/trust-batch-flow.md` — presigned_batch → confirm_upload → grant → deploy 完整參考文件
- `SKILL.md` 批次部署完整流程 guide

### Changed
- Template scan HIGH/CRITICAL hits → 強制升級為 MANUAL 審批（不可被 trust/auto_approve 繞過）
- Trust session 審批通知加入 pending request 摘要顯示

### Fixed
- bouncer-bug-014：`test_upload_cross_account_staging_uses_default_account_id` flaky test — 用 monkeypatch 獨立 DynamoDB table setup

### Tests
- Backend: 910 passed (+24 new tests) / coverage 81.33%

## [3.1.0] - 2026-02-26

### Added
- `bouncer_confirm_upload` MCP tool — 驗證 presigned batch 上傳後每個 s3_key 是否存在於 staging bucket
  - 使用 `list_objects_v2` 批量驗證（比 N 次 HeadObject 省 API call）
  - batch_id regex 驗證：`^batch-[0-9a-f]{12}$`（防注入）
  - DynamoDB audit trail：`pk=CONFIRM#{batch_id}`，TTL 7 天
  - 最多 50 個檔案，純查詢無需 Telegram 審批
- `STAGING_BUCKET` 常數新增至 `src/constants.py`

### Fixed
- Issue #13 — presigned URL 上傳後缺乏驗證，導致後續 `grant s3 cp` 靜默 404

### Tests
- Backend: 886 passed (+18 regression tests) / coverage 81.52%


## [3.7.0] - 2026-03-01

### Fixed
- `bouncer_execute`: `&&` chained commands now execute sequentially with proper risk-checking per sub-command (#30)
- Over-truncation of large command output (CloudWatch Logs etc.) — full pagination via `PaginatedOutput` dataclass (#27)
- Trust session source binding — prevents cross-source trust reuse (bouncer-sec-010)
- DynamoDB history/stats queries now use GSI instead of full table Scan — prevents timeout at scale
- EventBridge Scheduler auto-removes expired approval request buttons (#21)

### Added
- `sam_deploy.py`: auto-imports pre-existing CloudFormation resources on deploy conflict + `--dry-run-import` flag (#28)
- `SchedulerService`: centralized EventBridge Scheduler management for cleanup tasks
- `TrustSession` dataclass: typed wrapper for trust session records

### Refactored
- DynamoDB table initialization centralized in `db.py` via `_LazyTable` pattern
- Deduplicated `send_telegram_message_to()` and `sanitize_filename()` functions
- Lambda memory increased 256MB → 512MB for improved cold start performance
