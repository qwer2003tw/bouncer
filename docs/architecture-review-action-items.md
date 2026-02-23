# Bouncer 架構審查 — Action Items (最終版)

> **審查者**: 4 位 sub-agent 專家
> **最後更新**: 2026-02-23

---

## 總結

| 狀態 | 數量 |
|------|------|
| ✅ 完成 | 20 |
| 🟡 P2 待做 | 10 |
| ❌ 取消 | 4 |

**P0 + P1 全部清完。**

---

## ✅ 已完成

| 內容 | 部署 |
|------|------|
| Lambda Role 瘦身（PowerUser → 最小權限） | deploy-13b1d85f65df |
| Cross-account ARN 通配 → 保留（風險趨近零） | 評估後接受 |
| DeletionPolicy → 降 P2 | Steven 決定 |
| AutoPublishAlias → 降 P2 | Steven 決定 |
| mcp_tool_execute 重構（340→22 行 pipeline） | deploy-d62ff203736a |
| API Gateway Usage Plan（10 req/s, burst 50） | deploy-9c1595722af4 |
| CORS AllowOrigin: * 移除 | deploy-9c1595722af4 |
| CI Coverage Gate（pytest-cov 80%） | deploy-ca59b45fa56f |
| CodeBuild PrivilegedMode → 保留 true | 評估後接受 |
| BounceDeployerCFNRole IaC 化 → 降 P2 | 評估後降級 |
| cfn-lint --non-zero-exit-code error | deploy-ca59b45fa56f |
| CI 版本固定 | deploy-ca59b45fa56f |
| Python 3.9 → 3.12 | deploy-ca59b45fa56f |
| CI cross-account 測試修復 | deploy-ca59b45fa56f |
| Trust 通知加來源 + 剩餘時間 | deploy-ca59b45fa56f |
| 死碼清理 -134 行 | c72d86f |
| MCP_TOOLS dict → tool_schema.py | deploy-90e8a5683709 |
| Magic numbers → constants.py | deploy-90e8a5683709 |
| callbacks approve/deny 去重 -45 行 | deploy-90e8a5683709 |
| deployer.py urllib → telegram.py | deploy-90e8a5683709 |
| risk_scorer rules → JSON config -266 行 | deploy-474360f7e17a |
| 部署鎖殘留 bug 修復 | deploy-296455b105d7 (deployer) |

---

## ❌ 取消

| 內容 | 理由 |
|------|------|
| Telegram Webhook 防重放 | 已有 status guard，MITM 不現實 |
| DynamoDB KMS CMK | AWS 預設加密夠用 |
| Telegram 單點故障 | 99.9%+ 可用率，ROI 低 |
| Secrets Manager 取代環境變數 | CFN 管理已加密，migration 風險 > 收益 |

---

## 🟡 P2 — 有空再做（10 項）

### 架構（5 項）
| 內容 | 工作量 |
|------|--------|
| DeletionPolicy: Retain（DynamoDB tables） | S |
| AutoPublishAlias + DeploymentPreference | M |
| Sync 長輪詢反模式（Lambda 840s timeout） | M |
| BounceDeployerCFNRole IaC 化 | M |
| Custom Business Metrics（CloudWatch EMF） | M |

### 程式碼（3 項）
| 內容 | 工作量 |
|------|--------|
| sys.path.insert hack → proper package | M |
| 循環依賴（mcp_tools ↔ app ↔ callbacks） | M |
| Type hints 統一 | M |

### CI/CD + 監控（2 項）
| 內容 | 工作量 |
|------|--------|
| bandit 掃描範圍擴大 | S |
| SNS Alarm + DLQ 告警 | S |
