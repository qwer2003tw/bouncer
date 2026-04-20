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

Sprint 92 (#272):
  - Refactored into multiple modules for SRP
  - notifications_core: core utilities
  - notifications_execute: execute-path notifications
  - notifications_grant: grant-related notifications
  - notifications.py (this file): remaining notifications + re-exports as public API
"""

# Re-export from sub-modules — notifications.py is the public API
# All existing `from notifications import X` and `patch('notifications.X')` continue to work
from notifications_core import (  # noqa: F401
    _last_notification_time,
    NotificationResult,
    _should_throttle_notification,
    _escape_markdown,
    _send_message,
    _send_message_silent,
    post_notification_setup,
    _store_notification_snapshot,
)
from notifications_execute import (  # noqa: F401
    send_approval_request,
    send_trust_auto_approve_notification,
    send_blocked_notification,
    send_expiry_warning_notification,
    send_auto_approve_deploy_notification,
)
from notifications_grant import (  # noqa: F401
    send_grant_request_notification,
    send_grant_execute_notification,
    send_grant_complete_notification,
    send_account_approval_request,
)

import urllib.error
from typing import Optional

from aws_lambda_powertools import Logger
import telegram as _telegram
from constants import UPLOAD_TIMEOUT
from telegram_entities import MessageBuilder
from utils import format_size_human

logger = Logger(service="bouncer")


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


# ============================================================================
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
