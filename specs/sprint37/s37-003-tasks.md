# s37-003 Tasks — feat: ZTP Files auto-approve

**Issue:** #125
**TCS 評分：** D1（純 DDB 配置，無 code 改動）

---

## TCS 評分說明

| 維度 | 分數 | 說明 |
|------|------|------|
| 複雜度 | D1 | 只更新 DDB item 1 個 boolean 欄位 |
| 風險 | 低 | Fail-safe 保護：`template_diff_analyzer` 出錯時自動降級到人工審批 |
| 影響範圍 | 小 | 只影響 `ztp-files` project 的 deploy flow |
| 測試需求 | 最小 | 整合 smoke test + 確認 DDB 欄位 |

---

## 實作步驟

### Step 1: 確認 ztp-files 現有 DDB config

透過 Bouncer 執行：
```bash
aws dynamodb get-item \
  --table-name bouncer-projects \
  --key '{"project_id": {"S": "ztp-files"}}' \
  --region us-east-1
```

確認並記錄：
- [ ] `git_repo` 欄位存在且為 `https://github.com/...` 格式
- [ ] `stack_name` 正確
- [ ] `default_branch` = `master`（或正確的分支名）

### Step 2: 更新 `auto_approve_deploy=true`

透過 Bouncer 執行：
```bash
aws dynamodb update-item \
  --table-name bouncer-projects \
  --key '{"project_id": {"S": "ztp-files"}}' \
  --update-expression "SET auto_approve_deploy = :v" \
  --expression-attribute-values '{":v": {"BOOL": true}}' \
  --region us-east-1
```

（若 Step 1 發現 `git_repo` 未設定，補充更新 `git_repo` 欄位）

### Step 3: 驗證寫入成功

透過 Bouncer 執行：
```bash
aws dynamodb get-item \
  --table-name bouncer-projects \
  --key '{"project_id": {"S": "ztp-files"}}' \
  --region us-east-1 \
  --query 'Item.auto_approve_deploy'
```

預期輸出：`{"BOOL": true}`

### Step 4: Smoke Test（擇機執行）

下次有 code-only commit 時：
1. 觸發 `bouncer_deploy ztp-files`
2. 確認 MCP 回應 `auto_approved: true`
3. 確認 Telegram 收到 auto-approve 靜默通知（無審批按鈕）
4. 確認部署成功完成

### Step 5: Regression Check

確認仍有 infra 變更保護：
- 手動在 template.yaml 加一行 IAM resource（不 commit，只用於人工測試邏輯）  
  或在 Bouncer test 環境模擬 `diff_result.is_safe=False` path

---

## 完成條件

- [ ] DDB `ztp-files.auto_approve_deploy = true` 已驗證
- [ ] Smoke test：code-only deploy auto-approve 成功
- [ ] 無 regression：infra 變更仍走人工審批
