# Changelog

All notable changes to this project will be documented in this file.

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

