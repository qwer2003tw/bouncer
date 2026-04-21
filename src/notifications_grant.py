"""Grant and account management notification functions.

Extracted from notifications.py (Sprint 92, #272).
Contains notifications for:
- Grant request/execute/complete
- Account approval requests
"""

import time
import urllib.error

from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger
import telegram as _telegram
from constants import GRANT_APPROVAL_TIMEOUT
from telegram_entities import MessageBuilder, format_command_output
from utils import extract_exit_code

from notifications_core import post_notification_setup
from telegram_entities import _utf16_len  # noqa: E402

logger = Logger(service="bouncer")


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
