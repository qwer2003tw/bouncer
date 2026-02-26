"""Telegram notification functions for approval requests.

Extracted from app.py to break circular dependency:
  app.py → mcp_tools.py → app.py (for send_approval_request etc.)
Now: mcp_tools.py → notifications.py (no cycle)
"""

import os

import telegram as _telegram
from commands import is_dangerous
from constants import COMMAND_APPROVAL_TIMEOUT, TRUST_SESSION_MAX_COMMANDS
from utils import format_size_human, build_info_lines


def _escape_markdown(text):
    return _telegram.escape_markdown(text)


def _send_message(text, keyboard=None) -> dict:
    """Send a Telegram message and return the API response dict.

    Returns the raw API response (``{'ok': True, ...}`` on success, ``{}`` on
    any failure so callers can check ``result.get('ok')``).
    """
    return _telegram.send_telegram_message(text, keyboard)


def _send_message_silent(text, keyboard=None):
    _telegram.send_telegram_message_silent(text, keyboard)


def send_approval_request(request_id: str, command: str, reason: str, timeout: int = COMMAND_APPROVAL_TIMEOUT,
                          source: str = None, account_id: str = None, account_name: str = None,
                          assume_role: str = None, context: str = None,
                          template_scan_result: dict = None) -> bool:
    """發送 Telegram 審批請求

    Returns:
        True if the Telegram message was sent successfully, False otherwise.
    """
    cmd_preview = command if len(command) <= 500 else command[:500] + '...'
    # cmd_preview 放在 backtick code block 裡，不需要 escape
    # reason/source/context 由 build_info_lines 內部 escape，這裡不再手動 escape

    dangerous = is_dangerous(command)

    if timeout < 60:
        timeout_str = f"{timeout} 秒"
    elif timeout < 3600:
        timeout_str = f"{timeout // 60} 分鐘"
    else:
        timeout_str = f"{timeout // 3600} 小時"

    source_line = build_info_lines(source=source, context=context)

    if account_id and account_name:
        safe_account_name = _escape_markdown(account_name) if account_name else ''
        account_line = f"🏦 *帳號：* `{account_id}` ({safe_account_name})\n"
    elif assume_role:
        try:
            parsed_account_id = assume_role.split(':')[4]
            role_name = assume_role.split('/')[-1]
            account_line = f"🏦 *帳號：* `{parsed_account_id}` ({role_name})\n"
        except Exception as e:
            print(f"Error: {e}")
            account_line = f"🏦 *Role：* `{assume_role}`\n"
    else:
        default_account = os.environ.get('AWS_ACCOUNT_ID', '')
        account_line = f"🏦 *帳號：* `{default_account}` (預設)\n"

    safe_reason = _escape_markdown(reason)

    # Build optional template scan block (Phase 4)
    template_scan_block = ""
    if template_scan_result and template_scan_result.get('hit_count', 0) > 0:
        severity = template_scan_result.get('severity', 'unknown')
        hit_count = template_scan_result.get('hit_count', 0)
        max_score = template_scan_result.get('max_score', 0)
        escalate = template_scan_result.get('escalate', False)

        severity_emoji = {
            'critical': '🔴',
            'high': '🟠',
            'medium': '🟡',
            'low': '🟢',
        }.get(severity, '⚪')

        escalate_note = " ⚠️ *強制人工審批*" if escalate else ""
        template_scan_block = (
            f"\n🔍 *Template Scan：* {severity_emoji} {severity.upper()} "
            f"({hit_count} hits, score={max_score}){escalate_note}\n"
        )

        # Show first 3 factor details
        factors = template_scan_result.get('factors', [])
        for factor in factors[:3]:
            details = _escape_markdown(str(factor.get('details', '')))
            template_scan_block += f"  • `{details}`\n"
        if len(factors) > 3:
            template_scan_block += f"  _...及其他 {len(factors) - 3} 個風險_\n"

    if dangerous:
        text = (
            f"⚠️ *高危操作請求* ⚠️\n\n"
            f"{source_line}"
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {safe_reason}\n"
            f"{template_scan_block}"
            f"\n⚠️ *此操作可能不可逆，請仔細確認！*\n\n"
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
            f"{account_line}"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {safe_reason}\n"
            f"{template_scan_block}"
            f"\n🆔 *ID：* `{request_id}`\n"
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

    result = _send_message(text, keyboard)
    return bool(result and result.get('ok'))


def send_account_approval_request(request_id: str, action: str, account_id: str, name: str, role_arn: str, source: str, context: str = None):
    """發送帳號管理的 Telegram 審批請求"""
    # build_info_lines escapes internally; name is escaped manually below
    safe_name = _escape_markdown(name) if name else name
    source_line = build_info_lines(source=source, context=context)

    if action == 'add':
        text = (
            f"🔐 *新增 AWS 帳號請求*\n\n"
            f"{source_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {safe_name}\n"
            f"🔗 *Role：* `{role_arn}`\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"⏰ *5 分鐘後過期*"
        )
    else:
        text = (
            f"🔐 *移除 AWS 帳號請求*\n\n"
            f"{source_line}"
            f"🆔 *帳號 ID：* `{account_id}`\n"
            f"📛 *名稱：* {safe_name}\n\n"
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
    # code block 內不需要 escape

    result_preview = ""
    if result:
        if result.startswith('❌') or 'error' in result.lower()[:100]:
            result_status = "❌"
        else:
            result_status = "✅"
        result_text = result[:500] + '...' if len(result) > 500 else result
        # 用 code block（``` ）而非 inline code，避免多行內容破壞格式
        result_preview = f"\n{result_status} *結果：*\n```\n{result_text}\n```"

    source_line = f"🤖 {_escape_markdown(source)} · " if source else ""
    remaining_line = f"⏱ {remaining}" if remaining else ""
    session_info = f"{source_line}{remaining_line}".strip()
    session_line = f"\n{session_info}" if session_info else ""

    text = (
        f"🔓 *自動批准* (信任中)\n"
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
                lines.append(f" {i+1}. `{cmd_preview}`")
            if len(grantable) > max_display:
                lines.append(f" ...及其他 {len(grantable) - max_display} 個命令")

        if requires_individual:
            lines.append(f"\n⚠️ *需個別審批 ({len(requires_individual)}):*")
            offset = len(grantable)
            for i, d in enumerate(requires_individual[:max_display]):
                cmd_preview = d['command'][:80]
                lines.append(f" {offset+i+1}. `{cmd_preview}`")
            if len(requires_individual) > max_display:
                lines.append(f" ...及其他 {len(requires_individual) - max_display} 個命令")

        if blocked:
            lines.append(f"\n🚫 *已攔截 ({len(blocked)}):*")
            offset = len(grantable) + len(requires_individual)
            for i, d in enumerate(blocked[:max_display]):
                cmd_preview = d['command'][:80]
                lines.append(f" {offset+i+1}. `{cmd_preview}`")
            if len(blocked) > max_display:
                lines.append(f" ...及其他 {len(blocked) - max_display} 個命令")

        commands_text = '\n'.join(lines)

        text = (
            f"🔑 *批次權限申請*\n\n"
            f"🤖 *來源：* {_escape_markdown(source or 'Unknown')}\n"
            f"💬 *原因：* {_escape_markdown(reason or '')}\n"
            f"🏦 *帳號：* `{account_id}`\n"
            f"⏱ *TTL：* {ttl_minutes} 分鐘 | 模式：{mode_str}\n"
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

        if result and (result.startswith('❌') or 'error' in result.lower()[:100]):
            result_status = "❌"
        else:
            result_status = "✅"

        result_text = result[:500] + '...' if result and len(result) > 500 else (result or '')

        grant_short = grant_id[:20] + '...' if len(grant_id) > 20 else grant_id

        text = (
            f"🔑 *Grant 自動執行*\n"
            f"📋 `{cmd_preview}`\n"
            f"{result_status} *結果：*\n```\n{result_text}\n```\n"
            f"📊 剩餘: {remaining_info}\n"
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
    """發送 Grant Session 完成/過期通知"""
    try:
        grant_short = grant_id[:20] + '...' if len(grant_id) > 20 else grant_id

        text = (
            f"🔑 *Grant 已結束*\n\n"
            f"🆔 `{grant_short}`\n"
            f"💬 *原因：* {_escape_markdown(reason or '')}"
        )

        _send_message_silent(text)

    except Exception as e:
        print(f"[GRANT] send_grant_complete_notification error: {e}")


def send_blocked_notification(
    command: str,
    block_reason: str,
    source: str = '',
) -> None:
    """發送命令被封鎖的靜默通知"""
    try:
        cmd_preview = command[:100] + '...' if len(command) > 100 else command

        text = (
            f"🚫 *命令被封鎖*\n\n"
            f"📋 `{cmd_preview}`\n"
            f"❌ *原因：* {_escape_markdown(block_reason)}\n"
            f"🤖 *來源：* {_escape_markdown(source or 'Unknown')}"
        )

        _send_message_silent(text)

    except Exception as e:
        print(f"[BLOCKED] send_blocked_notification error: {e}")


# ============================================================================
# Trust Upload Notifications
# ============================================================================

def send_trust_upload_notification(
    filename: str,
    content_size: int,
    sha256_hash: str,
    trust_id: str,
    upload_count: int,
    max_uploads: int,
    source: str = '',
) -> None:
    """發送 Trust Upload 自動批准的靜默通知"""
    try:
        size_str = format_size_human(content_size)

        source_line = f"🤖 {_escape_markdown(source)}\n" if source else ""
        hash_short = sha256_hash[:16] if sha256_hash != 'batch' else 'batch'

        text = (
            f"📤 *信任上傳* (自動)\n"
            f"📁 `{filename}`\n"
            f"📊 {size_str} | SHA256: `{hash_short}`\n"
            f"📈 上傳: {upload_count}/{max_uploads}\n"
            f"{source_line}"
            f"🔑 `{trust_id}`"
        )

        keyboard = {
            'inline_keyboard': [[
                {'text': '🛑 結束信任', 'callback_data': f'revoke_trust:{trust_id}'}
            ]]
        }

        _send_message_silent(text, keyboard)

    except Exception as e:
        print(f"[TRUST UPLOAD] send_trust_upload_notification error: {e}")


def send_batch_upload_notification(
    batch_id: str,
    file_count: int,
    total_size: int,
    ext_counts: dict,
    reason: str,
    source: str = '',
    account_name: str = '',
    trust_scope: str = '',
) -> None:
    """發送批量上傳審批請求通知"""
    try:
        size_str = format_size_human(total_size)

        # build_info_lines escapes internally; no manual escape needed
        info_lines = build_info_lines(
            source=source or 'Unknown',
            reason=reason,
        )
        safe_account = _escape_markdown(account_name) if account_name else ''

        # Format extension groups
        ext_parts = []
        for ext, count in sorted(ext_counts.items()):
            ext_parts.append(f"{ext}: {count}")
        ext_line = ', '.join(ext_parts)

        account_line = f"🏦 *帳號：* {safe_account}\n" if safe_account else ""

        text = (
            f"📁 *批量上傳請求*\n\n"
            f"{info_lines}"
            f"{account_line}\n"
            f"📄 *{file_count} 個檔案* ({size_str})\n"
            f"📊 {ext_line}\n\n"
            f"🆔 `{batch_id}`"
        )

        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '📁 批准上傳', 'callback_data': f'approve:{batch_id}'},
                    {'text': '❌ 拒絕', 'callback_data': f'deny:{batch_id}'},
                ],
                [
                    {'text': '🔓 批准 + 信任10分鐘', 'callback_data': f'approve_trust:{batch_id}'},
                ],
            ]
        }

        _send_message(text, keyboard)

    except Exception as e:
        print(f"[BATCH UPLOAD] send_batch_upload_notification error: {e}")
