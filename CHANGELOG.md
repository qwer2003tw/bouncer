# Changelog

All notable changes to this project will be documented in this file.

## [3.4.0] - 2026-02-27

### Added
- (TODO: fill in)

### Changed
- (TODO: fill in)

### Fixed
- (TODO: fill in)

### Tests
- Backend: (TODO: test counts)

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

