# Bouncer 全面審查報告
**日期**: 2026-02-21 | **審查者**: 5 專家 Sub-Agent | **版本**: commit 3c99bfe

---

## 評分總覽

| 面向 | 評分 | 說明 |
|------|------|------|
| IAM 安全 | 4/10 | `*:*` 權限 + deny 不完整 = 可繞過 |
| 程式碼品質 | 5/10 | 大量重複、函數過長、import 混亂 |
| 測試覆蓋 | 5/10 | 476 全過但 mock 無效 + 關鍵路徑未測 |
| 架構設計 | 7/10 | Serverless 設計良好、成本極低 (~$6/月) |
| 運維成熟度 | 2/5 | 告警形同虛設、無 runbook、secrets 無輪換 |

---

## 🚨 P0 — 必須修復 (14 項)

### IAM (5)
| # | 問題 | 風險 |
|---|------|------|
| I-1 | Lambda `*:*` PowerUser + deny 不完整 — 缺 `UpdateAssumeRolePolicy`, `PassRole`, `CreatePolicy`, `PutRolePermissionsBoundary` 等 | 權限提升 |
| I-2 | BouncerRole `DenySelfEscalation` 只保護自身 — 可建新 role 或改其他 role | 跨帳號提權 |
| I-3 | CodeBuild `iam:CreateRole` 對 `role/*` 無 PermissionBoundary 強制 | 可建 admin role |
| I-4 | `HighErrorAlarm` 沒有 `AlarmActions` — 觸發了不通知任何人 | 故障無感知 |
| I-5 | Lambda `sts:AssumeRole` 無 resource 限制 — 可 assume 帳號內任何 role | 橫向移動 |

### 測試 (5)
| # | 問題 | 風險 |
|---|------|------|
| T-1 | 8 處 `subprocess.run` mock 完全無效 — execute_command 用 awscli，不是 subprocess | 核心路徑未測 |
| T-2 | 重複 class 名稱 (`TestCommandClassification` ×2, `TestDeployerMore` ×2) — Python 覆蓋前者，測試靜默消失 | 測試缺失 |
| T-3 | Cross-account execute assume role 實際流程無測試 | 安全功能未驗證 |
| T-4 | Trust session 過期 + 命令數上限無測試 | 安全機制未驗證 |
| T-5 | sync 模式 (`sync=True`) 完全沒測 | 功能未驗證 |

### 程式碼 (2)
| # | 問題 | 風險 |
|---|------|------|
| C-1 | `deployer.py` 繞過 `telegram.py` 直接用 urllib 發訊息 — 沒有 parse_mode、沒有錯誤處理 | 通知格式不統一 |
| C-2 | `app.py` 重複定義 `get_header()` 覆蓋 `utils.py` import | Bug 只修一邊 |

### 安全 (2)
| # | 問題 | 風險 |
|---|------|------|
| S-1 | Hardcoded 帳號 ID/Telegram ID 在 public repo | 偵察資訊洩露 (已派 agent 清理中) |
| S-2 | Secrets 用 CFN NoEcho parameter — `describe-stacks` 可能洩露 | 憑證洩露 |

---

## 🟡 P1 — 重要 (18 項)

### IAM (3)
| # | 問題 |
|---|------|
| I-6 | CodeBuild `PrivilegedMode: true` (root + Docker) — 真的需要嗎？ |
| I-7 | Target account trust policy `bouncer-*` + `clawdbot-bouncer-*` 偏寬 |
| I-8 | 無 ExternalId 防 confused deputy |

### 架構 (3)
| # | 問題 |
|---|------|
| A-1 | Trust session 用 DynamoDB Scan 查詢 — 隨資料量線性退化 |
| A-2 | Telegram 是單點故障 (SPOF) — down 了所有審批卡住 |
| A-3 | 單 Lambda 承擔所有角色 (MCP + REST + Webhook + 執行) |

### 運維 (4)
| # | 問題 |
|---|------|
| O-1 | 缺 API Gateway 4xx/5xx 告警 |
| O-2 | 缺 DynamoDB throttle 告警 |
| O-3 | 日誌保留期未設定 — 永久保留吃費用 |
| O-4 | 主 Lambda 沒有 X-Ray tracing |

### 程式碼 (4)
| # | 問題 |
|---|------|
| C-3 | 所有模組都有 `try/except ImportError` 雙 import (~20 處) |
| C-4 | `_get_app_module()` / `_get_table()` 在 3 個檔案重複 |
| C-5 | DynamoDB `update_item` approve/deny 模式重複 8 次 |
| C-6 | `mcp_tool_execute()` ~150 行、`mcp_tool_upload()` ~160 行 — 需拆分 |

### 測試 (4)
| # | 問題 |
|---|------|
| T-6 | `sequence_analyzer.py` + `smart_approval.py` — 完整模組 0 測試 |
| T-7 | Deploy callback 成功路徑測試不完整 |
| T-8 | Compliance checker 在 execute flow 的整合無測試 |
| T-9 | 6270 行單一測試檔案 + 117 個 class — 建議拆成 12 個檔案 |

---

## 🟢 P2 — 建議改善 (12 項)

| # | 面向 | 問題 |
|---|------|------|
| P2-1 | 安全 | S3 upload bucket 缺 versioning/logging |
| P2-2 | 安全 | RoleName 無長度限制 (target-account) |
| P2-3 | 運維 | 無 CloudWatch Dashboard |
| P2-4 | 運維 | 無 Runbook 文件 |
| P2-5 | 運維 | GitHub PAT 過期無管理 |
| P2-6 | 運維 | cfn-lint 用 `|| true` 不擋 merge |
| P2-7 | 架構 | 無 staging 環境 |
| P2-8 | 架構 | Lambda 無 rollback 策略 (DeploymentPreference) |
| P2-9 | 架構 | 無 Dead Letter Queue |
| P2-10 | 程式碼 | Magic numbers 散落 (截斷長度 1000/800、TTL 3600 等) |
| P2-11 | 程式碼 | MCP 錯誤回應格式不統一 (mcp_error vs mcp_result+isError) |
| P2-12 | 程式碼 | 死代碼 `DEFAULT_UPLOAD_BUCKET` 定義了但沒用 |

---

## 架構優點 ✅

- **成本極低** ~$6/月 (100 cmd/天)
- **ARM64 Lambda + DynamoDB PAY_PER_REQUEST** — 最佳成本效率
- **DynamoDB PITR + TTL** — 資料保護和自動清理都有
- **異步設計** — 避開 API Gateway 29s timeout
- **帳號管理 DynamoDB 化** — 擴展不需改 code
- **Step Functions 部署編排** — 可靠的狀態管理

---

## 建議修復順序

### Phase 1: 安全 (1-2 天)
1. 清理 hardcoded 敏感資訊 ← **進行中**
2. 修 Lambda IAM deny list (I-1)
3. 修 BouncerRole DenySelfEscalation scope (I-2)
4. CodeBuild 加 PermissionBoundary condition (I-3)
5. 修 HighErrorAlarm 加 AlarmActions (I-4)

### Phase 2: 測試 (2-3 天)
6. 修 subprocess mock → awscli mock (T-1)
7. 修重複 class 名稱 (T-2)
8. 補 cross-account / trust session / sync 測試 (T-3~5)
9. 拆測試檔案 (T-9)

### Phase 3: 程式碼品質 (1-2 天)
10. deployer.py 改用 telegram.py (C-1)
11. 統一 import 機制 (C-3)
12. 抽出共用 helper (C-4, C-5)
13. 拆分長函數 (C-6)

### Phase 4: 運維 (1 天)
14. 加告警 + SNS 通知 (O-1, O-2)
15. 設日誌保留期 (O-3)
16. 加 X-Ray (O-4)
17. 建 Runbook (P2-4)

---

*完整審查報告由 5 個專家 sub-agent 獨立產出後整合。*
