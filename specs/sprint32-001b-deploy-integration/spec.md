# Deploy Auto-Approve Integration

## Feature（一句話）
將 `changeset_analyzer` 接入 `mcp_tool_deploy` 流程：當 project 設定 `auto_approve_deploy=True` 且 changeset 分析為純 Lambda Code 變更時，跳過 Telegram 審批直接執行部署，並發靜默通知。

---

## User Stories

- **U1** — 作為 Agent，我希望對 `auto_approve_deploy=True` 的專案，只改 Lambda Code 時不需等待 Steven 審批，部署可在幾秒內自動開始。
- **U2** — 作為 Steven，我希望當 Bouncer 自動批准部署時，仍收到一則靜默通知，知道發生了什麼。
- **U3** — 作為 Steven，當 auto-approve 的 deploy 涉及基礎設施變更時，我希望收到正常的審批請求（附 changeset 摘要），不會被靜默跳過。
- **U4** — 作為管理員，我希望能透過 `add_project` / `update_project` 設定 `auto_approve_deploy` 欄位，預設為 `False`。
- **U5** — 作為 Steven，如果 changeset 分析失敗（API 錯誤、timeout），我希望系統 fail-safe 回退到人工審批，附錯誤摘要。

---

## Acceptance Scenarios（Given/When/Then）

### S1 — auto_approve_deploy=True + code-only → 直接部署
```
Given  project "bouncer" 設定 auto_approve_deploy=True
And    changeset 分析結果 is_code_only == True
When   mcp_tool_deploy() 被呼叫
Then   start_deploy() 被直接呼叫（不建立 pending_approval 記錄）
And    send_auto_approve_deploy_notification() 被呼叫
And   回傳 status="started" + deploy_id
```

### S2 — auto_approve_deploy=True + infra change → Telegram 審批 + changeset 摘要
```
Given  project "bouncer" 設定 auto_approve_deploy=True
And    changeset 分析結果 is_code_only == False（含 DynamoDB 變更）
When   mcp_tool_deploy() 被呼叫
Then   正常走 send_deploy_approval_request() 流程
And    Telegram 訊息附上 changeset 摘要（受影響的 resource 清單）
And   回傳 status="pending_approval"
```

### S3 — auto_approve_deploy=False（預設）→ 照舊審批
```
Given  project 未設定 auto_approve_deploy（或 = False）
When   mcp_tool_deploy() 被呼叫
Then   直接走 send_deploy_approval_request()，不呼叫 changeset_analyzer
```

### S4 — changeset 分析失敗 → fail-safe 人工審批
```
Given  project "bouncer" 設定 auto_approve_deploy=True
And    create_dry_run_changeset() 拋出 ClientError（e.g. IAM 不足）
When   mcp_tool_deploy() 被呼叫
Then   照舊走 send_deploy_approval_request()
And    Telegram 訊息附上錯誤摘要（"⚠️ Changeset 分析失敗：{reason}"）
And    cleanup_changeset() 仍被嘗試呼叫
```

### S5 — add_project 支援 auto_approve_deploy
```
Given  呼叫 add_project(project_id, config) 時 config 含 auto_approve_deploy=True
When   專案寫入 DynamoDB
Then   bouncer-projects table 中 auto_approve_deploy == True
```

### S6 — update_project 支援 auto_approve_deploy
```
Given  一個現有 project
When   update_project(project_id, {"auto_approve_deploy": True}) 被呼叫
Then   DynamoDB 中 auto_approve_deploy 欄位更新為 True
```

### S7 — send_auto_approve_deploy_notification 發送靜默 Telegram
```
Given  auto-approve 成功
When   send_auto_approve_deploy_notification() 被呼叫
Then   Telegram 收到一則靜默訊息（silent=True）
And    訊息包含：專案名、分支、deploy_id、source、reason
```

### S8 — template.yaml Lambda IAM 加 CFN changeset permissions
```
Given  Lambda Function 的 IAM policy
When   template.yaml 被部署
Then   Lambda 擁有以下 CFN Actions：
       - cloudformation:CreateChangeSet
       - cloudformation:DescribeChangeSet
       - cloudformation:DeleteChangeSet
And    Resource 限定為目標 stack ARN pattern（或 *，需 Constitution review）
```

---

## Edge Cases

| Case | 行為 |
|------|------|
| `stack_name` 為空（project 未設定） | 跳過 changeset 分析，直接走人工審批（warn log） |
| `auto_approve_deploy=True` 但 project 沒有 `stack_name` | Fail-safe → 審批，log warning |
| changeset 分析時間 > 60s | AnalysisResult.error populated → fail-safe |
| changeset 殘留（cleanup 失敗） | Log error 但繼續，不影響 deploy 流程 |
| 並行鎖衝突（deploy 進行中） | 既有邏輯不變，changeset 分析在鎖之前執行（或之後，視設計選擇） |
| `auto_approve_deploy` 欄位不存在 DDB（舊專案） | `project.get('auto_approve_deploy', False)` → False，向後相容 |

---

## Requirements

### Functional
- F1: `mcp_tool_deploy()` 新增分支：`auto_approve_deploy=True` 時執行 changeset 分析
- F2: code-only → `start_deploy()` 直接呼叫 + `send_auto_approve_deploy_notification()`
- F3: infra change / error → `send_deploy_approval_request()` 附 changeset 摘要
- F4: `add_project()` 支援 `auto_approve_deploy` 欄位（bool，預設 False）
- F5: `update_project()` 支援 patch `auto_approve_deploy` 欄位
- F6: `send_auto_approve_deploy_notification()` 靜默 Telegram 通知
- F7: `template.yaml` 加 CFN changeset IAM

### Non-functional
- NF1: `auto_approve_deploy=False` 的所有現有行為完全不變（向後相容）
- NF2: changeset 分析在並行鎖檢查之前執行（避免鎖住後分析失敗浪費時間）
- NF3: 分析失敗不拋例外，always fail-safe
- NF4: `send_auto_approve_deploy_notification()` 遵循現有 throttle pattern（NOTIFICATION_THROTTLE_SECONDS）
- NF5: changeset cleanup 一律在 try/except 包住，分析結果已確定後清理

---

## Interface Contract

### mcp_tool_deploy() 新增邏輯（偽碼）

```python
# 在 "建立審批請求" 之前插入：
auto_approve = project.get('auto_approve_deploy', False)
stack_name = project.get('stack_name', '')

if auto_approve and stack_name:
    changeset_name = None
    try:
        # template_url 從何而來？
        # Option A: 先跑一次 sam build，取得 S3 URL（需 SFN 協助）
        # Option B: 使用 deploy 時的 template S3 key（如已知）
        # → Sprint 32 採用 Option B（template_url 由 project config 或參數傳入）
        #   如果 template_url 無法取得 → fail-safe（warn + 走人工審批）
        template_url = _get_template_url(project, branch)  # 新增 helper
        if template_url:
            cfn = _get_cfn_client()
            changeset_name = create_dry_run_changeset(cfn, stack_name, template_url)
            analysis = analyze_changeset(cfn, stack_name, changeset_name)
        else:
            analysis = AnalysisResult(is_code_only=False, resource_changes=[],
                                      error="template_url unavailable")
    except Exception as e:
        analysis = AnalysisResult(is_code_only=False, resource_changes=[],
                                  error=str(e))
    finally:
        if changeset_name:
            try:
                cleanup_changeset(_get_cfn_client(), stack_name, changeset_name)
            except Exception:
                logger.exception("cleanup_changeset failed (non-fatal)")

    if is_code_only_change(analysis):
        # 直接部署
        result = start_deploy(project_id, deploy_branch, source or 'mcp', reason)
        send_auto_approve_deploy_notification(project, deploy_branch, result, source, reason)
        return mcp_result(req_id, {'content': [{'type': 'text', 'text': json.dumps({
            'status': result.get('status', 'started'),
            'deploy_id': result.get('deploy_id'),
            'auto_approved': True,
            'message': '自動批准：純 Lambda Code 變更，已直接部署'
        })}]})
    else:
        # 加上 changeset 摘要到 context
        changeset_summary = _format_changeset_summary(analysis)
        context = f"{context or ''}\n\n⚙️ Changeset 摘要：\n{changeset_summary}".strip()
        # fall through to normal approval flow

# （原有的 pending_approval 建立邏輯接著跑）
```

### send_auto_approve_deploy_notification() signature

```python
def send_auto_approve_deploy_notification(
    project: dict,
    branch: str,
    deploy_result: dict,
    source: str,
    reason: str,
) -> None:
    """靜默通知 Steven：auto-approve deploy 已啟動。"""
```

### add_project() / update_project() 變更

```python
# add_project：加入 auto_approve_deploy 欄位
item = {
    ...
    'auto_approve_deploy': bool(config.get('auto_approve_deploy', False)),
    ...
}

# update_project（新增函數 or 擴充現有 patch logic）：
# if 'auto_approve_deploy' in config:
#     update_expr += 'auto_approve_deploy = :aad'
#     values[':aad'] = bool(config['auto_approve_deploy'])
```

### template.yaml IAM 新增

```yaml
- Sid: ChangesetDryRunAccess
  Effect: Allow
  Action:
    - cloudformation:CreateChangeSet
    - cloudformation:DescribeChangeSet
    - cloudformation:DeleteChangeSet
  Resource: !Sub 'arn:aws:cloudformation:${AWS::Region}:*:stack/*/*'
  # Note: 跨帳號 stack（target_account）需另外 assume role 或 resource: '*'
  # 建議：先 '*'，後續 Sprint 收窄
```
