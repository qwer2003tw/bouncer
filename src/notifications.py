"""Telegram notification functions for approval requests.

Extracted from app.py to break circular dependency:
  app.py → mcp_tools.py → app.py (for send_approval_request etc.)
Now: mcp_tools.py → notifications.py (no cycle)

sprint7-002 (Approach B):
  - send_approval_request now returns (bool, Optional[int]) — success + message_id
  - post_notification_setup() stores telegram_message_id in DynamoDB and
    schedules EventBridge one-time expiry via SchedulerService

sprint24-003:
  - Deduplicate consecutive auto_approved notifications to reduce spam
"""

import datetime
import time
import urllib.error
from typing import NamedTuple, Optional

from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger
import telegram as _telegram
from commands import is_dangerous, check_lambda_env_update
from constants import COMMAND_APPROVAL_TIMEOUT, TRUST_SESSION_MAX_COMMANDS, UPLOAD_TIMEOUT, GRANT_APPROVAL_TIMEOUT, DEFAULT_ACCOUNT_ID
from trust import is_trust_excluded
from telegram_entities import MessageBuilder, format_command_output
from utils import format_size_human, extract_exit_code

logger = Logger(service="bouncer")

# Notification throttling (sprint24-003)
# Track last notification time to prevent spam from consecutive auto-approved commands
_last_notification_time = {}
NOTIFICATION_THROTTLE_SECONDS = 60


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


def _should_throttle_notification(notification_type: str) -> bool:
    """Check if we should throttle a notification based on recent activity.

    Args:
        notification_type: Type of notification (e.g., 'auto_approve', 'trust_approve')

    Returns:
        True if notification should be skipped (throttled), False if it should be sent
    """
    global _last_notification_time

    current_time = time.time()
    last_time = _last_notification_time.get(notification_type, 0)

    if current_time - last_time < NOTIFICATION_THROTTLE_SECONDS:
        logger.info("Throttling %s notification (last sent %.1fs ago)", notification_type, current_time - last_time, extra={"src_module": "notifications", "operation": "throttle_check", "notification_type": notification_type})
        return True

    _last_notification_time[notification_type] = current_time
    return False


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
        logger.info("Stored telegram_message_id=%s for request %s", telegram_message_id, request_id, extra={"src_module": "notifications", "operation": "post_notification_setup", "request_id": request_id, "message_id": telegram_message_id})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to store telegram_message_id for %s: %s", request_id, exc, extra={"src_module": "notifications", "operation": "post_notification_setup", "request_id": request_id, "error": str(exc)})

    # 2. Schedule EventBridge expiry trigger (embed telegram_message_id as fallback)
    try:
        from scheduler_service import get_scheduler_service

        svc = get_scheduler_service()
        svc.create_expiry_schedule(
            request_id=request_id,
            expires_at=expires_at,
            telegram_message_id=telegram_message_id,
        )
    except ClientError as exc:
        logger.exception("Failed to create expiry schedule for %s: %s", request_id, exc, extra={"src_module": "notifications", "operation": "post_notification_setup", "request_id": request_id, "error": str(exc)})


def _store_notification_snapshot(request_id: str, text: str, message_id: int) -> None:
    """Store Telegram notification text snapshot for UIUX analysis.

    Allows future analysis of: notification clarity, approve rate by text length,
    information density vs response time.

    Non-fatal: failures are silently swallowed.
    """
    from db import table as _table
    try:
        _table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET notification_text = :t, notification_length = :l, notification_message_id = :m',
            ExpressionAttributeValues={
                ':t': text[:2000],  # truncate for DDB item size
                ':l': len(text),
                ':m': message_id or 0,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to emit notification metric", exc_info=True)


def send_approval_request(request_id: str, command: str, reason: str, timeout: int = COMMAND_APPROVAL_TIMEOUT,
                          source: str = None, account_id: str = None, account_name: str = None,
                          assume_role: str = None, context: str = None,
                          template_scan_result: dict = None) -> bool:
    """發送 Telegram 審批請求（entities 模式，無 parse_mode）

    Returns:
        True if the Telegram message was sent successfully, False otherwise.
    """
    cmd_preview = command if len(command) <= 500 else command[:500] + '...'

    dangerous = is_dangerous(command)

    # 特殊警告：lambda update-function-configuration --environment
    lambda_env_level, lambda_env_msg = check_lambda_env_update(command)

    if timeout < 60:
        timeout_str = f"{timeout} 秒"
    elif timeout < 3600:
        timeout_str = f"{timeout // 60} 分鐘"
    else:
        timeout_str = f"{timeout // 3600} 小時"

    # Build account line info
    if account_id and account_name:
        account_display = (account_id, account_name)
        account_mode = 'named'
    elif assume_role:
        try:
            parsed_account_id = assume_role.split(':')[4]
            role_name = assume_role.split('/')[-1]
            account_display = (parsed_account_id, role_name)
            account_mode = 'named'
        except (ValueError, IndexError) as e:
            logger.exception("Failed to parse assume_role ARN: %s", e, extra={"src_module": "notifications", "operation": "send_approval_request", "error": str(e)})
            account_display = (assume_role, None)
            account_mode = 'role'
    else:
        account_display = (DEFAULT_ACCOUNT_ID, '預設')
        account_mode = 'named'

    # ---- Build message with MessageBuilder ----
    mb = MessageBuilder()

    if dangerous:
        mb.text("⚠️ ").bold("高危操作請求").text(" ⚠️").newline(2)
    else:
        mb.text("🔐 ").bold("AWS 執行請求").newline(2)

    # Source / context lines (no escape needed)
    if source:
        mb.text("🤖 ").bold("來源：").text(f" {source}").newline()
    if context:
        mb.text("📝 ").bold("任務：").text(f" {context}").newline()

    # Account line
    if account_mode == 'named':
        acc_id, acc_name = account_display
        mb.text("🏦 ").bold("帳號：").text(" ").code(acc_id).text(f" ({acc_name})").newline()
    else:
        # role fallback
        acc_id, _ = account_display
        mb.text("🏦 ").bold("Role：").text(" ").code(acc_id).newline()

    # Command
    mb.text("📋 ").bold("命令：").newline()
    mb.code(cmd_preview).newline(2)

    # Reason
    mb.text("💬 ").bold("原因：").text(f" {reason}").newline()

    # Lambda env warning
    if lambda_env_level == 'DANGEROUS' and lambda_env_msg:
        mb.newline()
        mb.text("🔴 ").bold(lambda_env_msg).newline()

    # Template scan block
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

        escalate_note = " ⚠️ 強制人工審批" if escalate else ""
        mb.newline()
        mb.text("🔍 ").bold("Template Scan：").text(
            f" {severity_emoji} {severity.upper()} ({hit_count} hits, score={max_score}){escalate_note}"
        ).newline()

        factors = template_scan_result.get('factors', [])
        for factor in factors[:3]:
            details = str(factor.get('details', ''))
            mb.text("  • ").code(details).newline()
        if len(factors) > 3:
            mb.text(f"  ...及其他 {len(factors) - 3} 個風險").newline()

    if dangerous:
        mb.newline()
        mb.text("⚠️ ").bold("此操作可能不可逆，請仔細確認！").newline(2)

    # ID and expiry
    mb.text("🆔 ").bold("ID：").text(" ").code(request_id).newline()

    # Calculate expiry timestamp and format for display
    expires_ts = int(time.time()) + timeout
    expires_str = datetime.datetime.fromtimestamp(expires_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mb.text("⏰ ").bold(f"{timeout_str}後過期").text(" (").date_time(expires_str, expires_ts).text(")")

    text, entities = mb.build()

    if dangerous:
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '⚠️ Confirm', 'callback_data': f'approve:{request_id}', 'style': 'primary'},
                    {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
                ]
            ]
        }
    else:
        # s57-001: Hide Trust button for trust-excluded commands
        if is_trust_excluded(command):
            keyboard = {
                'inline_keyboard': [
                    [
                        {'text': '✅ Approve', 'callback_data': f'approve:{request_id}', 'style': 'success'},
                        {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
                    ]
                ]
            }
        else:
            keyboard = {
                'inline_keyboard': [
                    [
                        {'text': '✅ Approve', 'callback_data': f'approve:{request_id}', 'style': 'success'},
                        {'text': '🔓 Trust 10min', 'callback_data': f'approve_trust:{request_id}', 'style': 'primary'},
                        {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
                    ]
                ]
            }

    result = _telegram.send_message_with_entities(text, entities, reply_markup=keyboard)
    ok = bool(result and result.get('ok'))
    message_id: Optional[int] = None
    if ok:
        message_id = result.get('result', {}).get('message_id')
        # Store notification snapshot for UIUX analysis (best-effort, non-fatal)
        try:
            _store_notification_snapshot(request_id, text, message_id)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to store notification snapshot", exc_info=True)
    return NotificationResult(ok=ok, message_id=message_id)

def send_account_approval_request(request_id: str, action: str, account_id: str, name: str, role_arn: str, source: str, context: str = None):
    """發送帳號管理的 Telegram 審批請求（entities 模式，無 parse_mode）"""
    try:
        mb = MessageBuilder()

        if action == 'add':
            mb.text("🔐 ").bold("新增 AWS 帳號請求").newline(2)
        else:
            mb.text("🔐 ").bold("移除 AWS 帳號請求").newline(2)

        # Source / context (no escape needed in entities mode)
        if source:
            mb.text("🤖 ").bold("來源：").text(f" {source}").newline()
        if context:
            mb.text("📝 ").bold("任務：").text(f" {context}").newline()

        mb.text("🆔 ").bold("帳號 ID：").text(" ").code(account_id).newline()
        mb.text("📛 ").bold("名稱：").text(f" {name or ''}").newline()

        if action == 'add' and role_arn:
            mb.text("🔗 ").bold("Role：").text(" ").code(role_arn).newline()

        mb.newline()
        mb.text("📋 ").bold("請求 ID：").text(" ").code(request_id).newline()
        mb.text("⏰ ").bold("10 分鐘後過期")

        text, entities = mb.build()

        keyboard = {
            'inline_keyboard': [[
                {'text': '✅ Approve', 'callback_data': f'approve:{request_id}', 'style': 'success'},
                {'text': '❌ Reject', 'callback_data': f'deny:{request_id}', 'style': 'danger'}
            ]]
        }

        result = _telegram.send_message_with_entities(text, entities, reply_markup=keyboard)
        return bool(result and result.get('ok'))
    except Exception as e:  # noqa: BLE001
        logger.exception("send_account_approval_request failed: %s", e, extra={
            "src_module": "notifications",
            "operation": "send_account_approval_request",
            "request_id": request_id,
            "error": str(e),
        })
        return False

def send_trust_auto_approve_notification(command: str, trust_id: str, remaining: str, count: int,
                                         result: str = None, source: str = None, reason: str = None):
    """發送 Trust Session 自動批准的靜默通知（entities 模式，無 parse_mode）

    sprint24-003: Throttles notifications to reduce spam from consecutive commands.
    """
    # Throttle consecutive notifications (sprint24-003)
    if _should_throttle_notification('trust_approve'):
        logger.info("Skipped trust auto-approve notification for command: %s...", command[:50], extra={"src_module": "notifications", "operation": "trust_auto_approve_notification"})
        return

    cmd_preview = command if len(command) <= 100 else command[:100] + '...'

    mb = MessageBuilder()
    mb.text("🔓 ").bold("自動批准").text(" (信任中)").newline()
    mb.text("📋 ").code(cmd_preview).newline()
    mb.text(f"📊 {count}/{TRUST_SESSION_MAX_COMMANDS}")

    # Session info line: source · remaining
    session_parts = []
    if source:
        session_parts.append(f"🤖 {source}")
    if remaining:
        session_parts.append(f"⏱ {remaining}")
    if session_parts:
        mb.newline().text(" · ".join(session_parts))

    if reason:
        mb.newline().text("💬 ").text(reason)

    # Build header first (without result)
    header_text, header_entities = mb.build()

    # Format command result with expandable blockquote for long output
    if result:
        from telegram_entities import _utf16_len
        _exit_code = extract_exit_code(result)
        result_status = "❌" if (_exit_code is not None and _exit_code != 0) else "✅"

        # Build result header line
        result_header = f"\n{result_status} 結果：\n"
        result_header_entity = {
            "type": "bold",
            "offset": _utf16_len(header_text) + _utf16_len(f"\n{result_status} "),
            "length": _utf16_len("結果：")
        }

        # Get formatted result entities (expandable_blockquote or pre based on length)
        result_entities, result_text = format_command_output(result)

        # Adjust result entity offsets to account for header + result_header
        header_and_result_header_len = _utf16_len(header_text + result_header)
        for entity in result_entities:
            entity['offset'] += header_and_result_header_len

        # Combine everything
        text = header_text + result_header + result_text
        entities = header_entities + [result_header_entity] + result_entities
    else:
        text, entities = header_text, header_entities

    keyboard = {
        'inline_keyboard': [[
            {'text': '🛑 End Trust', 'callback_data': f'revoke_trust:{trust_id}', 'style': 'danger'}
        ]]
    }

    # Safety net: Telegram 4096 char limit
    TELEGRAM_MAX_TEXT = 4096
    if len(text) > TELEGRAM_MAX_TEXT:
        # Truncate result text to fit within limit
        from telegram_entities import _utf16_len
        truncation_notice = "\n\n[輸出已截斷，超過 Telegram 4096 字元限制]"
        available = TELEGRAM_MAX_TEXT - len(header_text) - len(result_header) - len(truncation_notice) if result else TELEGRAM_MAX_TEXT - len(header_text) - len(truncation_notice)
        if available > 0 and result:
            result_text = result_text[:available] + truncation_notice
            # Rebuild text with truncated result
            text = header_text + result_header + result_text
            # Re-compute result entities for truncated text
            result_entities, result_text_for_entities = format_command_output(result_text)
            header_and_result_header_len = _utf16_len(header_text + result_header)
            for entity in result_entities:
                entity['offset'] += header_and_result_header_len
            entities = header_entities + [result_header_entity] + result_entities
        else:
            # Even header is too long, truncate everything
            text = text[:TELEGRAM_MAX_TEXT - len(truncation_notice)] + truncation_notice
            entities = []

    _telegram.send_message_with_entities(text, entities, reply_markup=keyboard, silent=True)


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
    project: str = None,
) -> None:
    """發送 Grant Session 審批請求通知（entities 模式，無 parse_mode）

    Args:
        grant_id: Grant ID
        commands_detail: 命令預檢結果清單
        reason: 申請原因
        source: 請求來源
        account_id: AWS 帳號 ID
        ttl_minutes: TTL（分鐘）
        allow_repeat: 是否允許重複
        project: 專案名稱（如 ztp-files）
    """
    try:
        mode_str = '可重複' if allow_repeat else '一次性'

        # 分類統計
        grantable = [d for d in commands_detail if d.get('category') == 'grantable']
        requires_individual = [d for d in commands_detail if d.get('category') == 'requires_individual']
        blocked = [d for d in commands_detail if d.get('category') == 'blocked']

        max_display = 10

        mb = MessageBuilder()
        mb.text("🔑 ").bold("批次權限申請").newline(2)
        mb.text("🤖 ").bold("來源：").text(f" {source or 'Unknown'}").newline()
        if project:
            mb.text("📦 ").bold("專案：").text(f" {project}").newline()
            mb.italic(f"(以 {project} deploy role 執行)").newline()
        mb.text("💬 ").bold("原因：").text(f" {reason or ''}").newline()
        mb.text("🏦 ").bold("帳號：").text(" ").code(str(account_id)).newline()
        mb.text("⏱ ").bold("TTL：").text(f" {ttl_minutes} 分鐘 | 模式：{mode_str}")

        if grantable:
            mb.newline(2).text("✅ ").bold(f"可授權 ({len(grantable)}):")
            for i, d in enumerate(grantable[:max_display]):
                cmd_preview = d['command'][:80]
                mb.newline().text(f" {i+1}. ").code(cmd_preview)
            if len(grantable) > max_display:
                mb.newline().text(f" ...及其他 {len(grantable) - max_display} 個命令")

        if requires_individual:
            mb.newline(2).text("⚠️ ").bold(f"需個別審批 ({len(requires_individual)}):")
            offset = len(grantable)
            for i, d in enumerate(requires_individual[:max_display]):
                cmd_preview = d['command'][:80]
                mb.newline().text(f" {offset+i+1}. ").code(cmd_preview)
            if len(requires_individual) > max_display:
                mb.newline().text(f" ...及其他 {len(requires_individual) - max_display} 個命令")

        if blocked:
            mb.newline(2).text("🚫 ").bold(f"已攔截 ({len(blocked)}):")
            offset = len(grantable) + len(requires_individual)
            for i, d in enumerate(blocked[:max_display]):
                cmd_preview = d['command'][:80]
                mb.newline().text(f" {offset+i+1}. ").code(cmd_preview)
            if len(blocked) > max_display:
                mb.newline().text(f" ...及其他 {len(blocked) - max_display} 個命令")

        mb.newline(2).text("🆔 ").bold("ID：").text(" ").code(grant_id)
        mb.newline().text("⏰ ").bold(f"審批期限：{GRANT_APPROVAL_TIMEOUT // 60} 分鐘")

        text, entities = mb.build()

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
        result = _telegram.send_message_with_entities(text, entities, reply_markup=keyboard)

        # #75: schedule expiry cleanup so buttons are cleared when approval times out
        telegram_message_id = (result or {}).get('result', {}).get('message_id')
        if telegram_message_id:
            import time
            # Grant approval timeout is GRANT_APPROVAL_TIMEOUT (300 seconds)
            expires_at = int(time.time()) + GRANT_APPROVAL_TIMEOUT
            try:
                post_notification_setup(
                    request_id=grant_id,
                    telegram_message_id=telegram_message_id,
                    expires_at=expires_at,
                )
            except ClientError as pns_exc:
                logger.exception("post_notification_setup failed for %s: %s", grant_id, pns_exc, extra={"src_module": "notifications", "operation": "send_grant_request_notification", "grant_id": grant_id, "error": str(pns_exc)})

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception("send_grant_request_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_grant_request_notification", "error": str(e)})

def send_grant_execute_notification(
    command: str,
    grant_id: str,
    result: str,
    remaining_info: str,
) -> None:
    """發送 Grant Session 命令自動執行的靜默通知（entities 模式，無 parse_mode）

    Args:
        command: 執行的命令
        grant_id: Grant ID
        result: 執行結果
        remaining_info: 剩餘資訊（如 "1/3 命令, 25:13"）
    """
    try:
        cmd_preview = command[:100] + '...' if len(command) > 100 else command

        _exit_code = extract_exit_code(result)
        result_status = "❌" if (_exit_code is not None and _exit_code != 0) else "✅"

        grant_short = grant_id[:20] + '...' if len(grant_id) > 20 else grant_id

        # Build header without result
        mb = MessageBuilder()
        mb.text("🔑 ").bold("Grant 自動執行").newline()
        mb.text("📋 ").code(cmd_preview).newline()
        mb.text(f"{result_status} ").bold("結果：").newline()

        header_text, header_entities = mb.build()

        # Format result with expandable blockquote for long output
        result_text = result if result else ''
        result_entities, formatted_result = format_command_output(result_text)

        # Adjust result entity offsets
        from telegram_entities import _utf16_len
        header_len = _utf16_len(header_text)
        for entity in result_entities:
            entity['offset'] += header_len

        # Build footer
        footer_mb = MessageBuilder()
        footer_mb.text("📊 剩餘: ").text(remaining_info).newline()
        footer_mb.text("🆔 ").code(grant_short)
        footer_text, footer_entities = footer_mb.build()

        # Adjust footer entity offsets
        footer_offset = _utf16_len(header_text + formatted_result + "\n")
        for entity in footer_entities:
            entity['offset'] += footer_offset

        # Combine everything
        text = header_text + formatted_result + "\n" + footer_text
        entities = header_entities + result_entities + footer_entities

        keyboard = {
            'inline_keyboard': [[
                {'text': '🛑 Revoke Grant', 'callback_data': f'grant_revoke:{grant_id}', 'style': 'danger'}
            ]]
        }

        # Safety net: Telegram 4096 char limit
        TELEGRAM_MAX_TEXT = 4096
        if len(text) > TELEGRAM_MAX_TEXT:
            # Truncate result text to fit within limit
            from telegram_entities import _utf16_len
            truncation_notice = "\n\n[輸出已截斷，超過 Telegram 4096 字元限制]"
            available = TELEGRAM_MAX_TEXT - len(header_text) - len("\n") - len(footer_text) - len(truncation_notice)
            if available > 0:
                # Save old footer offset to recalculate entity positions
                old_footer_offset = _utf16_len(header_text + formatted_result + "\n")
                # Truncate the formatted result
                formatted_result = formatted_result[:available] + truncation_notice
                # Rebuild text with truncated result
                text = header_text + formatted_result + "\n" + footer_text
                # Re-compute result entities for truncated text
                result_entities, _ = format_command_output(formatted_result)
                header_len = _utf16_len(header_text)
                for entity in result_entities:
                    entity['offset'] += header_len
                # Recalculate footer entity offsets
                new_footer_offset = _utf16_len(header_text + formatted_result + "\n")
                for entity in footer_entities:
                    # Extract relative offset within footer, then apply new footer offset
                    relative_offset = entity['offset'] - old_footer_offset
                    entity['offset'] = new_footer_offset + relative_offset
                entities = header_entities + result_entities + footer_entities
            else:
                # Even header+footer is too long, truncate everything
                text = text[:TELEGRAM_MAX_TEXT - len(truncation_notice)] + truncation_notice
                entities = []

        _telegram.send_message_with_entities(text, entities, reply_markup=keyboard, silent=True)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception(f"[GRANT] send_grant_execute_notification error: {e}", extra={"src_module": "grant", "operation": "send_grant_execute_notification", "error": str(e)})

def send_grant_complete_notification(grant_id: str, reason: str) -> None:
    """發送 Grant Session 完成/過期通知（entities 模式，無 parse_mode）"""
    try:
        grant_short = grant_id[:20] + '...' if len(grant_id) > 20 else grant_id

        mb = MessageBuilder()
        mb.text("🔑 ").bold("Grant 已結束").newline(2)
        mb.text("🆔 ").code(grant_short).newline()
        mb.text("💬 ").bold("原因：").text(f" {reason or ''}")

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities, silent=True)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception(f"[GRANT] send_grant_complete_notification error: {e}", extra={"src_module": "grant", "operation": "send_grant_complete_notification", "error": str(e)})

def send_blocked_notification(
    command: str,
    block_reason: str,
    source: str = '',
) -> None:
    """發送命令被封鎖的靜默通知（entities 模式，無 parse_mode）"""
    try:
        cmd_preview = command[:100] + '...' if len(command) > 100 else command

        mb = MessageBuilder()
        mb.text("🚫 ").bold("命令被封鎖").newline(2)
        mb.text("📋 ").code(cmd_preview).newline()
        mb.text("❌ ").bold("原因：").text(f" {block_reason}").newline()
        mb.text("🤖 ").bold("來源：").text(f" {source or 'Unknown'}")

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities, silent=True)

    except Exception as e:  # noqa: BLE001 — fire-and-forget
        logger.exception("send_blocked_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_blocked_notification", "error": str(e)})

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
    """發送 Trust Upload 自動批准的靜默通知（entities 模式，無 parse_mode）"""
    try:
        size_str = format_size_human(content_size)
        hash_short = sha256_hash[:16] if sha256_hash != 'batch' else 'batch'

        mb = MessageBuilder()
        mb.text("📤 ").bold("信任上傳").text(" (自動)").newline()
        mb.text("📁 ").code(filename or '').newline()
        mb.text(f"📊 {size_str} | SHA256: ").code(hash_short).newline()
        mb.text(f"📈 上傳: {upload_count}/{max_uploads}")
        if source:
            mb.newline().text(f"🤖 {source}")
        mb.newline().text("🔑 ").code(trust_id)

        text, entities = mb.build()

        keyboard = {
            'inline_keyboard': [[
                {'text': '🛑 End Trust', 'callback_data': f'revoke_trust:{trust_id}', 'style': 'danger'}
            ]]
        }

        _telegram.send_message_with_entities(text, entities, reply_markup=keyboard, silent=True)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception("send_trust_upload_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_trust_upload_notification", "error": str(e)})


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

        # Format extension groups
        ext_parts = []
        for ext, count in sorted(ext_counts.items()):
            ext_parts.append(f"{ext}: {count}")
        ext_line = ', '.join(ext_parts)

        # Timeout display
        timeout_val = timeout if timeout is not None else UPLOAD_TIMEOUT
        if timeout_val < 60:
            timeout_str = f"{timeout_val} 秒"
        elif timeout_val < 3600:
            timeout_str = f"{timeout_val // 60} 分鐘"
        else:
            timeout_str = f"{timeout_val // 3600} 小時"

        mb = MessageBuilder()
        mb.text("📁 ").bold("批量上傳請求").newline(2)
        mb.text("🤖 ").bold("來源：").text(f" {source or 'Unknown'}").newline()
        mb.text("💬 ").bold("原因：").text(f" {reason or ''}").newline()
        if account_name:
            mb.text("🏦 ").bold("帳號：").text(f" {account_name}").newline()
        mb.newline()
        mb.text("📄 ").bold(f"{file_count} 個檔案").text(f" ({size_str})").newline()
        mb.text(f"📊 {ext_line}").newline(2)
        mb.text("🆔 ").code(batch_id).newline()
        mb.text("⏰ ").bold(f"{timeout_str}後過期")

        text, entities = mb.build()

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

        result = _telegram.send_message_with_entities(text, entities, reply_markup=keyboard)
        ok = bool(result and result.get('ok'))
        message_id: Optional[int] = None
        if ok:
            message_id = result.get('result', {}).get('message_id')
        return NotificationResult(ok=ok, message_id=message_id)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception("send_batch_upload_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_batch_upload_notification", "error": str(e)})
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
    """發送 Presigned URL 生成的靜默通知（單檔）。（entities 模式，無 parse_mode）

    ❌ 絕對不含 presigned URL 本身。
    """
    try:
        mb = MessageBuilder()
        mb.text("📎 ").bold("Presigned URL 已生成").newline()
        mb.text("來源：").text(source or 'Unknown').newline()
        mb.text("檔案：").code(filename or '').newline()
        mb.text("帳號：").code(account_id or '').newline()
        mb.text("過期：").code(expires_at or '')

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities, silent=True)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception("send_presigned_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_presigned_notification", "error": str(e)})


def send_presigned_batch_notification(
    source: str,
    count: int,
    account_id: str,
    expires_at: str,
) -> None:
    """發送 Presigned URL Batch 生成的靜默通知。（entities 模式，無 parse_mode）

    ❌ 絕對不含任何 presigned URL。
    """
    try:
        mb = MessageBuilder()
        mb.text("📎 ").bold("Presigned URL Batch 已生成").newline()
        mb.text("來源：").text(source or 'Unknown').newline()
        mb.text(f"檔案數：{count} 個").newline()
        mb.text("帳號：").code(account_id or '').newline()
        mb.text("過期：").code(expires_at or '')

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities, silent=True)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception("send_presigned_batch_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_presigned_batch_notification", "error": str(e)})


# ============================================================================
# Trust Session Summary (sprint9-007-phase-a)
# =====================================================================

def send_trust_session_summary(trust_item: dict, end_reason: str = 'revoke') -> None:
    """Send a Telegram summary when a trust session ends (revoke or expiry).（entities 模式，無 parse_mode）

    Formats a message listing all commands executed during the trust session,
    with success/failure counts and truncation for large sessions.

    Args:
        trust_item: DynamoDB trust session item (may contain commands_executed list)
        end_reason: 'revoke' (manual revoke) or 'expiry' (auto expiry via scheduler)
    """
    try:
        commands_executed = trust_item.get('commands_executed', [])
        trust_id_short = str(trust_item.get('request_id', ''))[-12:]

        if end_reason == 'expiry':
            header_text = "信任時段結束（自動到期）"
        else:
            header_text = "信任時段結束（手動撤銷）"

        mb = MessageBuilder()

        # No commands — send brief notification
        if not commands_executed:
            mb.text("🔓 ").bold(header_text).newline(2)
            mb.text("🆔 ").code(trust_id_short).newline()
            mb.text("📋 無命令執行")
            text, entities = mb.build()
            _telegram.send_message_with_entities(text, entities, silent=True)
            return

        # Calculate duration
        import time as _t
        created_at = int(trust_item.get('created_at', 0))
        duration_secs = int(_t.time()) - created_at if created_at else 0
        duration_mins = duration_secs // 60
        duration_sec_part = duration_secs % 60
        duration_str = f"{duration_mins} 分 {duration_sec_part} 秒"

        # Count failures
        total = len(commands_executed)
        fail_count = sum(1 for e in commands_executed if not e.get('success', True))

        # Build command list (max 10 shown)
        display_limit = 10
        display_cmds = commands_executed[:display_limit]
        truncated = total > display_limit

        mb.text("🔓 ").bold(header_text).newline(2)
        mb.text("🆔 ").code(trust_id_short).newline()
        mb.text(f"⏱ 時長：{duration_str}").newline()
        mb.text(f"📋 執行了 {total} 個命令：").newline()

        for i, entry in enumerate(display_cmds, start=1):
            cmd = entry.get('cmd', '')[:80]
            ok_icon = "✅" if entry.get('success', True) else "❌"
            mb.text(f"  {i}. {ok_icon} ").code(cmd).newline()

        if truncated:
            mb.text(f"  ...還有 {total - display_limit} 個命令").newline()

        mb.newline()
        if fail_count == 0:
            mb.text("✅ 全部成功")
        else:
            mb.text(f"⚠️ {fail_count} 個失敗（請查看 CloudWatch Logs）")

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities, silent=True)

    except Exception as exc:  # noqa: BLE001 — fire-and-forget
        logger.exception('send_trust_session_summary error: %s', exc, extra={"src_module": "notifications", "operation": "send_trust_session_summary", "error": str(exc)})
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
    """Send a Telegram approval request for a frontend deployment.（entities 模式，無 parse_mode）

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
        total_size = sum(int(f.get("size", 0)) for f in files_summary)
        total_size_str = format_size_human(total_size)
        file_count = len(files_summary)

        mb = MessageBuilder()
        mb.text("🚀 ").bold("前端部署請求").newline(2)
        mb.text("📦 ").bold("專案：").text(f" {project or 'unknown'}").newline()
        mb.text("🗂 ").bold("目標 Bucket：").text(" ").code(target_info.get("frontend_bucket", "")).newline()
        mb.text("☁️ ").bold("CloudFront：").text(" ").code(target_info.get("distribution_id", "")).newline()
        mb.text("📁 ").bold(f"檔案（{file_count} 個，{total_size_str}）：").newline()

        for f in files_summary[:10]:
            fname = f.get("filename", "?")
            fsize = format_size_human(int(f.get("size", 0)))
            cc = f.get("cache_control", "")
            if "immutable" in cc:
                cc_short = "immutable"
            elif "no-store" in cc:
                cc_short = "no-cache"
            else:
                cc_short = "no-cache"
            mb.text("  • ").code(fname).text(f" ({fsize}) → {cc_short}").newline()

        if file_count > 10:
            mb.text(f"  ...還有 {file_count - 10} 個檔案").newline()

        mb.newline()
        mb.text("🤖 ").bold("來源：").text(f" {source or 'Unknown'}").newline()
        mb.text("💬 ").bold("原因：").text(f" {reason or '未提供原因'}").newline(2)
        mb.text("🆔 ").bold("ID：").text(" ").code(request_id).newline()
        mb.text("⏰ ").bold(f"{UPLOAD_TIMEOUT // 60} 分鐘後過期")

        text, entities = mb.build()

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 批准部署", "callback_data": f"approve:{request_id}", "style": "success"},
                    {"text": "❌ 拒絕", "callback_data": f"deny:{request_id}", "style": "danger"},
                ],
            ]
        }

        result = _telegram.send_message_with_entities(text, entities, reply_markup=keyboard)
        ok = bool(result and result.get("ok"))
        message_id = None
        if ok:
            message_id = result.get("result", {}).get("message_id")
        return NotificationResult(ok=ok, message_id=message_id)

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as exc:
        logger.exception("send_deploy_frontend_notification error: %s", exc, extra={"src_module": "notifications", "operation": "send_deploy_frontend_notification", "error": str(exc)})
        return NotificationResult(ok=False, message_id=None)


# ============================================================================
# Deploy Auto-Approve Notification (sprint32-001b)
# ============================================================================

def send_auto_approve_deploy_notification(
    project_id: str,
    deploy_id: str,
    source: Optional[str] = None,
    reason: str = '',
    changes_summary: str = '',
) -> None:
    """自動批准 deploy 的靜默通知（Steven 知道但不需要互動）"""
    try:
        mb = MessageBuilder()
        mb.text("🤖 ").bold("自動批准部署").newline(2)
        mb.text("📦 ").bold("專案：").text(f" {project_id}").newline()
        mb.text("🆔 ").bold("Deploy ID：").text(" ").code(deploy_id).newline()
        mb.text("🤖 ").bold("來源：").text(f" {source or 'auto-approve'}").newline()
        mb.text("📝 ").bold("原因：").text(f" {reason}").newline(2)
        mb.italic("純 code 變更，CFN changeset 分析通過")
        if changes_summary:
            mb.newline()
            mb.text("📋 ").bold("變更：").text(f" {changes_summary}")
        else:
            mb.newline()
            mb.italic("(無變更明細)")

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities, silent=True)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.warning(
            "send_auto_approve_deploy_notification failed",
            extra={
                "src_module": "notifications",
                "operation": "auto_approve_deploy_notification",
                "error": str(e),
            },
        )


# ============================================================================
# Approval Expiry Warning Notification (sprint35-003)
# ============================================================================

def send_expiry_warning_notification(request_id: str, command_preview: str, source: str = '') -> None:
    """Send ⏰ warning when approval request is about to expire (60s remaining).

    Args:
        request_id:       The Bouncer request ID (DynamoDB primary key).
        command_preview:  A preview of the command (first 100 chars).
        source:           Optional request source (e.g., 'ztp-files').
    """
    try:
        mb = MessageBuilder()
        mb.text("⏰ ").bold("審批請求即將過期").newline()
        mb.text("📋 ").code(request_id).newline()
        mb.text("💻 ").code(command_preview).newline()
        if source:
            mb.text(f"🤖 {source}").newline()
        mb.text("請在 60 秒內審批，否則請求將自動過期。")

        text, entities = mb.build()
        _telegram.send_message_with_entities(text, entities)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.exception("send_expiry_warning_notification error: %s", e, extra={"src_module": "notifications", "operation": "send_expiry_warning_notification", "error": str(e)})
