"""Telegram notification functions for approval requests.

Extracted from app.py to break circular dependency:
  app.py → mcp_tools.py → app.py (for send_approval_request etc.)
Now: mcp_tools.py → notifications.py (no cycle)
"""

import os

import telegram as _telegram
from commands import is_dangerous
from constants import COMMAND_APPROVAL_TIMEOUT, TRUST_SESSION_MAX_COMMANDS


def _escape_markdown(text):
    return _telegram.escape_markdown(text)


def _send_message(text, keyboard=None):
    _telegram.send_telegram_message(text, keyboard)


def _send_message_silent(text, keyboard=None):
    _telegram.send_telegram_message_silent(text, keyboard)


def send_approval_request(request_id: str, command: str, reason: str, timeout: int = COMMAND_APPROVAL_TIMEOUT,
                          source: str = None, account_id: str = None, account_name: str = None,
                          assume_role: str = None, context: str = None):
    """發送 Telegram 審批請求"""
    cmd_preview = command if len(command) <= 500 else command[:500] + '...'
    cmd_preview = _escape_markdown(cmd_preview)
    reason = _escape_markdown(reason)
    source = _escape_markdown(source) if source else None

    dangerous = is_dangerous(command)

    if timeout < 60:
        timeout_str = f"{timeout} 秒"
    elif timeout < 3600:
        timeout_str = f"{timeout // 60} 分鐘"
    else:
        timeout_str = f"{timeout // 3600} 小時"

    source_line = f"🤖 *來源：* {source}\n" if source else ""
    context_line = f"📝 *任務：* {_escape_markdown(context)}\n" if context else ""

    if account_id and account_name:
        account_line = f"🏢 *帳號：* `{account_id}` ({account_name})\n"
    elif assume_role:
        try:
            parsed_account_id = assume_role.split(':')[4]
            role_name = assume_role.split('/')[-1]
            account_line = f"🏢 *帳號：* `{parsed_account_id}` ({role_name})\n"
        except Exception as e:
            print(f"Error: {e}")
            account_line = f"🏢 *Role：* `{assume_role}`\n"
    else:
        default_account = os.environ.get('AWS_ACCOUNT_ID', '')
        account_line = f"🏢 *帳號：* `{default_account}` (預設)\n"

    if dangerous:
        text = (
            f"⚠️ *高危操作請求* ⚠️\n\n"
            f"{source_line}"
            f"{context_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"⚠️ *此操作可能不可逆，請仔細確認！*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"⏰ *{timeout_str}後過期*"
        )
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '⚠️ 確認執行', 'callback_data': f'approve:{request_id}'},
                    {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
                ]
            ]
        }
    else:
        text = (
            f"🔐 *AWS 執行請求*\n\n"
            f"{source_line}"
            f"{context_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"⏰ *{timeout_str}後過期*"
        )
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '✅ 批准', 'callback_data': f'approve:{request_id}'},
                    {'text': '🔓 信任10分鐘', 'callback_data': f'approve_trust:{request_id}'},
                    {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
                ]
            ]
        }

    _send_message(text, keyboard)


def send_account_approval_request(request_id: str, action: str, account_id: str, name: str, role_arn: str, source: str, context: str = None):
    """發送帳號管理的 Telegram 審批請求"""
    name = _escape_markdown(name) if name else name
    source = _escape_markdown(source) if source else None
    source_line = f"🤖 *來源：* {source}\n" if source else ""
    context_line = f"📝 *任務：* {_escape_markdown(context)}\n" if context else ""

    if action == 'add':
        text = (
            f"🔐 *新增 AWS 帳號請求*\n\n"
            f"{source_line}"
            f"{context_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {name}\n"
            f"🔗 *Role：* `{role_arn}`\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"⏰ *5 分鐘後過期*"
        )
    else:
        text = (
            f"🔐 *移除 AWS 帳號請求*\n\n"
            f"{source_line}"
            f"{context_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {name}\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"⏰ *5 分鐘後過期*"
        )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }

    _send_message(text, keyboard)


def send_trust_auto_approve_notification(command: str, trust_id: str, remaining: str, count: int,
                                         result: str = None, source: str = None):
    """發送 Trust Session 自動批准的靜默通知"""
    cmd_preview = command if len(command) <= 100 else command[:100] + '...'
    cmd_preview = _escape_markdown(cmd_preview)

    result_preview = ""
    if result:
        if result.startswith('❌') or 'error' in result.lower()[:100]:
            result_status = "❌"
        else:
            result_status = "✅"
        result_text = result[:200] + '...' if len(result) > 200 else result
        result_text = _escape_markdown(result_text)
        result_preview = f"\n{result_status} `{result_text}`"

    source_line = f"🤖 `{_escape_markdown(source)}` · " if source else ""
    remaining_line = f"⏱ {remaining}" if remaining else ""
    session_info = f"{source_line}{remaining_line}".strip()
    session_line = f"\n{session_info}" if session_info else ""

    text = (
        f"🔓 *自動批准* \\(信任中\\)\n"
        f"📋 `{cmd_preview}`\n"
        f"📊 {count}/{TRUST_SESSION_MAX_COMMANDS}"
        f"{session_line}"
        f"{result_preview}"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '🛑 結束信任', 'callback_data': f'revoke_trust:{trust_id}'}
        ]]
    }

    _send_message_silent(text, keyboard)


# ============================================================================
# Grant Session Notifications
# ============================================================================

def send_grant_request_notification(
    grant_id: str,
    commands_detail: list,
    reason: str,
    source: str,
    account_id: str,
    ttl_minutes: int,
    allow_repeat: bool = False,
) -> None:
    """發送 Grant Session 審批請求通知

    Args:
        grant_id: Grant ID
        commands_detail: 命令預檢結果清單
        reason: 申請原因
        source: 請求來源
        account_id: AWS 帳號 ID
        ttl_minutes: TTL（分鐘）
        allow_repeat: 是否允許重複
    """
    try:
        safe_source = _escape_markdown(source) if source else 'Unknown'
        safe_reason = _escape_markdown(reason) if reason else ''
        mode_str = '可重複' if allow_repeat else '一次性'

        # 分類統計
        grantable = [d for d in commands_detail if d.get('category') == 'grantable']
        requires_individual = [d for d in commands_detail if d.get('category') == 'requires_individual']
        blocked = [d for d in commands_detail if d.get('category') == 'blocked']

        # 組裝命令清單文字
        lines = []

        max_display = 10

        if grantable:
            lines.append(f"\n✅ *可授權 ({len(grantable)}):*")
            for i, d in enumerate(grantable[:max_display]):
                cmd_preview = d['command'][:80]
                lines.append(f" {i+1}\\. `{_escape_markdown(cmd_preview)}`")
            if len(grantable) > max_display:
                lines.append(f" \\.\\.\\.及其他 {len(grantable) - max_display} 個命令")

        if requires_individual:
            lines.append(f"\n⚠️ *需個別審批 ({len(requires_individual)}):*")
            offset = len(grantable)
            for i, d in enumerate(requires_individual[:max_display]):
                cmd_preview = d['command'][:80]
                lines.append(f" {offset+i+1}\\. `{_escape_markdown(cmd_preview)}`")
            if len(requires_individual) > max_display:
                lines.append(f" \\.\\.\\.及其他 {len(requires_individual) - max_display} 個命令")

        if blocked:
            lines.append(f"\n🚫 *已攔截 ({len(blocked)}):*")
            offset = len(grantable) + len(requires_individual)
            for i, d in enumerate(blocked[:max_display]):
                cmd_preview = d['command'][:80]
                block_reason = d.get('block_reason', '')
                lines.append(f" {offset+i+1}\\. `{_escape_markdown(cmd_preview)}`")
            if len(blocked) > max_display:
                lines.append(f" \\.\\.\\.及其他 {len(blocked) - max_display} 個命令")

        commands_text = '\n'.join(lines)

        text = (
            f"🔑 *批次權限申請*\n\n"
            f"🤖 *來源：* {safe_source}\n"
            f"💬 *原因：* {safe_reason}\n"
            f"🏦 *帳號：* `{account_id}`\n"
            f"⏱ *TTL：* {ttl_minutes} 分鐘 \\| 模式：{mode_str}\n"
            f"{commands_text}\n\n"
            f"🆔 *ID：* `{grant_id}`"
        )

        # 根據是否有 requires_individual 決定按鈕
        buttons = []
        if grantable or requires_individual:
            buttons.append([
                {'text': '✅ 全部批准', 'callback_data': f'grant_approve_all:{grant_id}'},
            ])
            if grantable and requires_individual:
                buttons[0].append(
                    {'text': '✅ 只批准安全的', 'callback_data': f'grant_approve_safe:{grant_id}'},
                )
        buttons.append([
            {'text': '❌ 拒絕', 'callback_data': f'grant_deny:{grant_id}'},
        ])

        keyboard = {'inline_keyboard': buttons}
        _send_message(text, keyboard)

    except Exception as e:
        print(f"[GRANT] send_grant_request_notification error: {e}")


def send_grant_execute_notification(
    command: str,
    grant_id: str,
    result: str,
    remaining_info: str,
) -> None:
    """發送 Grant Session 命令自動執行的靜默通知

    Args:
        command: 執行的命令
        grant_id: Grant ID
        result: 執行結果
        remaining_info: 剩餘資訊（如 "1/3 命令, 25:13"）
    """
    try:
        cmd_preview = command[:100] + '...' if len(command) > 100 else command
        cmd_preview = _escape_markdown(cmd_preview)

        if result and (result.startswith('❌') or 'error' in result.lower()[:100]):
            result_status = "❌"
        else:
            result_status = "✅"

        result_text = result[:200] + '...' if result and len(result) > 200 else (result or '')
        result_text = _escape_markdown(result_text)

        grant_short = grant_id[:20] + '...' if len(grant_id) > 20 else grant_id

        text = (
            f"🔑 *Grant 自動執行*\n"
            f"📋 `{cmd_preview}`\n"
            f"{result_status} `{result_text}`\n"
            f"📊 剩餘: {_escape_markdown(remaining_info)}\n"
            f"🆔 `{grant_short}`"
        )

        keyboard = {
            'inline_keyboard': [[
                {'text': '🛑 撤銷 Grant', 'callback_data': f'grant_revoke:{grant_id}'}
            ]]
        }

        _send_message_silent(text, keyboard)

    except Exception as e:
        print(f"[GRANT] send_grant_execute_notification error: {e}")


def send_grant_complete_notification(grant_id: str, reason: str) -> None:
    """發送 Grant Session 完成/過期通知

    Args:
        grant_id: Grant ID
        reason: 完成原因（如 "全部使用完畢"、"TTL 到期"）
    """
    try:
        safe_reason = _escape_markdown(reason) if reason else ''
        grant_short = grant_id[:20] + '...' if len(grant_id) > 20 else grant_id

        text = (
            f"🔑 *Grant 已結束*\n\n"
            f"🆔 `{grant_short}`\n"
            f"💬 *原因：* {safe_reason}"
        )

        _send_message_silent(text)

    except Exception as e:
        print(f"[GRANT] send_grant_complete_notification error: {e}")
