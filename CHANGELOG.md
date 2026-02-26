# Changelog

All notable changes to this project will be documented in this file.

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

