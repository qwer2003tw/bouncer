# Bouncer 架構審查 — Action Items (2026-02-22 最終版)

> **審查者**: 4 位 sub-agent 專家（安全架構師、Serverless 架構師、程式碼品質專家、DevOps 專家）
> **最後更新**: 2026-02-22 18:00 UTC

---

## 總結

| 狀態 | 數量 | 說明 |
|------|------|------|
| ✅ 完成 | 13 | P0×5 + P1×7 + 死碼清理 |
| 🟡 P2 | 16 | 有空再做，無急迫性 |
| ❌ 取消 | 4 | 評估後認為不需要 |

**P0 + P1 全部清完。Bouncer 無待修項目。**

---

## ✅ 已完成

| ID | 內容 | Deploy/Commit |
|----|------|---------------|
| P0-1 | Lambda Role 瘦身（PowerUser → DynamoDB+STS+SFn+SQS only） | deploy-13b1d85f65df |
| P0-2 | Cross-account ARN 通配 → **保留**（Application 層白名單，風險趨近零） | 評估後接受 |
| P0-3 | DeletionPolicy → **降 P2**（使用量不大） | Steven 決定 |
| P0-4 | AutoPublishAlias → **降 P2**（使用量不大） | Steven 決定 |
| P0-5 | mcp_tool_execute 重構（340→22 行 + pipeline pattern） | deploy-d62ff203736a |
| P1-1 | API Gateway Usage Plan（10 req/s, burst 50） | deploy-9c1595722af4 |
| P1-4 | CORS AllowOrigin: * 移除 | deploy-9c1595722af4 |
| P1-8 | CI Coverage Gate（pytest-cov 80%） | deploy-ca59b45fa56f |
| P1-9 | CodeBuild PrivilegedMode → **保留 true**（ZTP Files 需要 Docker） | 評估後接受 |
| P1-10 | BounceDeployerCFNRole IaC 化 → **降 P2**（只影響 Default 帳號） | 評估後降級 |
| P1-12 | cfn-lint `--non-zero-exit-code error` | deploy-ca59b45fa56f |
| P1-13 | CI 版本固定（ruff/bandit/cfn-lint/pytest-cov） | deploy-ca59b45fa56f |
| — | Python 3.9 → 3.12（Lambda + CI + CodeBuild） | deploy-ca59b45fa56f |
| — | 4 個 cross-account CI 測試修復 | deploy-ca59b45fa56f |
| — | Trust 通知加來源 + 剩餘時間 | deploy-ca59b45fa56f |
| — | 死碼清理 -134 行（quick_score, is_safe, needs_approval, record_executed_command, should_smart_approve, generate_table_cloudformation） | c72d86f |

---

## ❌ 取消

| ID | 內容 | 理由 |
|----|------|------|
| P1-2 | Telegram Webhook 防重放 | 已有 status 檢查（pending_approval guard），MITM Telegram 不現實 |
| P2-1 | DynamoDB KMS CMK 加密 | AWS 預設 AWS-owned key 加密已足夠 |
| P2-13 | Telegram 單點故障 | Telegram 99.9%+ 可用率，備援管道 ROI 太低 |
| P2-17 | Secrets Manager 取代環境變數 | Lambda 環境變數由 CFN 管理已加密，migration 風險大於收益 |

---

## 🟡 P2 — 有空再做

### 架構
| ID | 內容 | 工作量 |
|----|------|--------|
| P0-3 | DeletionPolicy: Retain（DynamoDB tables） | S |
| P0-4 | AutoPublishAlias + DeploymentPreference | M |
| P1-3 | Sync 長輪詢反模式（Lambda 840s timeout 空轉） | M |
| P1-10 | BounceDeployerCFNRole IaC 化 | M |
| P1-11 | Custom Business Metrics（CloudWatch EMF） | M |

### 程式碼品質
| ID | 內容 | 工作量 |
|----|------|--------|
| P1-5 | sys.path.insert hack → proper package structure | M |
| P1-6 | 循環依賴（mcp_tools ↔ app ↔ callbacks） | M |
| P2-6 | callbacks.py approve/deny 重複程式碼 | S |
| P2-7 | deployer.py 繞過 telegram.py 直接用 urllib | S |
| P2-8 | risk_scorer create_default_rules 317 行 → JSON 配置 | S |
| P2-9 | Magic numbers → constants.py | S |
| P2-10 | Type hints 統一 | M |
| P2-14 | app.py MCP_TOOLS dict ~300 行 → 獨立模組 | S |

### CI/CD
| ID | 內容 | 工作量 |
|----|------|--------|
| P2-12 | Hardcoded table names（deployer 相關） | S |
| P2-15 | bandit 掃描範圍擴大（mcp_server/、deployer/scripts/） | S |

### 監控
| ID | 內容 | 工作量 |
|----|------|--------|
| P2-2 | SNS Alarm 無訂閱者 | S |
| P2-3 | DLQ 無深度告警 | S |
| P2-4 | Cold Start 優化（合併 DynamoDB client 初始化） | M |

---

## ✅ 做得好的地方

1. **多層防禦** — Compliance → Blocked → Safelist → Rate Limit → Trust → Smart Approval → Manual
2. **Fail-closed** — 任何解析/評分失敗 fallback 到人工審批
3. **Pipeline 重構** — mcp_tool_execute 22 行入口 + 8 pipeline 函數，清晰好維護
4. **519 測試 / 81% 覆蓋率** — Lambda 專案中算優秀
5. **ARM64** — Lambda + CodeBuild 省 20%
6. **DynamoDB 最佳實踐** — PAY_PER_REQUEST + PITR + TTL
7. **完整 CI** — ruff + bandit + cfn-lint + pytest-cov 80% + docs check
8. **Python 3.12** — 最新 LTS
