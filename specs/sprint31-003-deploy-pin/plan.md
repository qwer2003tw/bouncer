# Implementation Plan: Deploy Pin → Notifier Progress Msg + Auto Unpin

## Technical Context
- 影響檔案：
  - `src/callbacks.py` — `handle_deploy_callback` (add pin call after update_message)
  - `deployer/notifier/app.py` — `handle_start`, `handle_progress`, `handle_success`, `handle_failure` (verify message_id flow + add thread support)
  - `deployer/template.yaml` — Notifier Lambda env vars (`MESSAGE_THREAD_ID`)
- 影響測試：
  - `tests/test_pin_unpin_deploy.py` (已存在 — extend)
  - 新增：`tests/test_sprint31_003_deploy_pin.py`
- 技術風險：
  - Notifier Lambda is a separate Lambda with its own deployment — changes need `sam deploy` for deployer stack
  - `pin_telegram_message` requires bot to have admin rights in the chat — document prerequisite
  - Group chat thread support: `sendMessage` must include `message_thread_id` — check if current Notifier app.py supports it

## Constitution Check
- 安全影響：無。pin/unpin 是 Telegram 視覺操作，不影響授權
- 成本影響：微量（每次 deploy 多 2 個 Telegram API 呼叫 — pin + unpin）
- 架構影響：中。Notifier Lambda 需要重新部署；需確認 template.yaml env var 設定

## Implementation Phases

### Phase 1: Pin in handle_deploy_callback (src/callbacks.py)
- After the `update_message("🚀 部署已啟動...")` call (line ~732), add:
```python
# Pin progress message (best-effort)
try:
    from telegram import pin_message
    pin_message(message_id, disable_notification=True)
    logger.info("deploy approval message pinned", 
                extra={"module": "deploy_callback", "deploy_id": deploy_id, "message_id": message_id})
except Exception as _pin_err:
    logger.warning("pin_message failed (ignored)", 
                   extra={"module": "deploy_callback", "error": str(_pin_err)})
```

### Phase 2: Verify Notifier Lambda message_id flow (deployer/notifier/app.py)
- `handle_start`: sends new message, stores `telegram_message_id` in history — BUT `callbacks.py` already stores the approval `message_id` in deploy record
  - Decision: Notifier should update the SAME approval message stored by callbacks.py, not create a new one
  - Change `handle_start`: check if `telegram_message_id` exists in history → update existing message; else send new
- `handle_progress`, `handle_success`, `handle_failure`: already read from history — verify they use the right key
- Add `message_thread_id` support in `send_telegram_message` and `update_telegram_message`

### Phase 3: template.yaml env vars (deployer/template.yaml)
- Add `MESSAGE_THREAD_ID` env var to Notifier Lambda if not present
- Verify `TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN` are set

### Phase 4: Tests
- Extend `tests/test_pin_unpin_deploy.py` with:
  - test_pin_called_after_deploy_approval_message_update
  - test_pin_failure_does_not_abort_deploy
- New `tests/test_sprint31_003_deploy_pin.py`:
  - test_notifier_handle_start_updates_existing_message
  - test_notifier_handle_success_unpins_message
  - test_notifier_handle_failure_unpins_message
