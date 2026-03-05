"""Telegram notification functions for approval requests.

Extracted from app.py to break circular dependency:
  app.py → mcp_tools.py → app.py (for send_approval_request etc.)
Now: mcp_tools.py → notifications.py (no cycle)

sprint7-002 (Approach B):
  - send_approval_request now returns (bool, Optional[int]) — success + message_id
  - post_notification_setup() stores telegram_message_id in DynamoDB and
    schedules EventBridge one-time expiry via SchedulerService
"""

import logging
import os
from typing import NamedTuple, Optional

import telegram as _telegram
from commands import is_dangerous, check_lambda_env_update
from constants import COMMAND_APPROVAL_TIMEOUT, TRUST_SESSION_MAX_COMMANDS, UPLOAD_TIMEOUT, GRANT_APPROVAL_TIMEOUT
from utils import format_size_human, build_info_lines

logger = logging.getLogger(__name__)


class NotificationResult(NamedTuple):
    """Result of sending a Telegram approval notification.

    Attributes:
        ok:         True if the Telegram API accepted the message.
        message_id: The Telegram ``message_id`` of the sent message, or None
                    if the send failed or the API response was unexpected.

    Backward-compatibility note:
        Callers that do ``if result:`` or ``if not result:`` continue to work
        because ``NamedTuple`` instances are truthy when ``ok`` is True, but
        — unlike a plain bool — only if converted explicitly.  Callers that
        relied on bare ``bool(result)`` should use ``result.ok`` instead.
        The ``bool()`` of a NamedTuple is *always* True (non-empty tuple), so
        callers using ``if not notified:`` **must** be updated to ``if not notified.ok:``.
    """
    ok: bool
    message_id: Optional[int]


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


# ============================================================================
# Post-notification Setup (sprint7-002)
# ============================================================================

def post_notification_setup(
    request_id: str,
    telegram_message_id: int,
    expires_at: int,
) -> None:
    """Store ``telegram_message_id`` in DynamoDB and schedule expiry cleanup.

    This function is called after a Telegram approval message has been sent
    successfully.  It performs two non-critical side-effects:

    1. Persists ``telegram_message_id`` on the DynamoDB item so the cleanup
       handler can later remove the inline keyboard.
    2. Creates an EventBridge Scheduler one-time schedule to invoke the
       ``cleanup_expired`` Lambda path at ``expires_at``.

    Both operations are best-effort: failures are logged but never propagated
    to the caller (the approval request has already been created).

    Args:
        request_id:         The Bouncer request ID (DynamoDB primary key).
        telegram_message_id: The ``message_id`` returned by the Telegram API.
        expires_at:         Unix timestamp when the request expires (DynamoDB TTL).
    """
    # 1. Store telegram_message_id in DynamoDB
    try:
        from db import table as _table

        _table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET telegram_message_id = :mid",
            ExpressionAttributeValues={":mid": telegram_message_id},
        )
        logger.info(
            "[POST-NOTIFY] Stored telegram_message_id=%s for request %s",
            telegram_message_id,
            request_id,
        )
    except Exception as exc:
        logger.error(
            "[POST-NOTIFY] Failed to store telegram_message_id for %s: %s",
            request_id,
            exc,
        )

    # 2. Schedule EventBridge expiry trigger (embed telegram_message_id as fallback)
    try:
        from scheduler_service import get_scheduler_service

        svc = get_scheduler_service()
        svc.create_expiry_schedule(
            request_id=request_id,
            expires_at=expires_at,
            telegram_message_id=telegram_message_id,
        )
    except Exception as exc:
        logger.error(
            "[POST-NOTIFY] Failed to create expiry schedule for %s: %s",
            request_id,
            exc,
        )


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

    # 特殊警告：lambda update-function-configuration --environment
    lambda_env_level, lambda_env_msg = check_lambda_env_update(command)
    lambda_env_warning = ""
    if lambda_env_level == 'DANGEROUS' and lambda_env_msg:
        lambda_env_warning = f"\n🔴 *{_escape_markdown(lambda_env_msg)}*\n"

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
            logger.error(f"Error: {e}")
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
            f"{lambda_env_warning}"
            f"{template_scan_block}"
            f"\n⚠️ *此操作可能不可逆，請仔細確認！*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"⏰ *{timeout_str}後過期*"
        )
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '⚠️ Confirm', 'callback_data': f'approve:{request_id}', 'style': 'primary'},
                    {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
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
                    {'text': '✅ Approve', 'callback_data': f'approve:{request_id}', 'style': 'success'},
                    {'text': '🔓 Trust 10min', 'callback_data': f'approve_trust:{request_id}', 'style': 'primary'},
                    {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
                ]
            ]
        }

    result = _send_message(text, keyboard)
    ok = bool(result and result.get('ok'))
    message_id: Optional[int] = None
    if ok:
        message_id = result.get('result', {}).get('message_id')
    return NotificationResult(ok=ok, message_id=message_id)


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
            {'text': '✅ Approve', 'callback_data': f'approve:{request_id}', 'style': 'success'},
            {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
        ]]
    }

    _send_message(text, keyboard)


def send_trust_auto_approve_notification(command: str, trust_id: str, remaining: str, count: int,
                                         result: str = None, source: str = None, reason: str = None):
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
    reason_line = f"\n💬 {_escape_markdown(reason)}" if reason else ""

    text = (
        f"🔓 *自動批准* (信任中)\n"
        f"📋 `{cmd_preview}`\n"
        f"📊 {count}/{TRUST_SESSION_MAX_COMMANDS}"
        f"{session_line}"
        f"{reason_line}"
        f"{result_preview}"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '🛑 End Trust', 'callback_data': f'revoke_trust:{trust_id}', 'style': 'danger'}
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
            f"🆔 *ID：* `{grant_id}`\n"
            f"⏰ *審批期限：{GRANT_APPROVAL_TIMEOUT // 60} 分鐘*"
        )

        # 根據是否有 requires_individual 決定按鈕
        buttons = []
        if grantable or requires_individual:
            buttons.append([
                {'text': '✅ Approve All', 'callback_data': f'grant_approve_all:{grant_id}', 'style': 'success'},
            ])
            if grantable and requires_individual:
                buttons[0].append(
                    {'text': '✅ Approve Safe Only', 'callback_data': f'grant_approve_safe:{grant_id}', 'style': 'success'},
                )
        buttons.append([
            {'text': '❌ Reject', 'callback_data': f'grant_deny:{grant_id}', 'style': 'danger'},
        ])

        keyboard = {'inline_keyboard': buttons}
        _send_message(text, keyboard)

    except Exception as e:
        logger.error(f"[GRANT] send_grant_request_notification error: {e}")


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
                {'text': '🛑 Revoke Grant', 'callback_data': f'grant_revoke:{grant_id}', 'style': 'danger'}
            ]]
        }

        _send_message_silent(text, keyboard)

    except Exception as e:
        logger.error(f"[GRANT] send_grant_execute_notification error: {e}")


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
        logger.error(f"[GRANT] send_grant_complete_notification error: {e}")


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
        logger.error(f"[BLOCKED] send_blocked_notification error: {e}")


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
                {'text': '🛑 End Trust', 'callback_data': f'revoke_trust:{trust_id}', 'style': 'danger'}
            ]]
        }

        _send_message_silent(text, keyboard)

    except Exception as e:
        logger.error(f"[TRUST UPLOAD] send_trust_upload_notification error: {e}")


def send_batch_upload_notification(
    batch_id: str,
    file_count: int,
    total_size: int,
    ext_counts: dict,
    reason: str,
    source: str = '',
    account_name: str = '',
    trust_scope: str = '',
    timeout: int = None,
) -> NotificationResult:
    """發送批量上傳審批請求通知

    Returns:
        NotificationResult with ok flag and optional message_id.
    """
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

        # Timeout display
        timeout_val = timeout if timeout is not None else UPLOAD_TIMEOUT
        if timeout_val < 60:
            timeout_str = f"{timeout_val} 秒"
        elif timeout_val < 3600:
            timeout_str = f"{timeout_val // 60} 分鐘"
        else:
            timeout_str = f"{timeout_val // 3600} 小時"

        text = (
            f"📁 *批量上傳請求*\n\n"
            f"{info_lines}"
            f"{account_line}\n"
            f"📄 *{file_count} 個檔案* ({size_str})\n"
            f"📊 {ext_line}\n\n"
            f"🆔 `{batch_id}`\n"
            f"⏰ *{timeout_str}後過期*"
        )

        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '📁 Approve Upload', 'callback_data': f'approve:{batch_id}', 'style': 'success'},
                    {'text': '❌ Reject', 'callback_data': f'deny:{batch_id}', 'style': 'danger'},
                ],
                [
                    {'text': '🔓 Approve + Trust 10min', 'callback_data': f'approve_trust:{batch_id}', 'style': 'success'},
                ],
            ]
        }

        result = _send_message(text, keyboard)
        ok = bool(result and result.get('ok'))
        message_id: Optional[int] = None
        if ok:
            message_id = result.get('result', {}).get('message_id')
        return NotificationResult(ok=ok, message_id=message_id)

    except Exception as e:
        logger.error(f"[BATCH UPLOAD] send_batch_upload_notification error: {e}")
        return NotificationResult(ok=False, message_id=None)


# ============================================================================
# Presigned URL Notifications (bouncer-sec-007)
# ============================================================================

def send_presigned_notification(
    filename: str,
    source: str,
    account_id: str,
    expires_at: str,
) -> None:
    """發送 Presigned URL 生成的靜默通知（單檔）。

    ❌ 絕對不含 presigned URL 本身。
    """
    try:
        safe_filename = _escape_markdown(filename or '')
        safe_source = _escape_markdown(source or 'Unknown')
        safe_account_id = _escape_markdown(account_id or '')
        safe_expires_at = _escape_markdown(expires_at or '')

        text = (
            f"📎 *Presigned URL 已生成*\n"
            f"來源：{safe_source}\n"
            f"檔案：`{safe_filename}`\n"
            f"帳號：`{safe_account_id}`\n"
            f"過期：`{safe_expires_at}`"
        )

        _send_message_silent(text)

    except Exception as e:
        logger.error(f"[PRESIGNED] send_presigned_notification error: {e}")


def send_presigned_batch_notification(
    source: str,
    count: int,
    account_id: str,
    expires_at: str,
) -> None:
    """發送 Presigned URL Batch 生成的靜默通知。

    ❌ 絕對不含任何 presigned URL。
    """
    try:
        safe_source = _escape_markdown(source or 'Unknown')
        safe_account_id = _escape_markdown(account_id or '')
        safe_expires_at = _escape_markdown(expires_at or '')

        text = (
            f"📎 *Presigned URL Batch 已生成*\n"
            f"來源：{safe_source}\n"
            f"檔案數：{count} 個\n"
            f"帳號：`{safe_account_id}`\n"
            f"過期：`{safe_expires_at}`"
        )

        _send_message_silent(text)

    except Exception as e:
        logger.error(f"[PRESIGNED] send_presigned_batch_notification error: {e}")


# ============================================================================
# Trust Session Summary (sprint9-007-phase-a)
# =====================================================================

def send_trust_session_summary(trust_item: dict, end_reason: str = 'revoke') -> None:
    """Send a Telegram summary when a trust session ends (revoke or expiry).

    Formats a message listing all commands executed during the trust session,
    with success/failure counts and truncation for large sessions.

    Args:
        trust_item: DynamoDB trust session item (may contain commands_executed list)
        end_reason: 'revoke' (manual revoke) or 'expiry' (auto expiry via scheduler)
    """
    try:
        commands_executed = trust_item.get('commands_executed', [])
        trust_id_short = str(trust_item.get('request_id', ''))[-12:]

        # Header differs by end reason
        if end_reason == 'expiry':
            # 🔓 信任時段結束（自動到期）
            header = "\U0001f513 *\u4fe1\u4efb\u6642\u6bb5\u7d50\u675f\uff08\u81ea\u52d5\u5230\u671f\uff09*"
        else:
            # 🔓 信任時段結束（手動撒銷）
            header = "\U0001f513 *\u4fe1\u4efb\u6642\u6bb5\u7d50\u675f\uff08\u624b\u52d5\u6492\u92b7\uff09*"

        # No commands — send brief notification
        if not commands_executed:
            text = (
                header + "\n\n"
                "\U0001f194 `" + trust_id_short + "`\n"
                "\U0001f4cb \u7121\u547d\u4ee4\u57f7\u884c"
            )
            _send_message_silent(text)
            return

        # Calculate duration
        import time as _t
        created_at = int(trust_item.get('created_at', 0))
        duration_secs = int(_t.time()) - created_at if created_at else 0
        duration_mins = duration_secs // 60
        duration_sec_part = duration_secs % 60
        duration_str = str(duration_mins) + " \u5206 " + str(duration_sec_part) + " \u79d2"

        # Count failures
        total = len(commands_executed)
        fail_count = sum(1 for e in commands_executed if not e.get('success', True))

        # Build command list (max 10 shown)
        display_limit = 10
        display_cmds = commands_executed[:display_limit]
        truncated = total > display_limit

        cmd_lines = []
        for i, entry in enumerate(display_cmds, start=1):
            cmd = entry.get('cmd', '')[:80]
            ok_icon = "\u2705" if entry.get('success', True) else "\u274c"
            cmd_lines.append("  " + str(i) + "\\. " + ok_icon + " `" + _escape_markdown(cmd) + "`")
        if truncated:
            cmd_lines.append("  _...\u9084\u6709 " + str(total - display_limit) + " \u500b\u547d\u4ee4_")

        cmd_block = "\n".join(cmd_lines)

        # Result line
        if fail_count == 0:
            result_line = "\u2705 \u5168\u90e8\u6210\u529f"
        else:
            result_line = "\u26a0\ufe0f " + str(fail_count) + " \u500b\u5931\u6557\uff08\u8acb\u67e5\u770b CloudWatch Logs\uff09"

        executed_label = "\u57f7\u884c\u4e86 " + str(total) + " \u500b\u547d\u4ee4\uff1a"

        text = (
            header + "\n\n"
            "\U0001f194 `" + trust_id_short + "`\n"
            "\u23f1 \u6642\u9577\uff1a" + duration_str + "\n"
            "\U0001f4cb " + executed_label + "\n"
            + cmd_block + "\n\n"
            + result_line
        )

        _send_message_silent(text)

    except Exception as exc:
        logger.error('[TRUST SUMMARY] send_trust_session_summary error: %s', exc)
# Deploy Frontend Notification (sprint9-003)
# ============================================================================

def send_deploy_frontend_notification(
    request_id: str,
    files_summary: list,
    target_info: dict,
    project: str = "",
    reason: str = "",
    source: str = "",
) -> "NotificationResult":
    """Send a Telegram approval request for a frontend deployment.

    Args:
        request_id:     Bouncer request ID.
        files_summary:  List of dicts with filename, size, cache_control.
        target_info:    Dict with frontend_bucket, distribution_id, region.
        project:        Project name (e.g. "ztp-files").
        reason:         Human-readable deploy reason.
        source:         Requesting agent/bot name.

    Returns:
        NotificationResult(ok, message_id)
    """
    try:
        from utils import format_size_human

        total_size = sum(int(f.get("size", 0)) for f in files_summary)
        total_size_str = format_size_human(total_size)
        file_count = len(files_summary)

        # Build per-file list (max 10 displayed)
        file_lines = []
        for i, f in enumerate(files_summary[:10]):
            fname = f.get("filename", "?")
            fsize = format_size_human(int(f.get("size", 0)))
            cc = f.get("cache_control", "")
            if "immutable" in cc:
                cc_short = "immutable"
            elif "no-store" in cc:
                cc_short = "no-cache"
            else:
                cc_short = "no-cache"
            file_lines.append(f"  \u2022 `{fname}` ({fsize}) \u2192 {cc_short}")
        if file_count > 10:
            file_lines.append(f"  _...and {file_count - 10} more files_")

        files_text = "\n".join(file_lines)

        safe_project = _escape_markdown(project or "unknown")
        safe_source = _escape_markdown(source or "Unknown")
        safe_reason = _escape_markdown(reason or "No reason provided")
        safe_bucket = _escape_markdown(target_info.get("frontend_bucket", ""))
        safe_dist = _escape_markdown(target_info.get("distribution_id", ""))

        text = (
            f"\U0001f680 *Frontend Deploy Request*\n\n"
            f"\U0001f4e6 *Project:* `{safe_project}`\n"
            f"\U0001f5c2 *Target Bucket:* `{safe_bucket}`\n"
            f"\u2601\ufe0f *CloudFront:* `{safe_dist}`\n"
            f"\U0001f4c1 *Files ({file_count}, {total_size_str}):*\n"
            f"{files_text}\n\n"
            f"\U0001f916 *Source:* {safe_source}\n"
            f"\U0001f4ac *Reason:* {safe_reason}\n\n"
            f"\U0001f194 *ID:* `{request_id}`\n"
            f"\u23f0 *Expires in {UPLOAD_TIMEOUT // 60} min*"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Deploy", "callback_data": f"approve:{request_id}", "style": "success"},
                    {"text": "❌ Reject", "callback_data": f"deny:{request_id}", "style": "danger"},
                ],
            ]
        }

        result = _send_message(text, keyboard)
        ok = bool(result and result.get("ok"))
        message_id = None
        if ok:
            message_id = result.get("result", {}).get("message_id")
        return NotificationResult(ok=ok, message_id=message_id)

    except Exception as exc:
        logger.error("[DEPLOY-FRONTEND] send_deploy_frontend_notification error: %s", exc)
        return NotificationResult(ok=False, message_id=None)
