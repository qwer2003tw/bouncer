# Sprint 4: Task List

## Summary

| Task | Story | Priority | Parallelism | Files |
|------|-------|----------|-------------|-------|
| T1 | sec-006 | P0 | T1 ∥ T2 ∥ T3 | commands.py |
| T2 | sec-007 | P0 | T1 ∥ T2 ∥ T3 | mcp_presigned.py |
| T3 | sec-008 | P0 | T1 ∥ T2 ∥ T3 | grant.py |
| T4 | ops-001 + ops-003 | P0 | 獨立（一次 deploy） | template.yaml |

**預估總工時：** T1(2h) + T2(1h) + T3(1h) + T4(0.5h) = 並行後 ~2.5h

---

## [T1] [P0] [Story 1] Credential Isolation — commands.py

**ID:** bouncer-sec-006
**並行:** 可與 T2, T3 並行（不同檔案）
**預估:** 2h（含測試）

### Checklist

- [ ] 確認 Lambda 環境是否有 `aws` CLI binary（決定 subprocess vs botocore session 方案）
- [ ] 修改 `execute_command()` (L354-440)
  - 如有 `aws` binary → 用 `subprocess.run()` + isolated `env` dict
  - 如無 → 用 `botocore.session.Session` 注入 credentials 到 `create_clidriver()`
- [ ] 移除 `os.environ` 修改邏輯（L388-397, L423-430）
- [ ] 確保 Default account path（無 assume role）不受影響
- [ ] 保留 `AWS_PAGER=''` 設定（移到 subprocess env 或 session config）
- [ ] 補測試：concurrent execution with different accounts
  - 用 `threading.Thread` spawn 2 concurrent calls
  - Mock STS assume_role 回傳不同 credentials
  - 驗證各 thread 拿到正確 credentials
- [ ] 補測試：assume role 失敗時 env 不被修改
- [ ] 補測試：default account（no assume role）正常運作

### 驗收標準
- `execute_command()` 不再直接修改 `os.environ`
- 所有既有 `test_commands.py` / `test_bouncer.py` 測試通過
- 新增 concurrent test 通過

---

## [T2] [P0] [Story 2] Presigned URL Notification — mcp_presigned.py

**ID:** bouncer-sec-007
**並行:** 可與 T1, T3 並行（不同檔案）
**預估:** 1h（含測試）

### Checklist

- [ ] 在 `_generate_presigned_url()` 成功路徑末尾加 Telegram silent 通知
  - Format: `📎 Presigned URL 已生成 | source | file | expires | account`
  - 用 `try/except` 包裹，失敗不影響回傳
- [ ] 在 `_generate_presigned_batch_urls()` 成功路徑末尾加 Telegram silent 通知
  - Format: `📎 Presigned URL Batch 已生成 | source | {count} 個 | expires | account`
- [ ] **確認通知不包含 presigned URL 本身**（安全審查重點）
- [ ] Import `send_telegram_message_silent`（參考 notifications.py 現有 pattern）
- [ ] 補測試：成功 path → 通知被呼叫
- [ ] 補測試：失敗 path → 通知不被呼叫
- [ ] 補測試：通知內容不含 `X-Amz-Signature` 或 URL 格式字串

### 驗收標準
- 每次 presigned URL 生成 → Telegram 收到 silent 通知
- 失敗請求不觸發通知
- 通知中無 presigned URL

---

## [T3] [P0] [Story 3] ReDoS Prevention — grant.py

**ID:** bouncer-sec-008
**並行:** 可與 T1, T2 並行（不同檔案）
**預估:** 1h（含測試）

### Checklist

- [ ] `compile_pattern()` 開頭加前置驗證：
  - `len(pattern) > 200` → `ValueError("Pattern 長度超過上限（200 字元）")`
  - wildcard `*` 總數（排除 `{placeholder}` 內的）> 10 → `ValueError`
  - `***` 連續 3+ 個 star → `ValueError("Pattern 含有不合法的連續 wildcard")`
- [ ] `re.compile()` 呼叫包 `try/except re.error` → `ValueError("Pattern 編譯失敗: ...")`
- [ ] 補測試：正常 pattern compile + match 成功
- [ ] 補測試：pattern > 200 chars → ValueError
- [ ] 補測試：pattern 含 6+ 個獨立 `*` wildcard → ValueError
- [ ] 補測試：pattern 含 `****` → ValueError
- [ ] 補測試：合法 5-wildcard pattern match 1000 char string 在 100ms 內完成
- [ ] 補測試：invalid regex → ValueError with message

### 驗收標準
- 惡意 pattern 在 compile 階段被拒
- 既有合法 pattern 不受影響
- 所有既有 grant 相關測試通過

---

## [T4] [P0] [Story 4+5] Template Fix — template.yaml

**ID:** bouncer-ops-001 + bouncer-ops-003
**並行:** 獨立，可在 T1/T2/T3 前或後做
**預估:** 0.5h（含部署驗證）

### Checklist

- [ ] **ops-001:** `LambdaDurationAlarm.Properties.Threshold` 改 `600000` → `50000`
- [ ] **ops-003:** 加 `AlarmEmail` parameter（Type: String, Default: ""）
- [ ] **ops-003:** 加 `HasAlarmEmail` condition
- [ ] **ops-003:** 加 `AlarmEmailSubscription` resource（Condition: HasAlarmEmail）
- [ ] 驗證 template syntax: `sam validate`
- [ ] 部署時帶 `AlarmEmail` parameter
- [ ] 部署後確認：
  - CloudWatch alarm threshold = 50000
  - SNS subscription 存在（如提供了 email）
  - 既有 alarms 仍連接到同一 SNS topic

### 驗收標準
- `LambdaDurationAlarm.Threshold` = 50000
- `AlarmEmailSubscription` 在有 email 時建立
- 所有既有 alarm 不受影響

---

## 部署順序

```
Phase 1（並行開發）:
  T1: commands.py (credential isolation)
  T2: mcp_presigned.py (presigned notification)
  T3: grant.py (ReDoS prevention)

Phase 2（合併 + 測試）:
  整合 T1/T2/T3 → 跑完整測試 → 一次部署

Phase 3（template 修改）:
  T4: template.yaml → sam deploy
```

**或者更保守的順序：**
1. T3 (ReDoS) — 最小風險，先驗證
2. T2 (notification) — 低風險
3. T4 (template) — infra 變更
4. T1 (credential isolation) — 最大變更，最後

---

## 待確認事項（需 Steven 決定）

1. **sec-006 方案選擇：** subprocess (方案 A) vs botocore session (方案 B) — 取決於 Lambda 是否有 `aws` CLI binary
2. **ops-003 email 來源：** CloudFormation Parameter (方案 A) vs SSM Parameter Store (方案 B) — 建議方案 A
3. **ops-003 alarm email：** 要用哪個 email 地址？
