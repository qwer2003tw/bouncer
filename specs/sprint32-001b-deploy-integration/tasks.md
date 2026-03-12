# Tasks — Sprint 32-001b Deploy Auto-Approve Integration

## TCS Scoring Guide
D1 Files (0-5) + D2 Cross-module (0-5) + D3 Testing (0-5) + D4 Infra (0-5) + D5 External (0-5)
Simple: 0-6 | Medium: 7-12 | Complex: 13+ (must split)

---

## 前置依賴
⚠️ 001b 所有 tasks 需要 001a 全部完成後才能開始。

---

## Tasks

[T001] [P1] 新增 `update_project_config(project_id, updates)` 到 deployer.py
| TCS=6 (Simple)
| D1=1（改 deployer.py）D2=1（DynamoDB update expression）D3=2（需測試 patch 行為）D4=1（DDB schema 變更）D5=1（UpdateItem expression syntax）
| Notes: `update_project` 目前不存在，需新增。支援 patch 任意欄位；`auto_approve_deploy` + `template_s3_url` 兩個新欄位
| Deliverable: `update_project_config()` 函數 + `add_project()` 加入 `auto_approve_deploy`, `template_s3_url` 欄位

[T002] [P1] 修改 `mcp_tool_deploy()` — 插入 auto-approve changeset 分析分支
| TCS=10 (Medium)
| D1=1（改 deployer.py）D2=3（import changeset_analyzer，呼叫 start_deploy，整合 notifications）D3=3（需 mock 多層呼叫）D4=0 D5=3（CFN API，start_deploy 回傳格式確認）
| Notes:
|   - 插入位置：step 4（鎖）之後、step 5（pending_approval）之前
|   - auto_approve_deploy=False → 完全不進新分支（向後相容保證）
|   - template_url 從 project.get('template_s3_url', '') 讀取；空值 → fail-safe
|   - 分析後 cleanup_changeset 一律在 finally 執行
|   - code-only → start_deploy() 直接呼叫，回傳 {status, deploy_id, auto_approved=True}
|   - infra change / error → append changeset_summary 到 context，走原有審批流程

[T003] [P1] 新增 `send_auto_approve_deploy_notification()` 到 notifications.py
| TCS=5 (Simple)
| D1=1（改 notifications.py）D2=1（import telegram_entities.MessageBuilder）D3=2 D4=0 D5=1（Telegram silent=True）
| Notes: 參考 send_trust_auto_approve_notification() pattern；silent=True；throttle 不適用（deploy 頻率低）
| Deliverable: 函數實作 + 在 mcp_tool_deploy 的 auto-approve 路徑中呼叫

[T004] [P1] 修改 `template.yaml` — Lambda IAM 加 CFN changeset permissions
| TCS=5 (Simple)
| D1=1（改 template.yaml）D2=0 D3=0 D4=3（IAM policy，需 deploy 生效）D5=1（CFN IAM resource scope）
| Notes:
|   - 新增 Sid: ChangesetDryRunAccess
|   - Actions: cloudformation:CreateChangeSet, DescribeChangeSet, DeleteChangeSet
|   - Resource: '*'（Sprint 33 再收窄，TODO comment 標記）
|   - 插入位置：現有 StepFunctionsAccess 之後（template.yaml line ~410 附近）

[T005] [P1] 新增 `tests/test_sprint32_deploy_auto_approve.py`（8+ test cases）
| TCS=8 (Medium)
| D1=1（1 新測試檔）D2=3（mock deployer, changeset_analyzer, notifications, telegram）D3=4（8+ cases）D4=0 D5=0
| Test cases:
|   TC01 - auto_approve=True + code-only → start_deploy called, status="started", auto_approved=True
|   TC02 - auto_approve=True + infra change → send_deploy_approval_request called, changeset_summary in context
|   TC03 - auto_approve=True + changeset error → send_deploy_approval_request (fail-safe), error in context
|   TC04 - auto_approve=False → changeset_analyzer NOT called, normal approval flow
|   TC05 - auto_approve=True + template_s3_url empty → fail-safe (no changeset, normal approval)
|   TC06 - add_project with auto_approve_deploy=True → DDB item has auto_approve_deploy=True
|   TC07 - update_project_config patches auto_approve_deploy → DDB updated correctly
|   TC08 - send_auto_approve_deploy_notification sends silent Telegram with deploy_id
|   TC09 - cleanup_changeset always called in finally (even if analysis fails) (bonus)
|   TC10 - auto_approve=True + code-only → send_auto_approve_deploy_notification called once (bonus)

---

## Summary

| Task | TCS | Complexity |
|------|-----|------------|
| T001 | 6   | Simple     |
| T002 | 10  | Medium     |
| T003 | 5   | Simple     |
| T004 | 5   | Simple     |
| T005 | 8   | Medium     |
| **Total** | **34** | **（5 tasks，2 Medium / 3 Simple）** |

---

## Open Questions for Sprint Kick-off

1. **template_s3_url 取得方式**：Sprint 32 採用「由 project config 讀取 template_s3_url」，但誰負責在 deploy 前更新這個欄位？是 agent 手動呼叫 update_project_config？還是需要修改 SAM deployer workflow 寫入？→ 建議 Sprint 32 採用手動設定，Sprint 33 自動化。

2. **IAM Resource scope**：`cloudformation:CreateChangeSet` Resource: `*` 是否會被 palisade 規則掃到？→ 先用 `*`，Sprint 33 收窄。

3. **start_deploy() 在 mcp_tool_deploy 中呼叫**：`start_deploy()` 目前在 webhook callback 路徑呼叫（用戶按 Telegram 批准後）。從 `mcp_tool_deploy` 直接呼叫功能上應該相同，但需確認 `triggered_by` 參數語義（本期建議用 `source or 'auto-approve'`）。

4. **changeset 分析位置（鎖之前 or 之後）**：spec 建議在鎖之後插入，避免分析成功但鎖無法取得。但如果分析耗時 30 秒，鎖已被另一個請求搶走，用戶要再等一次。→ 建議先 check 鎖（不取），再分析，分析完再取鎖。（Sprint 32 可簡單處理：鎖之後）

---

## Dependencies
- 001a 全部完成（changeset_analyzer.py 可 import）
- 不依賴其他 Sprint 32 tasks
