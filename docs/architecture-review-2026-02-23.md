# Bouncer 架構審查整合報告 (2026-02-23)

> 3 位專家（架構、安全、程式碼品質）獨立審查後整合，已過濾不實用或 over-engineering 的建議。

## 評分

| 維度 | 分數 | 說明 |
|------|------|------|
| 架構健康度 | 6.5/10 | 循環依賴 + God Module 是主要問題 |
| 安全性 | 7.5/10 | 無 Critical 漏洞，IAM 太寬是最大風險 |
| 程式碼品質 | 7.5/10 | 測試覆蓋好，但巨型函數多 |

---

## 值得做的改進（過濾後）

### P1 — 應該修

#### 1. 🐛 `smart_approval.py` 有 Bug — 序列分析永遠不生效
`get_sequence_risk_modifier()` 返回 `Tuple[float, str]`，但呼叫方用 `.get()` 當 dict。
tuple 沒有 `.get()`，被 except 吞掉 → 序列分析功能 = 死的。
**修復**: 1 行改動。

#### 2. 🔒 `secretsmanager get-secret-value` 不應該 auto-approve
任何有 API access 的 agent 可以無審批讀取所有 secrets。
**修復**: 從 `AUTO_APPROVE_PREFIXES` 移除。

#### 3. 🔒 Trust ID 用 MD5 前 8 字元 — collision space 太小
32-bit collision space，約 65536 次嘗試就能碰撞。
**修復**: 改用 SHA-256，取 16 字元（64-bit）。
**注意**: 會破壞現有 trust session（需要部署時清一下）。

#### 4. 📦 刪除 3 個 DEPRECATED wait_for_* 函數
`wait_for_upload_result`, `wait_for_result_mcp`, `wait_for_result_rest` — 全專案零呼叫者。
**修復**: 直接刪除，-95 行。

---

### P2 — 建議做（提升維護性）

#### 5. `mcp_tools.py` 拆分（1967 行 God Module）
最大的維護痛點。建議拆成：
- `mcp_execute.py` — execute pipeline
- `mcp_upload.py` — upload + batch upload pipeline
- `mcp_admin.py` — account/trust/help tools
- `mcp_tools.py` — thin dispatcher

#### 6. `app.py` handle_mcp_tool_call 的 22 個 elif → dict dispatch
```python
TOOL_HANDLERS = {'bouncer_execute': mcp_tool_execute, ...}
handler = TOOL_HANDLERS.get(tool_name)
```

#### 7. 重複邏輯抽出
- `_format_size_human()` 重複 6 次 → 統一用 mcp_tools 的那個
- `source_line/context_line/account_line` 模板重複 15+ 次 → 抽成 `_build_info_lines()` helper
- `handle_grant_approve_all` / `handle_grant_approve_safe` 合併成一個 + mode 參數

#### 8. Dead code 清理
- `_has_blocked_flag()` — 零呼叫者
- `_test_scoring()` / `_test_sequence_analyzer()` — 移到 tests/
- app.py 的 11 個 re-export — 確認哪些還需要

---

### 不做 / 低優先

以下是審查提出但我認為目前不值得做的：

| 建議 | 不做原因 |
|------|----------|
| IAM policy 改白名單 | 目前有應用層 blocklist + Deny statement，改白名單改動量巨大且會限制新 AWS API 探索能力 |
| HMAC 驗證 | 有 REQUEST_SECRET + API Gateway，風險已經很低 |
| Telegram timestamp 驗證 | DynamoDB 冪等性已防 replay |
| DynamoDB GSI 改 compound key | TTL 自動清理，hot partition 在目前流量下不成問題 |
| deployer.py lazy init | 只影響 cold start 幾十 ms |
| 所有 magic number → constant | 大部分已有常數，剩餘的不影響可讀性 |
| mcp_tool_trust_status scan → get_item | 使用頻率極低 |

---

## 建議執行順序

1. **P1 Bug fix**: smart_approval.py 序列分析 bug（5 分鐘）
2. **P1 Security**: 移除 secretsmanager auto-approve（2 分鐘）
3. **P1 Security**: Trust ID 改 SHA-256 + 16 字元（30 分鐘）
4. **P1 Cleanup**: 刪 deprecated functions（5 分鐘）
5. **P2**: mcp_tools.py 拆分（2-3 小時）
6. **P2**: dict dispatch + 重複邏輯清理（1 小時）
7. **P2**: dead code 清理（30 分鐘）
