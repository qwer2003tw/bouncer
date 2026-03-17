"""
Bouncer - Grant Session Callback 處理模組

Grant 審批相關的 Telegram callback handlers
"""

import urllib.error
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from utils import response
from telegram import answer_callback, update_message


logger = Logger(service="bouncer")


def handle_grant_approve(query: dict, grant_id: str, mode: str = 'all') -> dict:
    """處理 Grant 批准 callback

    Args:
        query: Telegram callback query
        grant_id: Grant session ID
        mode: 'all' 全部批准 | 'safe_only' 只批准安全命令
    """
    from grant import approve_grant, get_grant_session

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    mode_label = '全部' if mode == 'all' else '僅安全'

    try:
        # Check if grant contains dangerous commands before approving
        show_alert = False
        if mode == 'all':
            grant_check = get_grant_session(grant_id)
            if grant_check:
                commands_detail = grant_check.get('commands_detail', [])
                has_dangerous = any(d.get('category') == 'requires_individual' for d in commands_detail)
                if has_dangerous:
                    show_alert = True

        grant = approve_grant(grant_id, user_id, mode=mode)
        if not grant:
            answer_callback(callback_id, '❌ Grant 不存在或已處理')
            return response(200, {'ok': True})

        granted = grant.get('granted_commands', [])
        ttl_minutes = grant.get('ttl_minutes', 30)

        cb_suffix = '命令' if mode == 'all' else '安全命令'

        if show_alert:
            answer_callback(callback_id, f'⚠️ 高危 Grant 確認：已批准 {len(granted)} 個{cb_suffix}', show_alert=True)
        else:
            answer_callback(callback_id, f'✅ 已批准 {len(granted)} 個{cb_suffix}')
        update_message(
            message_id,
            f"✅ *Grant 已批准（{mode_label}）*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"📋 *已授權命令：* {len(granted)} 個\n"
            f"⏱ *有效時間：* {ttl_minutes} 分鐘\n"
            f"👤 *批准者：* {user_id}",
        )

        return response(200, {'ok': True})

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
        logger.error(f"[GRANT] handle_grant_approve error (mode={mode}): {e}", extra={"src_module": "grant", "operation": "handle_grant_approve", "mode": mode, "error": str(e)})
        answer_callback(callback_id, f'❌ 批准失敗: {str(e)[:50]}')
        return response(500, {'error': str(e)})


# Backward-compatible aliases
def handle_grant_approve_all(query: dict, grant_id: str) -> dict:
    """處理 Grant 全部批准 callback"""
    return handle_grant_approve(query, grant_id, mode='all')


def handle_grant_approve_safe(query: dict, grant_id: str) -> dict:
    """處理 Grant 只批准安全命令 callback"""
    return handle_grant_approve(query, grant_id, mode='safe_only')


def handle_grant_deny(query: dict, grant_id: str) -> dict:
    """處理 Grant 拒絕 callback"""
    from grant import deny_grant

    callback_id = query.get('id', '')
    user_id = str(query.get('from', {}).get('id', ''))
    message_id = query.get('message', {}).get('message_id')

    try:
        success = deny_grant(grant_id)
        if not success:
            answer_callback(callback_id, '❌ 拒絕失敗')
            return response(200, {'ok': True})

        answer_callback(callback_id, '❌ 已拒絕')
        update_message(
            message_id,
            f"❌ *Grant 已拒絕*\n\n"
            f"🔑 *Grant ID：* `{grant_id}`\n"
            f"👤 *拒絕者：* {user_id}",
        )

        return response(200, {'ok': True})

    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
        logger.error(f"[GRANT] handle_grant_deny error: {e}", extra={"src_module": "grant", "operation": "handle_grant_deny", "error": str(e)})
        answer_callback(callback_id, f'❌ 處理失敗: {str(e)[:50]}')
        return response(500, {'error': str(e)})
