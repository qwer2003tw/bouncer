# Implementation Plan — 001b Deploy Auto-Approve Integration

## Technical Context

### 影響檔案

| 檔案 | 操作 | 風險 |
|------|------|------|
| `src/deployer.py` | **修改** `mcp_tool_deploy()`, `add_project()`, `update_project()` | 中：核心部署路徑，需嚴格不破壞 auto_approve_deploy=False 行為 |
| `src/notifications.py` | **新增** `send_auto_approve_deploy_notification()` | 低：只新增函數 |
| `template.yaml` | **修改** Lambda IAM policy | 中：IAM 變更需 deploy 生效；Resource scope 需 review |
| `tests/test_sprint32_deploy_auto_approve.py` | **新增** | 低 |

### 現有架構關鍵點

#### deployer.py
- `mcp_tool_deploy()` 從 line 854 開始，完整流程：
  1. 驗證參數
  2. 取得 project config
  3. preflight_check_secrets
  4. 並行鎖檢查
  5. **[插入點]** auto-approve changeset 分析
  6. 建立 pending_approval item，put_item
  7. send_deploy_approval_request()
  8. return pending_approval

- `add_project()` line 271：只有固定欄位清單，需手動加 `auto_approve_deploy`
- `update_project()` **不存在！** 需確認是否有 patch API（搜尋結果未見 update_project）

#### 關鍵問題：template_url 從哪來？
CFN CreateChangeSet 需要 `TemplateURL`（S3 URL）或 `TemplateBody`。

**選項分析：**

| 選項 | 可行性 | 備註 |
|------|--------|------|
| A. 用 `TemplateBody` 從 GitHub 下載原始 template.yaml | ✅ 可行 | 需 GitHub PAT；SAM template 需先 transform；**本期不採用（複雜度高）** |
| B. 用現有 stack 的 GetTemplate | ✅ 可行 | 拿 deployed template；但無法比較 *新* 版本的變更 → **不正確** |
| C. 讓 SAM deploy worker 先 build+package，上傳 S3，回傳 URL | ✅ 最正確 | 架構改動大，需修改 Step Functions | **未來版本** |
| D. 用 DescribeStacks 取 deployed template + 新版的 Parameters 做 changeset | ⚠️ 部分可行 | 只能比較 parameter 差異，不能比較 code | **不夠** |
| **E. 先觸發 sam package（CLI），取得 S3 packaged template URL** | ✅ **本期採用** | 需在 mcp_tool_deploy 觸發 build Lambda 或使用已有 S3 artifact |

**Sprint 32 決策（保守方案）：**
- `template_url` 從 project config 新增選填欄位 `template_s3_url` 或由呼叫方傳入
- 如果 `template_url` 無法取得（欄位為空）→ fail-safe，走人工審批，log warning
- 這讓 001b 可以完整測試 integration，template_url 解決方案可後續 Sprint 補完
- **或者**：額外選項 F — 在 001b 中只做 `template_s3_url` 從 project config 讀取，agent 使用前先 `update_project` 設定 URL

> ⚠️ **Spec 裁定**：Sprint 32 採用「template_url 由 project config 的 `template_s3_url` 欄位提供」。agent 在呼叫 `bouncer_deploy` 之前，先執行 `sam package` 並 update_project。若欄位不存在或為空 → fail-safe。

#### update_project() 是否存在？
```
grep -n "def update_project\|update_project" /home/ec2-user/projects/bouncer/src/deployer.py
```
→ 待確認。若不存在，T002 需新增此函數。

### 風險評估

| 風險 | 機率 | 緩解 |
|------|------|------|
| mcp_tool_deploy 分支邏輯破壞現有 auto_approve=False 行為 | 中 | 分支在 step 4（鎖之後）插入；`auto_approve=False` 完全不進新分支 |
| template_url 取得困難（Sprint 32 無 sam package step） | 高 | fail-safe：無 URL → 照舊人工審批 |
| IAM resource scope 太寬（`*`）被 palisade 掃到 | 中 | 加 Constitution note；後續 Sprint 收窄到 `arn:aws:cloudformation:...:stack/bouncer-*/*` |
| start_deploy() 在 mcp_tool_deploy() 中呼叫（現在只在 callback 路徑呼叫） | 中 | 確認 start_deploy() 無 callback-only 依賴；需追蹤 deploy_id 回傳路徑 |

---

## Constitution Check

### 安全
- ✅ `auto_approve_deploy` 預設 False，舊專案不受影響
- ✅ Changeset dry-run 不執行變更（CreateChangeSet without ExecuteChangeSet）
- ✅ Fail-safe：分析失敗 → 人工審批（不會更危險）
- ⚠️ IAM `cloudformation:CreateChangeSet` Resource: `*` → 建議 Sprint 33 收窄為 `arn:aws:cloudformation:${Region}:${Account}:stack/bouncer-*/*`
- ✅ `send_auto_approve_deploy_notification()` 讓 Steven 始終知道自動批准發生

### 成本
- 每次 auto-approve deploy 增加：1x CreateChangeSet + 1-N DescribeChangeSet + 1x DeleteChangeSet
- 估算：~3 API 呼叫，成本可忽略
- Lambda 執行時間增加：~5-10 秒（polling changeset），在 Lambda timeout 範圍內

### 架構
- ✅ 保持 mcp_tool_deploy 單一進入點，不分裂成兩個 tool
- ✅ changeset_analyzer 作為純模組 import（無全域狀態）
- ✅ start_deploy() 不需修改（已有完整部署邏輯）
- ⚠️ 需確認 start_deploy() 回傳格式與 mcp_tool_deploy 期望的 mcp_result 相容

---

## Implementation Phases

### Phase 2.1 — add_project / update_project 支援 auto_approve_deploy
**目標：** DynamoDB 欄位支援  
**影響：** `deployer.py:add_project()` + 新增 `update_project_config()`（若不存在）

```python
# add_project 新增欄位
'auto_approve_deploy': bool(config.get('auto_approve_deploy', False)),
'template_s3_url': config.get('template_s3_url', ''),  # 選填

# update_project_config（新函數）
def update_project_config(project_id: str, updates: dict) -> dict:
    """Patch 更新 project config（只更新傳入的 key）"""
```

### Phase 2.2 — mcp_tool_deploy() 插入 auto-approve 分支
**目標：** 在鎖之後、建立 pending_approval 之前，插入 changeset 分析邏輯  
**插入位置：** line ~918（`# 建立審批請求` 之前）

### Phase 2.3 — send_auto_approve_deploy_notification()
**目標：** 靜默 Telegram 通知  
**Pattern：** 參考 `send_trust_auto_approve_notification()`（line 328 in notifications.py）

```python
def send_auto_approve_deploy_notification(
    project: dict, branch: str, deploy_result: dict,
    source: str = None, reason: str = None,
) -> None:
    mb = MessageBuilder()
    mb.text("🚀 ").bold("自動批准部署").newline()
    mb.text(f"📦 {project.get('name', '')}").newline()
    mb.text(f"🌿 {branch}").newline()
    mb.text(f"🆔 {deploy_result.get('deploy_id', '')}").newline()
    if source:
        mb.text(f"🤖 {source}").newline()
    text, entities = mb.build()
    _telegram.send_message_with_entities(text, entities, silent=True)
```

### Phase 2.4 — template.yaml IAM
```yaml
- Sid: ChangesetDryRunAccess
  Effect: Allow
  Action:
    - cloudformation:CreateChangeSet
    - cloudformation:DescribeChangeSet
    - cloudformation:DeleteChangeSet
  Resource: '*'
  # TODO(sprint33): narrow to arn:aws:cloudformation:${Region}:*:stack/bouncer-*/*
```

### Phase 2.5 — 新增 tests/test_sprint32_deploy_auto_approve.py
8+ test cases covering S1-S8 scenarios。
