# Bouncer Sprint 8 — Task List

> Generated: 2026-03-01

---

[S8-001] [P0] [SEQUENTIAL] fix: import LambdaLogGroup into CFN（用 SAM-transformed template）
  Files: deployer/scripts/sam_deploy.py, template.yaml
  Depends: none (first in chain)
  Estimate: High
  Notes: Add AWS::Logs::LogGroup to _RESOURCE_ID_KEYS; add LambdaLogGroup resource to template.yaml with DeletionPolicy=Retain; modify import_resources() to use sam-packaged template for CFN import

[S8-002] [P0] [SEQUENTIAL] fix: deploy 失敗訊息截斷，自動存關鍵錯誤行到 DynamoDB
  Files: src/deployer.py
  Depends: S8-005 (error patterns)
  Estimate: Medium
  Notes: New _extract_error_lines() helper; store error_lines list in deploy history DynamoDB; update Telegram failure notification to include top 3 error lines

[S8-003] [P0] [PARALLEL] fix: REST endpoint handle_clawdbot_request 缺少 Unicode 正規化
  Files: src/app.py, src/mcp_execute.py
  Depends: none
  Estimate: Low
  Notes: Import _normalize_command from mcp_execute; apply before block/risk checks in handle_clawdbot_request(); optionally add unicodedata.normalize('NFKC') step

[S8-004] [P1] [PARALLEL] fix: bouncer_deploy_history CLI --args 無法使用
  Files: src/deployer.py, src/tool_schema.py (investigation: mcporter CLI, HTTP transport)
  Depends: none
  Estimate: Medium
  Notes: Investigation-first task; reproduce with curl to isolate Lambda vs CLI issue; fix may be schema, coercion, or mcporter config

[S8-005] [P1] [SEQUENTIAL] fix: EarlyValidation 錯誤時明確 CFN import 提示（sam_deploy.py）
  Files: deployer/scripts/sam_deploy.py
  Depends: S8-001 (template changes)
  Estimate: Medium
  Notes: Add EarlyValidation regex pattern; improve error messages with actionable import commands; include AWS docs link; add stack events check suggestion

[S8-006] [P1] [PARALLEL] feat: bouncer_upload_batch 上傳後自動驗證 S3 結果
  Files: src/mcp_upload.py
  Depends: none
  Estimate: Medium
  Notes: New _verify_upload() helper using s3.head_object(); add verified/s3_size/expected_size to response; best-effort (don't block on verification failure)

[S8-007] [P1] [PARALLEL] feat: trust session 過期後通知受影響的 pending 請求
  Files: src/trust.py, src/notifications.py, src/app.py, src/scheduler_service.py
  Depends: none
  Estimate: High
  Notes: Schedule EventBridge trigger on trust creation; new handle_trust_expired() handler in app.py; query pending requests by source; send Telegram notification; cancel schedule on revocation
