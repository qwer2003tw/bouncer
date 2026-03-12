# Implementation Plan: Immediate Feedback After Deploy Approval

## Technical Context
- 影響檔案：
  - `src/callbacks.py` — `handle_deploy_callback` (line ~688), `handle_deploy_frontend_callback` (line ~1544)
- 影響測試：
  - `tests/test_deployer_main.py`
  - `tests/test_deploy_frontend_integration_s11.py`
  - 新增：`tests/test_sprint31_002_deploy_feedback.py`
- 技術風險：
  - `update_message` before `start_deploy` adds one Telegram API call per approval (~200-500ms) — acceptable
  - If Telegram has a temporary issue, the best-effort call may fail → exception must be caught to avoid aborting deploy
  - Double `update_message` is fine (second call overwrites first)

## Constitution Check
- 安全影響：無。不改變授權邏輯
- 成本影響：微量（每次 deploy 審批多一次 Telegram API 呼叫；不影響 Lambda cost significantly）
- 架構影響：無。純粹 UX timing fix

## Implementation Phases

### Phase 1: Fix handle_deploy_callback (callbacks.py)
```python
if action == 'approve':
    answer_callback(callback_id, '🚀 啟動部署中...')
    _update_request_status(table, request_id, 'approved', user_id)
    
    # [NEW] Immediate feedback — remove buttons before start_deploy
    try:
        update_message(
            message_id,
            f"🚀 *部署啟動中...*\n\n"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n"
            f"⏳ 正在啟動 Step Functions...",
            remove_buttons=True
        )
    except Exception as _imm_err:
        logger.warning("immediate feedback update_message failed (ignored)", 
                       extra={"module": "deploy_callback", "error": str(_imm_err)})
    
    # 啟動部署（existing code continues）
    result = start_deploy(project_id, branch, user_id, reason)
    ...
```

### Phase 2: Fix handle_deploy_frontend_callback (callbacks.py)
- Same pattern as Phase 1, applied to `handle_deploy_frontend_callback`

### Phase 3: Tests
- `tests/test_sprint31_002_deploy_feedback.py`:
  - test_deploy_approve_calls_update_message_before_start_deploy
  - test_deploy_approve_immediate_feedback_on_start_deploy_failure
  - test_deploy_approve_immediate_feedback_telegram_error_is_ignored
  - test_deploy_frontend_approve_immediate_feedback
