"""Execute-path notification functions.

Extracted from notifications.py (Sprint 92, #272).
Contains notifications for:
- Command approval requests
- Trust auto-approve
- Blocked commands
- Expiry warnings
- Auto-approve deploy notifications
"""

import datetime
import time
import urllib.error
from typing import Optional

from aws_lambda_powertools import Logger
import telegram as _telegram
from commands import is_dangerous, check_lambda_env_update
from constants import COMMAND_APPROVAL_TIMEOUT, TRUST_SESSION_MAX_COMMANDS, DEFAULT_ACCOUNT_ID
from trust import is_trust_excluded
from telegram_entities import MessageBuilder, format_command_output
from utils import extract_exit_code

from notifications_core import (
    NotificationResult,
    _should_throttle_notification,
    _store_notification_snapshot,
)

logger = Logger(service="bouncer")


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
