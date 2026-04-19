# s37-001 Tasks — fix: changeset count=0 不觸發人工確認

**Issue:** #124
**TCS 評分：** D2（小改動，邏輯清楚，風險低）

---

## TCS 評分說明

| 維度 | 分數 | 說明 |
|------|------|------|
| 複雜度 | D2 | 單一函數邏輯修改，條件分支清楚 |
| 風險 | 低 | Fail-safe 保留（仍進人工確認），只改通知文字 |
| 影響範圍 | 小 | 只改 `deployer/notifier/app.py` 1 個函數 |
| 測試需求 | 簡單 | Unit test mock telegram + event |

---

## 實作步驟

### Step 1: 修改 `handle_infra_approval_request()`

**File:** `deployer/notifier/app.py`

1. 在 `change_count = event.get('change_count', 0)` 下方加：
   ```python
   analysis_error = event.get('analysis_error', None)
   ```

2. 將現有通知文字中的 `f"🔧 *變更數量：* {change_count}\n\n⚡ 偵測到 infra 變更，需要人工確認才能繼續部署。"` 替換為三路分支：
   ```python
   if analysis_error and change_count == 0:
       change_info = (
           f"⚠️ *Changeset 分析失敗*\n"
           f"📋 *原因：* `{str(analysis_error)[:200]}`\n\n"
           f"無法確認變更範圍，請人工審查。"
       )
   elif change_count == 0:
       change_info = "⚠️ *分析結果異常：0 個變更卻未 auto-approve，請人工確認。*"
   else:
       change_info = (
           f"🔧 *變更數量：* {change_count}\n\n"
           f"⚡ 偵測到 infra 變更，需要人工確認才能繼續部署。"
       )
   ```

3. 更新 `text` 字串，用 `{change_info}` 取代原本的 `f"🔧 *變更數量：* {change_count}\n\n⚡ 偵測到 infra 變更..."` 部分。

### Step 2: 新增 Unit Tests

**File:** `deployer/tests/test_notifier_analyze.py`

新增 class `TestHandleInfraApprovalRequest`，包含：
- `test_analysis_error_shows_error_msg`：驗證 analysis_error 非空時文字正確
- `test_zero_count_no_error_shows_anomaly`：驗證 count=0 且 error=None 時顯示異常說明  
- `test_normal_change_count_shows_count`：回歸測試，change_count=3 顯示「變更數量: 3」
- `test_analysis_error_truncated_at_200_chars`：長 error 訊息截斷驗證

Mock 策略：
- `patch('app.send_telegram_message')` → 捕捉通知文字
- `patch('app.get_history')` → 回傳 `{'branch': 'master'}`
- `patch('app.update_history')` → no-op

### Step 3: 跑測試確認

```bash
cd /home/ec2-user/projects/bouncer/deployer
python -m pytest tests/test_notifier_analyze.py -v
```

確認所有 new + existing tests 通過。

### Step 4: Code Review Checklist

- [ ] `analysis_error` 截斷到 200 字
- [ ] 原有 `change_count > 0` 邏輯不變
- [ ] `task_token` 儲存邏輯不受影響
- [ ] Telegram keyboard（批准/拒絕按鈕）不受影響
