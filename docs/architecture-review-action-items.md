# Bouncer 架構審查 — Action Items

> **日期**: 2026-02-22
> **審查者**: 4 位 sub-agent 專家（安全架構師、Serverless 架構師、程式碼品質專家、DevOps 專家）
> **專案**: Bouncer (AWS CLI 審批系統)

---

## 總評分

| 面向 | 評分 | 專家 |
|------|------|------|
| 安全架構 | 6/10 | 安全架構師 |
| Well-Architected | 7/10 | Serverless 架構師 |
| 程式碼品質 | 7/10 | 程式碼品質專家 |
| DevOps | 6/10 | DevOps 專家 |
| **綜合** | **6.5/10** | — |

---

## 🔴 P0 — Critical (必須修)

### P0-1: Lambda Role 過度授權 (PowerUser `Action: '*'`)
- **來源**: 安全、DevOps
- **位置**: `template.yaml` L195-197 `PowerUserAccess` Statement
- **問題**: Lambda Execution Role 有 `Action: '*', Resource: '*'`，靠 Deny list 排除 IAM 操作。Deny list 永遠不夠完整（沒 deny `iam:PassRole`、`iam:TagRole`、`sts:*` 等）
- **風險**: Lambda 被入侵 → 攻擊者可做幾乎任何操作
- **修復**: 改用 Allow-list。Lambda 本身只需 DynamoDB CRUD、STS AssumeRole、States、SQS（DLQ）、Logs。AWS CLI 命令的權限透過 Assume Role 取得
- **工作量**: M（需仔細列出所有需要的權限）

### P0-2: Cross-Account Assume Role 資源通配 `arn:aws:iam::*:role/BouncerRole`
- **來源**: 安全、DevOps
- **位置**: `template.yaml` L188
- **問題**: 允許 assume **任何** AWS 帳號的 BouncerRole
- **風險**: 攻擊者在自己帳號建 BouncerRole → 導向惡意帳號
- **修復**: 限定已知帳號清單 `arn:aws:iam::{account_id}:role/BouncerExecutionRole`
- **工作量**: S

### P0-3: 無 DeletionPolicy — Stack 刪除 = 資料全消
- **來源**: DevOps
- **位置**: `template.yaml` 所有 DynamoDB Table
- **問題**: DynamoDB table 沒設 `DeletionPolicy: Retain`，stack 意外刪除所有資料直接消失
- **風險**: 災難性資料遺失
- **修復**: 所有 DynamoDB table + SQS queue 加 `DeletionPolicy: Retain`
- **工作量**: S

### P0-4: 無 Rollback 機制 / 無 Canary Deploy
- **來源**: DevOps、Serverless
- **位置**: `template.yaml` Lambda 設定
- **問題**: 沒有 `AutoPublishAlias`、`DeploymentPreference`。部署失敗 Lambda 不會自動 rollback
- **風險**: 壞版本立即影響 100% 流量
- **修復**: 啟用 `AutoPublishAlias: live` + `DeploymentPreference: Type: AllAtOnce`（或 Canary10Percent5Minutes）
- **工作量**: M

### P0-5: `mcp_tool_execute` 函數 340 行，複雜度極高
- **來源**: 程式碼品質
- **位置**: `src/mcp_tools.py`
- **問題**: 單一函數包含合規檢查、阻擋名單、白名單、rate limiting、trust session、smart approval、DynamoDB 寫入、Telegram 通知。Cyclomatic complexity 20+
- **風險**: 極難維護和測試，改一個邏輯容易影響其他
- **修復**: 拆成 pipeline pattern — `check_compliance()` → `check_blocked()` → `check_safelist()` → `check_rate_limit()` → `check_trust()` → `submit_for_approval()`
- **工作量**: L

---

## 🟠 P1 — High (應該修)

### P1-1: API Gateway 無 WAF / 無 API 層認證
- **來源**: 安全、Serverless、DevOps
- **位置**: `template.yaml` L311-329
- **問題**: 無 WAF、無 Usage Plan、無 API Key。只靠 application 層 `X-Approval-Secret` header
- **風險**: DDoS 打穿 Lambda concurrent limit → 影響整個帳號
- **修復**: 加 WAF rate-based rule + API Gateway Usage Plan + Throttling
- **工作量**: M

### P1-2: Telegram Webhook 無防重放
- **來源**: 安全
- **位置**: `src/app.py` L337-342
- **問題**: 只檢查 `X-Telegram-Bot-Api-Secret-Token`，沒有 timestamp 驗證或 nonce 追蹤
- **風險**: Secret 洩漏 → 攻擊者重放舊 approve callback 自動批准命令
- **修復**: 加 Telegram Update ID 去重 + timestamp 驗證 + IP 白名單（Telegram server IPs）
- **工作量**: M

### P1-3: Lambda Timeout 900s + sync 長輪詢是反模式
- **來源**: Serverless
- **位置**: `template.yaml` Lambda Timeout + `src/app.py` `MCP_MAX_WAIT=840`
- **問題**: API Gateway 硬限 29 秒，sync 模式下 Lambda 跑 840 秒但 APIGW 早就 504 了，Lambda 空轉浪費錢
- **風險**: 資源浪費 + 佔用 Lambda 並發量
- **修復**: 移除 sync 長輪詢，全改 client-side polling（bouncer_status 已存在）。Lambda timeout 降到 30s
- **工作量**: M

### P1-4: CORS `AllowOrigin: '*'` 不必要
- **來源**: 安全、DevOps
- **位置**: `template.yaml` L322, `src/utils.py` L53
- **問題**: Server-to-server API 不需要 CORS
- **修復**: 移除 CORS 或限制為特定 origin
- **工作量**: S

### P1-5: `sys.path.insert(0, ...)` 散佈 11 個檔案
- **來源**: 程式碼品質
- **問題**: 沒有正確的 Python package 結構，每個模組都靠 sys.path hack import
- **修復**: 加 `__init__.py` + 用相對 import 或 proper packaging
- **工作量**: M（需同步更新所有 import 和測試）

### P1-6: 循環依賴 (`mcp_tools` ↔ `app` ↔ `callbacks`)
- **來源**: 程式碼品質
- **問題**: 需要 `_get_app_module()` 延遲 import 避免循環
- **修復**: 抽出共用 interface layer，打破循環依賴
- **工作量**: M

### P1-7: `sequence_analyzer.py` (60%) 和 `smart_approval.py` (63%) 測試覆蓋率不足
- **來源**: 程式碼品質
- **位置**: 869 行核心風險評分模組
- **修復**: 為 `analyze_sequence`、`extract_resource_ids`、`should_smart_approve` 補測試
- **工作量**: M

### P1-8: CI Coverage Gate 缺失
- **來源**: DevOps
- **位置**: `.github/workflows/`
- **問題**: 沒跑 pytest-cov，不知道覆蓋率，模組漏測不會發現
- **修復**: 加 `pytest-cov` + 設 coverage threshold（如 80%）
- **工作量**: S

### P1-9: CodeBuild PrivilegedMode: true
- **來源**: 安全
- **位置**: `deployer/template.yaml` L436
- **問題**: 如果 SAM build 不需要 Docker，特權模式不必要地增加風險
- **修復**: 評估是否真的需要 `--use-container`，不需要就關掉
- **工作量**: S

### P1-10: BounceDeployerCFNRole 未在 template 中定義
- **來源**: 安全
- **位置**: `deployer/template.yaml` L304, L577
- **問題**: 手動建立的 role，無法審查權限範圍
- **修復**: 將 CFN execution role 定義在 template 中 + 套 Permission Boundary
- **工作量**: M

### P1-11: Custom Business Metrics 缺失
- **來源**: Serverless
- **問題**: 沒有自訂指標（approval latency、trust session usage、rate limit hits、blocked count）
- **修復**: 用 CloudWatch EMF 在 Lambda 中發送自訂 metrics
- **工作量**: M

### P1-12: cfn-lint `|| true` 靜默忽略錯誤
- **來源**: DevOps
- **位置**: `.github/workflows/`
- **問題**: CFN 語法錯誤會被靜默忽略
- **修復**: 移除 `|| true`，讓 warning 只 warning 不 fail，error 要 fail
- **工作量**: S

### P1-13: CI 依賴版本未固定
- **來源**: DevOps
- **位置**: `.github/workflows/`
- **問題**: `pip install ruff/bandit/cfn-lint` 沒 pin 版本，未來可能突然壞
- **修復**: 改成 `ruff==0.x.x` 等固定版本
- **工作量**: S

---

## 🟡 P2 — Medium (有空再修)

### P2-1: DynamoDB 未用 KMS CMK 加密
- **來源**: 安全
- **位置**: `template.yaml` 所有 DynamoDB Table
- **問題**: 用預設 AWS-owned key，無法控制 key rotation 和存取
- **修復**: 加 `SSESpecification` 使用 KMS CMK
- **工作量**: M

### P2-2: SNS Alarm 無訂閱者
- **來源**: Serverless
- **位置**: `template.yaml` `AlarmNotificationTopic`
- **問題**: 告警觸發但沒人收到通知
- **修復**: 加 email/Telegram subscription
- **工作量**: S

### P2-3: DLQ 無深度告警
- **來源**: Serverless
- **位置**: `template.yaml` `ApprovalFunctionDLQ`
- **問題**: 訊息進 DLQ 不會被通知
- **修復**: 加 CloudWatch Alarm 監控 `ApproximateNumberOfMessagesVisible`
- **工作量**: S

### P2-4: Cold Start 較重 — 模組層級 import 12+ 個模組
- **來源**: Serverless
- **位置**: `src/app.py`
- **問題**: 每個模組各自初始化 boto3 DynamoDB resource
- **修復**: 合併 DynamoDB client 初始化到一處 + lazy import
- **工作量**: M

### P2-5: `mcp_tool_upload` 206 行巨型函數
- **來源**: 程式碼品質
- **位置**: `src/mcp_tools.py`
- **修復**: 同 P0-5，拆成小函數
- **工作量**: M

### P2-6: `callbacks.py` approve/deny 大量重複程式碼
- **來源**: 程式碼品質
- **位置**: `src/callbacks.py` L168
- **修復**: 抽取 `_update_request_status()` 共用函數
- **工作量**: S

### P2-7: `deployer.py` 繞過 `telegram.py` 直接用 urllib
- **來源**: 程式碼品質
- **位置**: `src/deployer.py`
- **修復**: 統一使用 `telegram.py` 模組
- **工作量**: S

### P2-8: `risk_scorer.py` `create_default_rules` 317 行
- **來源**: 程式碼品質
- **位置**: `src/risk_scorer.py`
- **修復**: 預設規則移到 JSON 配置檔
- **工作量**: S

### P2-9: Magic numbers 散佈多個檔案
- **來源**: 程式碼品質
- **問題**: `MCP_MAX_WAIT=840`、`ttl + 60`、`result[:1000]` 等
- **修復**: 統一到 `constants.py`
- **工作量**: S

### P2-10: Type hints 不一致
- **來源**: 程式碼品質
- **問題**: risk_scorer 有完整 hints，callbacks/app 幾乎沒有
- **修復**: 統一加 type hints，至少 public 函數
- **工作量**: M

### P2-11: Python 3.9 接近 EOL
- **來源**: DevOps
- **問題**: Python 3.9 已於 2025-10 EOL
- **修復**: 升級到 Python 3.12
- **工作量**: M（需測試所有依賴相容性）

### P2-12: Hardcoded table names (deployer 相關)
- **來源**: DevOps
- **位置**: 主 template 中 `bouncer-projects` 等
- **修復**: 用 `!Ref` 或 `!ImportValue`
- **工作量**: S

### P2-13: Telegram 單點故障
- **來源**: Serverless
- **問題**: Telegram API 不可用 → 審批流程卡死
- **修復**: 考慮備援通知管道或 fallback 機制
- **工作量**: L

### P2-14: `app.py` MCP_TOOLS 字典佔 ~300 行
- **來源**: 程式碼品質
- **位置**: `src/app.py`
- **修復**: Tool schema 抽到獨立 JSON 檔或模組
- **工作量**: S

### P2-15: bandit 掃描範圍不足
- **來源**: DevOps
- **問題**: 只掃 `src/`，沒掃 `mcp_server/`、`deployer/scripts/`
- **修復**: 擴大掃描範圍
- **工作量**: S

### P2-16: `commands.py` 只有 74% 覆蓋率
- **來源**: 程式碼品質
- **修復**: 補測試
- **工作量**: S

### P2-17: 環境變數明文傳遞 Secrets
- **來源**: 安全
- **位置**: `template.yaml` L165-167
- **問題**: `TELEGRAM_BOT_TOKEN` 和 `REQUEST_SECRET` 用環境變數傳入
- **修復**: 改用 Secrets Manager + Lambda 啟動時讀取
- **工作量**: M

---

## ✅ 做得好的地方

1. **多層防禦架構** — Compliance → Blocked → Safelist → Rate Limit → Trust → Smart Approval → Manual
2. **Fail-closed 安全設計** — 任何解析/評分失敗都 fallback 到人工審批
3. **96% Docstring 覆蓋率** — 幾乎每個函數都有清楚的 docstring
4. **Risk Scorer 設計優秀** — 純函數、依賴注入、完整 dataclass、規則可配置
5. **519 個測試、81% 整體覆蓋率** — 在 Lambda 專案中算優秀
6. **ARM64 架構** — Lambda + CodeBuild 都用 ARM64，省 20%
7. **DynamoDB 最佳實踐** — PAY_PER_REQUEST + PITR + TTL
8. **部署鎖** — DynamoDB conditional write 防並發部署
9. **X-Ray Tracing** — 全面啟用
10. **Permission Boundary** — deployer 有 SAMDeployerBoundary

---

## 建議 Sprint 規劃

### Sprint 1: 安全加固 (2-3 天)
P0-1, P0-2, P0-3, P1-4, P1-12, P1-13

### Sprint 2: 部署改善 (1-2 天)
P0-4, P1-9, P1-10, P1-8

### Sprint 3: API 安全 + 監控 (2-3 天)
P1-1, P1-2, P1-11, P2-2, P2-3

### Sprint 4: 程式碼重構 (3-5 天)
P0-5, P1-5, P1-6, P2-5, P2-6

### Sprint 5: 清理 + 升級 (2-3 天)
P1-3, P2-4, P2-11, P2-9, P2-10
