# Changelog

All notable changes to this project will be documented in this file.

## [3.59.0] - 2026-03-18

### Added
- `src/app.py`, `src/mcp_execute.py`, `src/callbacks.py` — Approval Pending Reminder: schedule EventBridge reminder N minutes after request creation (default: 10 min); auto-cleanup on approve/deny (s59-001)
- `src/constants.py` — `PENDING_REMINDER_MINUTES` constant for reminder scheduling (s59-001)
- `src/scheduler.py` — `create_pending_reminder_schedule`, `delete_reminder_schedule` methods (s59-001)
- `src/trust.py` — per-minute velocity check for trust session commands; new constants `TRUST_RATE_LIMIT_PER_MINUTE` (default: 5), `TRUST_RATE_LIMIT_ENABLED`; new exception `TrustRateExceeded`; DDB fields `rate_window_start`, `rate_window_count` (s59-002)
- `src/mcp_execute.py`, `src/callbacks_command.py`, `src/mcp_deploy_frontend.py` — error handling for `TrustRateExceeded` (s59-002)
- `tests/test_sprint59.py` — comprehensive test suite covering reminder schedule creation/skip/handler, and trust rate detection scenarios

### Metrics
- CloudWatch `TrustRateExceeded` metric on rate limit trigger (s59-002)

## [3.58.0] - 2026-03-18

### Added
- `tests/_module_list.py` — centralized `BOUNCER_MODS` list (source of truth for test isolation); fixes cross-batch moto pollution root cause from Sprint 57
- `src/callbacks_command.py` — structured `session_lifecycle` audit logs for command approved/denied/trust approved
- `src/grant.py` — structured `session_lifecycle` audit logs for grant approved/denied
- `src/deployer.py` — `_get_changed_files()` helper; code-only deploy notifications now include git diff file list (max 3 files + count)

### Refactored
- `src/mcp_upload.py` — move `get_s3_client` imports to module top-level (was repeated in 3 functions)

### Fixed
- `tests/conftest.py` — `BOUNCER_MODS` now imported from `_module_list.py`; `tests/` added to `sys.path` for xdist worker compat
- `tests/test_template_diff_analyzer.py` — add `xdist_group` to prevent cross-worker `GetSecretValue` pollution

### Workspace
- `skills/bouncer-exec/scripts/bouncer_exec.sh` — new `--cli-input-json` parameter to bypass shell quoting for complex JSON arguments

## [3.57.0] - 2026-03-18

### Changed
- `src/callbacks_command.py` — Trust 相關操作的命令（`create_trust_session`、`increment_trust_upload_count` 等）不再顯示 Trust 按鈕，避免多餘操作選項（s57-001）
- `src/app.py` — 命令請求過期時更新原始 Telegram 訊息（改為❌狀態）而非發送新通知，保持對話整潔；無 `telegram_message_id` 時 fallback 回原本行為（s57-002）

## [3.56.0] - 2026-03-18

### Fixed
- `src/callbacks_command.py` — OTP 改為 callback 時重新計算 risk_score，不依賴 DDB 存的值（s56-001）
- `src/constants.py` — `iam delete-role` 從直接 block 改為需要人工審批（s56-002）
- `src/callbacks_command.py` — 所有 `update_message` 呼叫加 error handling，防止 400 錯誤中斷流程（s56-003）
- `src/app.py` — Notifier 發送過期警告前先確認 DDB 狀態，已 approved 的請求不再收到警告（s56-004）

## [3.55.0] - 2026-03-17

### Security
- `src/otp.py` — 新增 OTP 二次驗證模組（6位數，5分鐘TTL，3次失敗鎖定）
- `src/callbacks_command.py` — risk_score ≥ 66 的命令審批前要求 OTP 確認
- `src/telegram_commands.py` — 新增 `/otp {code}` 指令

### Refactored
- `src/callbacks_upload.py` — 從 callbacks.py 抽出 upload callbacks（Phase 3）；callbacks.py 1294 → 910 行

## [3.54.1] - 2026-03-17

### Fixed
- `src/changeset_analyzer.py` — 增加 changeset max_wait 從 60s 至 120s，避免 timeout 被誤判為失敗（#137）

## [3.54.0] - 2026-03-17

### Fixed
- `src/deployer.py` — deploy lock 在 `get_execution_status` ClientError 時未釋放，導致後續所有 deploy 被卡住（s54-001）
- `src/mcp_execute.py` — `_check_auto_approve` 通知永遠顯示 ✅，改為根據 exit code 顯示 ❌/✅（s54-003）

### Security
- `src/constants.py` + `src/mcp_execute.py` — Trust Session IP binding：`_check_trust_session` 現在傳遞 `caller_ip` 給 `should_trust_approve()`；`TRUST_IP_BINDING_MODE` 預設從 `warn` 改為 `strict`（s54-002）

### Refactored
- `src/callbacks_command.py` 新建：從 `callbacks.py` 抽出 `handle_command_callback` 及 7 個輔助函數（Phase 2 拆分）；callbacks.py 從 1804 → 1294 行（s54-005）
- `src/aws_clients.py` — 確認無 raw `boto3.client('s3')` 殘留於 aws_clients.py 外（s54-004）

## [3.53.0] - 2026-03-17

### Changed
- `src/tool_schema.py` + `src/app.py` + `bouncer_mcp.py` — 移除已 deprecated 的 `bouncer_deploy_frontend` tool（由 `bouncer_request_frontend_presigned` + `bouncer_confirm_frontend_deploy` 取代）
- `src/callbacks_grant.py` — 新建，從 `callbacks.py` 抽出 `handle_grant_approve/deny` 函數（Phase 1 拆分）
- `src/callbacks.py` — 從 1900 行縮減至 1804 行

## [3.52.0] - 2026-03-16

### Security
- `src/template_diff_analyzer.py` — 新增 10 個高風險 pattern 偵測：
  - IAM 資源新增（AWS::IAM::Role/Policy/ManagedPolicy 等）
  - Trust relationship 變更（AssumeRolePolicyDocument）
  - Security Group `0.0.0.0/0` / `::/0` 開放
  - KMS Key 及 KeyPolicy 變更
  - Lambda 環境變數疑似明文 secret（password/token/api_key 等）
  - VPC/Subnet 公開 IP 設定（AssociatePublicIpAddress/MapPublicIpOnLaunch）

### Tests
- 19 tests in `tests/test_template_diff_analyzer.py`

## [3.51.0] - 2026-03-16

### Security
- `template.yaml` — CloudFormation IAM 範圍從 `Resource: "*"` 縮窄到指定 stack ARN（Sprint 33 遺留 TODO）
- Backlog 新增 6 個 `template_diff_analyzer` 安全偵測項目（IAM 變更、Security Group、Trust relationship、KMS、Lambda secret、VPC 公開 IP）

### Fixed
- `tests/test_e2e_cleanup_button_s18.py` — 加 `xdist_group` 隔離，修復 xdist 並行時 flaky

## [3.50.0] - 2026-03-16

### Changed
- `tests/conftest.py` — `app_module` + `mock_dynamodb` 從 `scope="module"` 改為 `scope="function"`，每個 test 獨立 DynamoDB + 模組狀態，消除 ordering 污染
- `.github/workflows/ci.yaml` — 改用 `-n auto --dist=loadgroup` 並行跑 tests，移除 `-p no:randomly`

### Tests
- CI 從 serial 6 分鐘 → parallel 約 4 分鐘

## [3.49.0] - 2026-03-16

### Fixed
- `tests/conftest.py` — 移除 `del sys.modules` 迴圈，改用 `importlib.reload` 針對性 reload，減少 xdist worker 競爭
- `tests/test_notifications_main.py` — `_mock_entities_send` fixture 改用 `mock.patch.object`，thread-safe mock，修復 xdist 並行時 module state 污染問題

### Tests
- `pytest -n auto --dist=loadscope` 穩定通過（從 flaky 到穩定）

## [3.48.0] - 2026-03-16

### Added
- `src/commands.py` + `src/mcp_execute.py` + `src/tool_schema.py` + `bouncer_mcp.py` — `bouncer_execute` 新增 `cli_input_json` 參數，將 dict 寫入 tempfile 並以 `--cli-input-json file://` 傳給 AWS CLI，完全繞過 shell 引號問題。支援含中文、換行、巢狀引號的 JSON 值 (#129 #130)

### Tests
- 8 new tests in `tests/test_cli_input_json_s48.py`

## [3.47.0] - 2026-03-16

### Added
- `src/telegram_entities.py` — `date_time()` 加 `unix_timestamp` 參數，生成正確的 `{unix_time: N}` entity 讓 Telegram 客戶端自動轉換時區 (#s44-003)
- `src/trust.py` + `src/grant.py` — Session lifecycle 加 `logger.info`：trust/grant 建立、批准、拒絕、撤銷都有 structured audit log (#s44-007)
- `.github/workflows/ci.yaml` — 加 `-n auto --dist=loadfile` 啟用 pytest-xdist 並行跑 tests（pytest-xdist 已在 requirements-dev.txt）(#s47-001)

### Tests
- 5 new tests in `tests/test_sprint47.py`

## [3.46.1] - 2026-03-15

### Security
- `src/callbacks.py` — 所有 callback handlers（command/deploy/upload/batch/account）加上 TTL 過期驗證，防止過期審批按鈕繼續執行操作

## [3.46.0] - 2026-03-15

### Security
- `src/app.py` — `infra_approve` callback 現在驗證 `infra_approval_token_ttl`，過期的審批請求會被拒絕，防止舊按鈕繼續觸發部署 (#s46-001)

## [3.45.1] - 2026-03-15

### Fixed
- `src/notifications.py` — 移除 entities 模式下 italic 文字的 Markdown 底線（`_text_` → `mb.italic("text")`），修復 auto-approve 通知顯示多餘底線的問題

## [3.45.0] - 2026-03-15

### Fixed
- `src/mcp_deploy_frontend.py` — `bouncer_confirm_frontend_deploy` 修正 `send_deploy_frontend_notification()` 呼叫參數：改用正確的 `files_summary` + `target_info` 取代不存在的 `file_count` (#128)
- `src/tool_schema.py` + `src/app.py` — 補上 `bouncer_request_frontend_presigned` + `bouncer_confirm_frontend_deploy` 在 Lambda handler 的路由，修復 "Unknown tool" 錯誤（Sprint 38 遺漏）
- `CLAUDE.md` — 新增新增 MCP tool 必須更新 4 個檔案的 checklist + Hotfix 不能跳過測試的規則

## [3.44.1] - 2026-03-15

### Fixed
- `src/app.py` + `src/tool_schema.py` — register `bouncer_request_frontend_presigned` and `bouncer_confirm_frontend_deploy` in Lambda handler. Sprint 38 added these tools but forgot to register them in the actual Lambda endpoint, causing "Unknown tool" errors.

## [3.44.0] - 2026-03-14

### Fixed
- `src/notifications.py` — `send_account_approval_request` 加 try-except，防止 Telegram 失敗導致 Lambda crash (#s44-004)
- `src/telegram.py` — Telegram API 失敗改 `logger.error` + emit `NotificationFailure` CloudWatch metric (#s44-005)
- `deployer/notifier/app.py` — `cleanup_changeset` silent `except: pass` 改為 print warning (#s44-006)
- `src/deployer.py` — deploy 審批請求 TTL 從 360s 延長到 7 天，方便查歷史記錄 (#s44-001)
- `src/changeset_analyzer.py` — `create_dry_run_changeset` 改用 `TemplateURL`（不再下載 TemplateBody），修復 ZTP Files YAML quote ValidationError (#s44-009)
- `deployer/template.yaml` — `NotifySuccess` SFN state `build_id` 改為空字串，修復 infra approval 路徑 unpin 不觸發 (#s44-002)

### Added
- `src/notifications.py` — `send_approval_request` 成功時將通知文字 snapshot 存入 DDB，供 UIUX 分析使用 (#s44-008)

### Tests
- 7 new tests in `tests/test_sprint44_fixes.py`

## [3.43.0] - 2026-03-14

### Fixed
- `src/grant.py` — `bouncer_request_grant` 新增 `project` 參數，自動從 `bouncer-projects` DDB 查詢 `deploy_role_arn`，存入 grant record (#127)
- `src/mcp_execute.py` — `_check_grant_session` 使用 `grant.assume_role_arn` 執行命令，有效解決 S3/CloudFront AccessDenied 問題
- `src/notifications.py` — grant 審批通知顯示專案名稱和 role 資訊

### Security
- Role ARN 永遠從 DDB project config 取得，不接受 user input

### Tests
- 6 new regression tests in `tests/test_grant_assume_role_s43.py`

## [3.42.0] - 2026-03-14

### Added
- `bouncer_mcp.py` — emit `ToolCall` CloudWatch EMF metric with `ToolName` dimension at `tools/call` dispatcher. Enables usage tracking for all MCP tools via CloudWatch Metrics Insights (#bouncer-usage-tracking)

### Tests
- 2 new tests in `tests/test_tool_usage_tracking_s42.py`

## [3.41.0] - 2026-03-14

### Fixed
- `deployer/scripts/sam_deploy.py` — add `_notify_progress()` helper, invoke NotifierLambda async at SCANNING/BUILDING/DEPLOYING phases. Fixes deploy progress message stuck at INITIALIZING (#s41-001)
- `deployer/template.yaml` — pass `NOTIFIER_LAMBDA_ARN` to CodeBuild environment variables

### Tests
- 3 new regression tests in `deployer/tests/test_notify_progress_s41.py`

## [3.40.1] - 2026-03-14

### Fixed
- `template.yaml` — add `states:SendTaskSuccess` + `states:SendTaskFailure` to `ApprovalFunctionRole` IAM policy. Required for ZTP Files infra approval SFN callback; missing permissions caused all Telegram approval notifications to fail
- `src/notifications.py` — remove `date_time` entity from approval notifications. Telegram Bot API `date_time` MessageEntity requires `{unix_time: int}`, incompatible with `MessageBuilder` offset/length pattern. Caused 400 Bad Request on all approval requests (Sprint 39 regression)

## [3.40.0] - 2026-03-14

### Fixed
- `src/mcp_execute.py` — Trust session now bypasses rate limit: `_check_trust_session` moved before `_check_rate_limit` in execution pipeline. Trust sessions are already protected by MAX_COMMANDS=20, TTL=10min, and IP binding (#31)

### Added
- `src/grant.py` + `bouncer_mcp.py` — `bouncer_request_grant` 新增 `approval_timeout` 參數（預設 300s，最大 900s=15分鐘），允許多步驟操作有更長的審批等待時間 (#29)

### Tests
- 7 new regression tests: `tests/test_sprint40_trust_rate_limit.py` (2) + `tests/test_sprint40_grant_timeout.py` (5)

## [3.39.0] - 2026-03-14

### Added
- `src/notifications.py` — `send_auto_approve_deploy_notification` 加 `changes_summary` 參數，顯示 git diff 摘要或 CFN changeset 資源清單（#s39-001）
- `src/deployer.py` — `_format_changeset_summary()` helper，格式化 resource_changes 為可讀字串
- `deployer/notifier/app.py` — Deploy checklist 新增 `ANALYZING` phase（Changeset 分析中）+ elapsed time 顯示（#43）
- `src/notifications.py` — 審批通知加上絕對過期時間（`date_time` MessageEntity，Telegram 自動轉本地時區）（#42）

### Tests
- 8 new tests: `tests/test_sprint39_ux.py` (6) + `deployer/tests/test_notifier.py` (2)

## [3.38.0] - 2026-03-14

### Added
- `bouncer_request_frontend_presigned`: Step 1 of new frontend deploy flow — generate S3 presigned PUT URLs, bypassing API Gateway 6MB payload limit (#126)
- `bouncer_confirm_frontend_deploy`: Step 2 — verify all files uploaded via head_object, create approval request (#126)
- Staging key format: `frontend/{project}/{request_id}/{filename}`, presigned URL expiry 300s

### Fixed
- `deployer/notifier/app.py` — infra approval buttons localized to English + style colors: "✅ Approve Deploy" (green), "❌ Reject Deploy" (red) (#41 #46)

### Tests
- 10 new tests in `tests/test_mcp_deploy_frontend_presigned_s38.py`

## [3.37.0] - 2026-03-13

### Added
- `src/deployer.py` — include CFN changeset resource details (Added/Modified/Removed) in infra approval notification (#123)
- `src/deployer.py` / `deployer/scripts/sam_deploy.py` — `auto_approve_code_only` flag: changeset-based auto-approve for ztp-files project when no infra changes detected (#125)

### Fixed
- `src/notifier/app.py` — `No updates` FAILED changeset status now treated as `is_code_only=True`, skipping infra approval (#124)
- `src/callbacks.py` / `src/mcp_execute.py` — add `account_id` and `account_name` to auto-approved notification (#122)
- CI: replace specific exception catches (ClientError/OSError) with generic Exception in fire-and-forget paths; fix silent except:pass patterns; fix test isolation (patch targets, DEFAULT_ACCOUNT_ID leakage)

### Tests
- 2125 tests passing (2 pre-existing flaky in test_e2e_cleanup_button_s18 — ordering dependent, pass in isolation)

## [3.36.0] - 2026-03-13

### Added
- `src/template_diff_analyzer.py` — `analyze_template_diff()`: GitHub compare API + regex scan of template.yaml diff for high-risk patterns (Principal:*, AuthType:NONE, S3 public access); fail-safe: any error → human approval (#123 S36-001)
- `src/deployer.py` — replace CFN changeset analysis with git diff template scan; code-only or no high-risk → auto-approve; high-risk findings attached to approval context (#123 S36-001)
- `src/upload_scanner.py` — `scan_upload()`: block dangerous file extensions (.exe/.sh/.bat etc.); detect secret patterns (AWS keys, GitHub PAT, private keys, hardcoded credentials); fail-open design (#smart-phase5 S36-002)
- `src/mcp_upload.py` — integrate `scan_upload()` in single upload + batch upload; blocked files rejected before approval; high-risk findings shown in Telegram approval notification (#smart-phase5 S36-002)

### Fixed
- `deployer/scripts/sam_deploy.py` — pass `artifacts_bucket` to SAM CLI `--s3-bucket` for large templates (#123)
- `deployer/scripts/sam_deploy.py` — auto-sync `sam_deploy.py` to S3 during bouncer-deployer deploy; download packaged template from S3 when `SKIP_PACKAGE=true` (#124)
- `src/template.yaml` — add `secretsmanager:GetSecretValue` to `ApprovalFunctionRole` — required for git diff auto-approve to read GitHub PAT (#123)

### Tests
- 10 new tests in `tests/test_template_diff_analyzer.py` (S36-001)
- 26 new tests in `tests/test_upload_scanner.py` (S36-002)

## [3.35.0] - 2026-03-13

### Fixed
- `src/mcp_execute.py` — `_submit_for_approval()`: catch `RuntimeError` and `Exception` in Telegram failure handler to properly cleanup DDB orphan records (#s35-002)

### Added
- `deployer/template.yaml` — Step Functions: `StartBuild.waitForTaskToken` → `AnalyzeChangeset` → `CheckChangesetResult(Choice)` → `SamDeploy` / `WaitForInfraApproval.waitForTaskToken` post-package changeset analysis flow (#122 S35-001a)
- `deployer/scripts/sam_deploy.py` — `_notify_sfn_package_complete()`: send SFN `SendTaskSuccess` with `template_s3_key` after `sam package`; `SKIP_PACKAGE` env var support for SamDeploy state (#122 S35-001b)
- `deployer/notifier/app.py` — `handle_analyze()`: dry-run changeset analysis on fresh template → `send_task_success(is_code_only)`; `handle_infra_approval_request()`: store taskToken + Telegram notification for human approval (#122 S35-001c)
- `deployer/notifier/changeset_analyzer.py` — copy of `src/changeset_analyzer.py` for use in notifier Lambda (#122 S35-001c)
- `src/scheduler_service.py` — `create_expiry_warning_schedule()`, `delete_warning_schedule()`: EventBridge Scheduler integration for approval timeout notifications (#31 S35-003)
- `src/notifications.py` — `send_expiry_warning_notification()`: ⏰ Telegram warning 60s before approval request expires (#31 S35-003)
- `src/mcp_execute.py` — call `create_expiry_warning_schedule()` after approval request created; `src/callbacks.py` — delete schedule on approve/deny (#31 S35-003)

### Infrastructure
- `deployer/template.yaml` — IAM: `NotifierLambdaRole` + `CodeBuildRole` granted `states:SendTaskSuccess`, `states:SendTaskFailure`, `states:SendTaskHeartbeat` (#122 S35-001a)

### Tests
- 9 new tests in `deployer/tests/test_notifier_analyze.py` (S35-001c)
- 8 new tests in `deployer/tests/test_sam_deploy.py` (S35-001b)
- 2 pre-existing `TestOrphanApprovalCleanup` failures resolved (S35-002)

## [3.34.0] - 2026-03-12

### Fixed
- `src/notifications.py` — `send_grant_execute_notification()`: replace unreliable `result.startswith('❌')` with `extract_exit_code()` for correct ✅/❌ status in auto-approved notifications (#102 S34-001)
- `src/callbacks.py` — `handle_command_callback()`: add immediate feedback `update_message("⏳ 執行中...")` before `execute_command()`, aligned with deploy callback pattern (#117 S34-002)

### Security
- `src/constants.py` — `TRUST_IP_BINDING_MODE`: configurable IP binding mode via `BOUNCER_IP_BINDING_MODE` env var (`strict`/`warn`/`disabled`, default `warn`) (#sec-004 S34-003)
- `src/trust.py` — `should_trust_approve()`: `strict` mode blocks IP mismatch; `warn` mode logs+metric but allows (default); `disabled` skips check entirely

### Added
- `src/telegram_entities.py` — `format_command_output()`: long output (>50 lines) → `expandable_blockquote` entity; short output → `pre` entity; empty → "(no output)" (#63 S34-004)
- `src/telegram_entities.py` — `MessageBuilder.expandable_blockquote()` method
- `src/notifications.py` — `send_trust_auto_approve_notification()` and `send_grant_execute_notification()` use `format_command_output()` for collapsible long output

### Tests
- 4 regression tests for exit code 0/1/127/no-code in `test_notifications_main.py` (S34-001)
- 1 test verifying call order in `test_callbacks_main.py` (S34-002)
- 9 new IP binding mode tests in `test_trust_ip_binding.py` (S34-003)
- 16 new tests in `tests/test_format_command_output.py` (S34-004)

## [3.33.0] - 2026-03-12

### Added
- `src/changeset_analyzer.py` — `create_dry_run_changeset()`: fetch template via S3 `GetObject` + `TemplateBody` (replaces `TemplateURL`); query existing stack params with `describe_stacks()` and pass `UsePreviousValue=True` to avoid "Parameters must have values" error; supports encrypted SAM artifacts bucket (#118 fix-2/4/6/7)
- `src/deployer.py` — Auto-approve deploy flow: `bouncer_deploy` performs dry-run changeset analysis before creating approval request; code-only changes (Lambda::Function/Version/Alias only) auto-approve and call `start_deploy()` directly; infra changes append changeset summary to context (#118)
- `src/notifications.py` — `send_auto_approve_deploy_notification()`: silent Telegram notification for auto-approved deploys (#118)
- `template.yaml` — IAM: `s3:GetObject` on `sam-deployer-artifacts-*`; `kms:Decrypt`/`kms:GenerateDataKey` for SAM artifacts KMS key; `cloudformation:CreateChangeSet`/`DescribeChangeSet`/`DeleteChangeSet`/`DescribeStacks` (#118 fix-2/5/7)

### Fixed
- `src/changeset_analyzer.py` — `is_code_only_change()`: allow `Lambda::Version Add/Delete` and `Lambda::Alias Modify` for SAM AutoPublishAlias lifecycle; empty changeset treated as safe no-op (#118 fix-1)
- `src/deployer.py` — `create_dry_run_changeset` uses stable S3 key (`bouncer/packaged-template.yaml`) instead of content-addressed hash key discovered via `list_objects_v2` (#118/#120 fix)

### Infrastructure
- DynamoDB `bouncer-projects`: `auto_approve_deploy=true`, `template_s3_url` set for `bouncer` project (#118)

### Tests
- `tests/test_changeset_analyzer.py` — Full unit test coverage for `is_code_only_change()`, `create_dry_run_changeset()`, `analyze_changeset()`, `cleanup_changeset()` (#118)

## [3.28.0] - 2026-03-11

### Security
- `src/mcp_deploy_frontend.py` — Trust Session integration: `_check_deploy_trust()` now calls `should_trust_approve()` with frontend project validation via `deploy_role_arn` check; `_execute_trusted_deploy()` implemented with audit log (`trust_bypass=True`) (#sprint29-001)

### Refactored
- `src/telegram.py`, `src/utils.py`, `src/accounts.py`, `src/notifications.py`, `src/mcp_deploy_frontend.py`, `src/mcp_history.py`, `src/metrics.py`, `src/sequence_analyzer.py`, `src/scheduler_service.py`, `src/paging.py`, `src/telegram_commands.py`, `src/mcp_presigned.py`, `src/mcp_upload.py` — Migrated all 13 remaining stdlib `logging` modules to aws-lambda-powertools `Logger`; fixed 6 `exc_info=True` incompatibilities (#sprint29-002)
- `src/mcp_history.py` — `_query_command_history_table()` replaced full-table `scan()` with GSI query on `type-created_at-index` (newest-first, cost-efficient) (#sprint29-003)
- `src/sequence_analyzer.py` — `record_command()` now writes `item_type="CMD"` + `created_at` Unix timestamp for GSI compatibility

### Fixed
- `src/callbacks.py` — Removed `pin_message()` from approval callback (was pinning static approval message) (#sprint29-004)
- `deployer/notifier/app.py` — `handle_start()` now pins progress message; `pin_telegram_message()` added; unpin in `handle_success()`/`handle_fail()` verified

### Infrastructure
- `template.yaml` — Added `type-created_at-index` GSI to `CommandHistoryTable` (`item_type` HASH + `created_at` RANGE, PAY_PER_REQUEST, ALL projection) — requires CFN stack update

### Tests
- Backend: ~2000 tests, coverage ≥ 75%

## [3.27.0] - 2026-03-11

### Refactored
- `src/deployer.py` — 6 silent bare except blocks → typed `ClientError`/`Exception` + `logger.exception()` with structured context (project_id, deploy_id) (#sprint28-001)
- `src/grant.py`, `src/risk_scorer.py`, `src/smart_approval.py`, `src/mcp_confirm.py` — Migrated from stdlib `logging` to aws-lambda-powertools `Logger` for structured JSON logs (#sprint28-002)
- `src/mcp_history.py` — 4 bare except blocks → typed exceptions (`binascii.Error`, `UnicodeDecodeError`, `json.JSONDecodeError`, `TypeError`, `ValueError`, `decimal.InvalidOperation`) + noqa annotations (#sprint28-003)
- `src/mcp_deploy_frontend.py` — 3 bare except blocks → noqa annotations with warning log (best-effort cleanup patterns) (#sprint28-003)

### Tests
- Backend: 1974 tests, coverage ≥ 75%

## [3.16.0] - 2026-03-08

### Added
- `bouncer_exec.sh` — `--json-args JSON` mode: pass pre-built JSON to bypass shell pipe truncation (#85)
- `src/aws_clients.py` — `get_s3_client()` + `get_cloudfront_client()` factory functions, replacing 6 duplicate STS/S3 patterns (#79)
- `deployer.py` — `deploy_status` response includes `failed_resources` + `error_summary` from CloudFormation events on FAILED status (#55)
- `app.py` + `callbacks.py` — Complete audit trail: `approved_by`, `approved_at`, `source_ip`, `duration_ms` written to DDB on approval (#74)
- `template.yaml` — API Gateway access log enabled with JSON format, 30-day CloudWatch retention (#76)

### Tests
- Backend: 1912 tests, coverage 89%

## [3.15.0] - 2026-03-06
> **Hotfix patches**: CI entities mock check added; deployer tests run separately to avoid import conflict; stale `_send_message` mocks updated for entities Phase 3 migration.

### Security
- `scripts/run-tests.sh` — 移除永久排除的安全測試（safelist、cross-account、assume-role、disabled-account），34 個安全測試現在在 CI 中跑 (#83)

### Fixed
- `scripts/run-tests.sh` — 加入 `deployer/tests/` 到測試收集範圍，113 個 deployer 測試現在在 CI 跑 (#82)
- `src/mcp_history.py` — `/stats` 指令改用 GSI query 而非全表 scan，降低費用和延遲 (#81)

### Refactored
- `src/notifications.py` — entities Phase 3：`send_trust_auto_approve_notification`、`send_batch_upload_notification`、`send_grant_request_notification`、`send_grant_execute_notification` 遷移至 entities 模式 (#52)

### Tests
- Backend: 1826 tests, coverage 89%

## [3.14.0] - 2026-03-06

### Fixed
- `deployer.py` — deploy 審批通知後呼叫 `post_notification_setup()`，按鈕現在會在請求過期時自動清除 (#75)
- `deployer.py` — `deploy_status` response 移除不準確的 `phase` 欄位（永遠顯示 INITIALIZING），改標記為 deprecated（#53）
- `telegram.py` — button whitelist 現在保留 `style` 欄位（Telegram Bot API 9.4 支援），移除其他未知欄位 (#60)
- `mcp_execute.py` — auto_approved / trust_auto_approved / grant 路徑 response 加入 `request_id` 欄位 (#71)
- `bouncer_exec.sh` — `rate_limited` 狀態加明確處理：顯示提示、等 15 秒後重試一次 (#73)

### Tests
- Backend: 1810 tests, coverage 89%

## [3.13.1] - 2026-03-06

### Fixed
- 32 tests updated to work with entities Phase 2 migration (mock target updated from `_send_message` to `send_message_with_entities`)
- Test isolation fixes for `TestTelegramCommandsGSI`

## [3.13.0] - 2026-03-06

### Added
- `notifications.py` — 3 核心通知函數遷移至 entities 模式（#52 Phase 2）：`send_approval_request()`、`send_blocked_notification()`、`send_account_approval_request()`
- `callbacks.py` + `telegram.py` — DANGEROUS 命令 approve 改用 `show_alert=True` modal alert（#62）
- `paging.py` + `callbacks.py` — on-demand pagination：改為 Next Page button，不再自動發所有頁（#54）
- `deployer/scripts/sam_deploy.py` — deploy 前驗證 GitHub PAT，HTTP 401 → 清楚錯誤訊息含 Secrets Manager 位置（#57）

### Fixed
- 14 個既有 failing tests 修復（not_found status、DDB project config mock、boto3 deploy assertion、test isolation）

### Tests
- Backend: 1768 tests, coverage 89%

## [3.12.0] - 2026-03-05

### Added
- `deploy_frontend` — PROJECT_CONFIGS 現在從 DynamoDB 讀取，新專案不需要 redeploy Bouncer (#68)
- `telegram.py` — `build_entities_message()` + `send_message_with_entities()` entities 模式基礎層 Phase 1 (#52)

### Fixed
- `app.py` + `scheduler_service.py` — CLEANUP handler fallback：DDB record 不存在時用 schedule event payload 的 telegram_message_id 清按鈕 (#70)
- `deployer.py` — `bouncer_deploy_status` 回 `expired` 當 TTL 已過，`not_found` 當 record 不存在，不再混用 `pending` (#69)

### Tests
- Backend: 1671 tests, coverage 89%

## [3.11.1] - 2026-03-04

### Fixed
- `callbacks.py` — `deploy_frontend` 改用 boto3 直接操作，deploy role 不再需要暫存 bucket 讀取權限（closes #67）
- `callbacks.py` — `deploy_frontend` 加入每個檔案的審計 log（file, size, source, target, request_id, project, user_id）

## [3.11.0] - 2026-03-04

### Added
- `bouncer_deploy_frontend` — 改用 per-project `deploy_role_arn`（IAM role），不再依賴 Lambda execution role；PROJECT_CONFIGS 每個專案需設定 `deploy_role_arn` (sprint11-001, closes #67)
- `deploy_status` response 新增 `progress_hint` 欄位（顯示目前階段：正在初始化 / build / CloudFormation）和 `sfn_status` 欄位（Step Functions execution status，與 `build_status` 分開）(sprint11-002, closes #53 #56)
- `sendChatAction` typing indicator — 命令執行中向 Telegram 發送 "typing" 視覺回饋 (sprint11-003, closes #61)

### Fixed
- Telegram inline keyboard `style` 欄位已移除（非標準 Bot API 欄位）；改用 `json_body=True` 正確序列化 `reply_markup`，按鈕現在正確渲染 (sprint11-004, closes #60)
- Trust session 過期且有 pending 請求時改為響鈴通知（`sound` flag），而非靜默通知 (sprint11-005, closes #65)

### Tests
- 新增 `bouncer_deploy_frontend` Phase B integration tests（execute_command S3 copy format + failure detection）(closes #59)
- Backend: 1539+ tests

## [3.10.0] - 2026-03-03

### Fixed
- `deployer.py` — `get_deploy_status()` record 不存在時回傳 `{status: pending}` 而非 error；加 `elapsed_seconds`（RUNNING）和 `duration_seconds`（SUCCESS/FAILED）(sprint10-001, closes #47)
- `mcp_execute.py` + `utils.py` — execution error tracking 改用 regex 抓 `(exit code: N)`，不再只偵測 ❌ prefix (sprint10-002, closes #48)
- `bouncer_exec.sh` — 含空格/pipe 字元的參數自動用雙引號包裹，解決 aws_cli_split 解析問題 (sprint10-003, closes #49, closes #51)

### Changed
- `notifications.py` — 11 個 Telegram 按鈕文字改為英文（Approve/Reject/Trust 10min 等） (sprint10-004, closes #46)
- `notifications.py` — 按鈕加入 Bot API 9.4 `style` 欄位（success=green/danger=red/primary=blue）(sprint10-004, closes #41)
- `notifications.py` — expires_at 顯示加入 UTC 絕對時間（如「5 分鐘後過期（UTC 14:35）」）(sprint10-005, closes #42)

### Tests
- 新增 `tests/test_deployer_sprint10_001.py`（18 tests）
- 新增 `tests/test_mcp_execute_sprint10_002.py`（21 tests）
- 新增 `tests/test_button_ux_sprint10.py`（6 tests）
- Backend: 1539 tests, coverage 89%

## [3.9.0] - 2026-03-02

### Added
- `bouncer_deploy_frontend` MCP tool — one-click frontend deploy: staging → one approval → S3 copy + CloudFront invalidation (sprint9-003, closes #32 #34)
- `execute_command` integration for `deploy_frontend` callbacks — no extra Lambda IAM permissions needed
- Trust session expiry summary — automatic Telegram notification on revoke/expiry showing executed commands list and success/failure counts (sprint9-007)
- Execution error tracking to DynamoDB: `exit_code`, `error_output`, `executed_at` fields on failure (sprint9-001, closes #38)

### Fixed
- `bouncer_upload_batch` early payload size validation — rejects oversized payloads **before** base64 decode to prevent Lambda silent failure (sprint9-002, closes #33)
- `trust_scope` missing error message improved with explicit examples (sprint9-005, closes #36)
- base64 truncation detection from OS CLI argument length limit — immediate error when `len(content) % 4 != 0` (sprint9-006, closes #37)
- Trust expiry notification for non-execute requests — shows action type correctly (sprint9-008, closes #40)
- `deploy_frontend` callback parameter order + test mock (sprint9-003b-fixup)

### Changed
- Deployment strategy reverted canary → AllAtOnce (frequent releases, canary unsuitable)

## [3.8.0] - 2026-03-02

### Added
- LambdaLogGroup CFN import support via SAM-transformed template (`sam build + sam package` flow) (sprint8-001)
- `bouncer_upload_batch` S3 verification after upload — non-blocking, results tracked in `verification_failed` field (sprint8-006, closes #35)
- Trust session expiry notification — when trust expires, affected pending requests are notified via Telegram (sprint8-007)
- `bouncer-exec` skill v1.0 — CLI-like wrapper for `bouncer_execute` with clean output and auto-poll

### Fixed
- `bouncer_deploy_history` CLI `--args` parameter mapping — `project` parameter now correctly passed (sprint8-004)
- Deploy failure message truncation — key error lines (up to 5) now stored in DynamoDB and included in Telegram notification (sprint8-002)
- REST endpoint `handle_clawdbot_request` missing Unicode normalization (NFKC) before risk checks (sprint8-003)
- EarlyValidation errors now show actionable CFN import steps instead of generic message (sprint8-005)
- Pre-existing test failures: GSI mock isolation, trust source binding, stats scan→query (7 tests fixed, sprint8-008+009)
- `test_approve_trust_batch` regression (sprint8-008)

### Security
- Added `AWS::Logs::LogGroup: LogGroupName` to `_RESOURCE_ID_KEYS` for correct CFN import resource identification

## [3.7.0] - 2026-03-01

### Added
- `sam_deploy.py`: auto-imports pre-existing CloudFormation resources on deploy conflict + `--dry-run-import` flag (sprint7-005, closes #28)
- `SchedulerService`: centralized EventBridge Scheduler management for cleanup tasks
- `TrustSession` dataclass: typed wrapper for trust session records

### Fixed
- `bouncer_execute`: `&&` chained commands now execute sequentially with proper risk-checking per sub-command (sprint7-001, closes #30)
- Over-truncation of large command output (CloudWatch Logs etc.) — full pagination via `PaginatedOutput` dataclass (sprint7-004, closes #27)
- Trust session source binding — prevents cross-source trust reuse (sprint7-006, bouncer-sec-010)
- DynamoDB history/stats queries now use GSI instead of full table Scan — prevents timeout at scale (sprint7-003)
- EventBridge Scheduler auto-removes expired approval request buttons (sprint7-002, closes #21)
- LambdaLogGroup CFN import conflict (sprint7-010)

### Refactored
- DynamoDB table initialization centralized in `db.py` via `_LazyTable` pattern (sprint7-008)
- Deduplicated `send_telegram_message_to()` and `sanitize_filename()` functions (sprint7-007)
- Lambda memory increased 256MB → 512MB for improved cold start performance (sprint7-009)

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
