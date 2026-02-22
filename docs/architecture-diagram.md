```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                              Bouncer v3.0.0 — 整體架構                                   │
└─────────────────────────────────────────────────────────────────────────────────────────┘

 ┌──────────────────┐     ┌──────────────────┐
 │  OpenClaw Agent   │     │  Steven (審批者)   │
 │  (Private Bot)    │     │  Telegram App     │
 └────────┬─────────┘     └────────┬──────────┘
          │ MCP (stdio)            │ Webhook callback
          ▼                        │ (approve/deny/trust)
 ┌──────────────────┐              │
 │  bouncer_mcp.py  │              │
 │  (本機 MCP Server) │              │
 └────────┬─────────┘              │
          │ HTTPS                  │
          ▼                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     API Gateway (REST)                               │
│                     bouncer-prod-api                                 │
│                                                                     │
│  POST /           POST /mcp          POST /webhook   GET /status/   │
│  (REST legacy)    (MCP JSON-RPC)     (Telegram)      (polling)      │
│                                                                     │
│  ⚠️ 無 WAF、無 API Key、無 Usage Plan                                │
│  認證: Application 層 X-Approval-Secret / X-Telegram-Bot-Api-Secret │
│  CORS: AllowOrigin: '*'                                             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Lambda: bouncer-prod-function                     │
│                    Runtime: Python 3.9 (ARM64)                      │
│                    Memory: 256MB / Timeout: 900s                    │
│                    Tracing: X-Ray Active                            │
│                    DLQ: SQS bouncer-dlq                             │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    請求處理 Pipeline                          │   │
│  │                                                              │   │
│  │  ┌─────────────┐  ┌──────────┐  ┌───────────┐              │   │
│  │  │ Compliance  │→│ Blocked  │→│ Auto-     │              │   │
│  │  │ Checker     │  │ Patterns │  │ Approve   │              │   │
│  │  │ (aws安規)    │  │ (危險命令) │  │ (安全命令)  │              │   │
│  │  └──────┬──────┘  └────┬─────┘  └─────┬─────┘              │   │
│  │         │ pass         │ pass         │ pass               │   │
│  │         ▼              ▼              ▼                     │   │
│  │  ┌─────────────┐  ┌──────────┐  ┌───────────┐              │   │
│  │  │ Rate Limit  │→│ Trust    │→│ Smart     │              │   │
│  │  │ (頻率限制)    │  │ Session  │  │ Approval  │              │   │
│  │  │ GSI 查詢     │  │ (信任期)  │  │ (Shadow)  │              │   │
│  │  └──────┬──────┘  └────┬─────┘  └─────┬─────┘              │   │
│  │         │ pass         │ pass         │ pending             │   │
│  │         ▼              ▼              ▼                     │   │
│  │                ┌────────────────────┐                       │   │
│  │                │  Telegram 審批請求   │                       │   │
│  │                │  [批准] [信任10分] [拒絕] │                    │   │
│  │                └────────────────────┘                       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    IAM 權限 (⚠️ 問題區)                      │   │
│  │                                                              │   │
│  │  ✅ DynamoDB CRUD (7 tables)                                 │   │
│  │  ✅ STS AssumeRole                                           │   │
│  │  ✅ Step Functions (deployer)                                │   │
│  │  ✅ SQS (DLQ)                                               │   │
│  │  ⚠️  Action: '*' (PowerUser)  ← P0-1 問題                   │   │
│  │  ⚠️  Deny list 不完整 (缺 iam:PassRole 等)                   │   │
│  │  ⚠️  arn:aws:iam::*:role/BouncerRole  ← P0-2 通配           │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  命令執行方式:                                                       │
│  ┌──────────────────────────────────────┐                          │
│  │ Default 帳號: 直接用 Lambda Role 跑    │ ← 這就是要 Action:* 的原因 │
│  │ Cross-Account: assume BouncerExecRole │ ← 安全，有隔離          │
│  └──────────────────────────────────────┘                          │
└─────────────────────────────────────────────────────────────────────┘
          │                              │
          │ DynamoDB                     │ STS AssumeRole
          ▼                              ▼
┌──────────────────────┐    ┌───────────────────────────────┐
│   DynamoDB Tables     │    │   Cross-Account 帳號           │
│                       │    │                               │
│ bouncer-prod-requests │    │  ┌─────────────────────────┐ │
│  └─ GSI: source-idx   │    │  │ Dev (992382394211)      │ │
│  └─ GSI: status-idx   │    │  │ BouncerExecutionRole ✅  │ │
│  └─ TTL ✅ / PITR ✅   │    │  └─────────────────────────┘ │
│                       │    │  ┌─────────────────────────┐ │
│ bouncer-prod-accounts │    │  │ 1st (841882238387)      │ │
│  └─ PITR ✅            │    │  │ BouncerExecutionRole ✅  │ │
│                       │    │  └─────────────────────────┘ │
│ bouncer-command-hist  │    │  ┌─────────────────────────┐ │
│  └─ TTL ✅ / PITR ✅   │    │  │ LT  (811246247192)      │ │
│                       │    │  │ BouncerExecutionRole ✅  │ │
│ bouncer-shadow-approvals│   │  └─────────────────────────┘ │
│  └─ TTL ✅             │    └───────────────────────────────┘
│                       │
│ ⚠️ 無 DeletionPolicy   │
│ ⚠️ 無 KMS CMK 加密      │
└──────────────────────┘

          │ states:StartExecution
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Deployer Stack (獨立 CFN)                         │
│                                                                     │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐       │
│  │ Step        │───▶│ CodeBuild    │───▶│ CloudFormation  │       │
│  │ Functions   │    │ sam-deployer │    │ (SAM Deploy)    │       │
│  │ Workflow    │    │ ARM64        │    │                 │       │
│  └──────┬──────┘    └──────┬───────┘    └────────┬────────┘       │
│         │                  │                     │                │
│    成功/失敗通知        ┌────┴────┐          CFN Exec Role        │
│    → Telegram         │ S3 下載  │       BounceDeployerCFNRole    │
│                       │ sam_    │       (手動建立, ⚠️ 未在 IaC)    │
│                       │ deploy  │                                │
│                       │ .py     │                                │
│                       └─────────┘                                │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │ DynamoDB: bouncer-projects / deploy-history / deploy-locks │     │
│  └─────────────────────────────────────────────────────────┘       │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │ S3: sam-deployer-artifacts-190825685292                    │     │
│  │  └─ deployer-scripts/sam_deploy.py                        │     │
│  │  └─ KMS 加密 ✅ / Versioning ✅ / Lifecycle ✅              │     │
│  └─────────────────────────────────────────────────────────┘       │
│                                                                     │
│  KMS: alias/bouncer-deployer (auto rotation ✅)                     │
│  Permission Boundary: SAMDeployerBoundary ✅                        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        監控 & 告警                                    │
│                                                                     │
│  CloudWatch Alarms:                                                 │
│  ✅ Lambda Error Rate (>5 in 5min)                                  │
│  ✅ API Gateway 5xx                                                  │
│  ✅ Lambda p99 Duration (>600s)                                      │
│  ⚠️ SNS Topic 無訂閱者 — 告警發了沒人收到                              │
│  ⚠️ DLQ 無深度告警                                                   │
│  ⚠️ 無 Custom Business Metrics                                      │
│                                                                     │
│  X-Ray: ✅ Lambda + Step Functions 全啟用                             │
│  Logging: ✅ JSON 結構化 + Audit Logging (decision_type/latency)     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         CI/CD                                        │
│                                                                     │
│  GitHub Actions:                                                    │
│  ✅ ruff (lint)                                                      │
│  ✅ bandit (security scan) — ⚠️ 只掃 src/                            │
│  ✅ cfn-lint — ⚠️ || true 靜默忽略                                    │
│  ✅ pytest (519 tests, 81% coverage)                                │
│  ⚠️ 無 coverage gate                                                │
│  ⚠️ 無 integration test                                             │
│  ⚠️ 依賴版本未固定                                                    │
│                                                                     │
│  部署:                                                               │
│  ⚠️ 無 AutoPublishAlias / DeploymentPreference                      │
│  ⚠️ 無 Canary/Blue-Green                                            │
│  ✅ Deployer 有審批 + 鎖 + 通知                                       │
└─────────────────────────────────────────────────────────────────────┘
```
