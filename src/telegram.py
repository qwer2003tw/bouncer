"""
Bouncer - Telegram API 模組
處理所有 Telegram 訊息發送、更新、callback 回應
"""
import json
import time
import urllib.error
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from aws_lambda_powertools import Logger

from constants import TELEGRAM_TOKEN, TELEGRAM_API_BASE, APPROVED_CHAT_ID

logger = Logger(service="bouncer")

__all__ = [
    'escape_markdown',
    'send_telegram_message',
    'send_telegram_message_silent',
    'send_telegram_message_to',
    'update_message',
    'answer_callback',
    'update_and_answer',
    'send_chat_action',
    'send_message_with_entities',
    '_telegram_request',
    '_telegram_requests_parallel',
    'pin_message',
    'unpin_message',
]


def _telegram_requests_parallel(requests: list) -> list:
    """並行發送多個 Telegram API 請求

    Args:
        requests: list of (method, data, timeout, json_body) tuples

    Returns:
        list of results in same order
    """
    if not requests:
        return []

    results = [None] * len(requests)

    def do_request(idx, method, data, timeout, json_body):
        return idx, _telegram_request(method, data, timeout, json_body)

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        futures = [
            executor.submit(do_request, i, method, data, timeout, json_body)
            for i, (method, data, timeout, json_body) in enumerate(requests)
        ]
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result

    return results


def _telegram_request(method: str, data: dict, timeout: int = 5, json_body: bool = False) -> dict:
    """統一的 Telegram API 請求函數

    Args:
        method: API 方法名（如 sendMessage, editMessageText）
        data: 請求資料
        timeout: 超時秒數
        json_body: True 時用 JSON 格式發送

    Returns:
        API 回應或空 dict
    """
    if not TELEGRAM_TOKEN:
        return {}

    url = f"{TELEGRAM_API_BASE}{TELEGRAM_TOKEN}/{method}"
    start_time = time.time()

    try:
        if json_body:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
        else:
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(data).encode(),
                method='POST'
            )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            result = json.loads(resp.read().decode())
            elapsed = (time.time() - start_time) * 1000
            logger.debug("Telegram %s: %.0fms", method, elapsed, extra={"src_module": "telegram", "operation": "call_api", "method": method, "elapsed_ms": elapsed})
            return result
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, json.JSONDecodeError, ValueError) as e:
        elapsed = (time.time() - start_time) * 1000
        logger.debug("Telegram %s error (%.0fms): %s", method, elapsed, e, extra={"src_module": "telegram", "operation": "call_api", "method": method, "elapsed_ms": elapsed, "error": str(e)})

        # Fallback: if sendMessage fails with Markdown, retry without parse_mode
        if method == 'sendMessage' and 'parse_mode' in data and '400' in str(e):
            logger.info("Retrying %s without parse_mode", method, extra={"src_module": "telegram", "operation": "call_api_fallback", "method": method})
            fallback_data = {k: v for k, v in data.items() if k != 'parse_mode'}
            try:
                if json_body:
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(fallback_data).encode(),
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                else:
                    req = urllib.request.Request(
                        url,
                        data=urllib.parse.urlencode(fallback_data).encode(),
                        method='POST'
                    )
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
                    result = json.loads(resp.read().decode())
                    elapsed2 = (time.time() - start_time) * 1000
                    logger.debug("Telegram %s fallback OK (%.0fms)", method, elapsed2, extra={"src_module": "telegram", "operation": "call_api_fallback", "method": method, "elapsed_ms": elapsed2})
                    return result
            except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, json.JSONDecodeError, ValueError) as e2:
                logger.debug("Telegram %s fallback also failed: %s", method, e2, extra={"src_module": "telegram", "operation": "call_api_fallback", "method": method, "error": str(e2)})

        return {}


def escape_markdown(text: str) -> str:
    """轉義 Telegram Markdown V1 特殊字元

    根據 Telegram Bot API 官方文件：
    "To escape characters '_', '*', '`', '[' outside of an entity,
     prepend the character '\\' before them."

    See: https://core.telegram.org/bots/api#markdown-style
    """
    if not text:
        return text
    for char in ['\\', '*', '_', '`', '[']:
        text = text.replace(char, '\\' + char)
    return text


KNOWN_BUTTON_FIELDS = {
    'text', 'callback_data', 'url', 'style',
    'login_url', 'switch_inline_query', 'switch_inline_query_current_chat',
    'switch_inline_query_chosen_chat', 'pay', 'icon_custom_emoji_id',
    'web_app', 'callback_game', 'copy_text', 'icon_color',
}


def _strip_unsupported_button_fields(keyboard: dict) -> dict:
    """Remove fields not supported by Telegram API using a whitelist.

    Note: 'style' is intentionally preserved — supported since Telegram Bot API 9.4.
    See: https://core.telegram.org/bots/api#inlinekeyboardbutton
    """
    if not keyboard:
        return keyboard
    result = dict(keyboard)
    if 'inline_keyboard' in result:
        result['inline_keyboard'] = [
            [{k: v for k, v in btn.items() if k in KNOWN_BUTTON_FIELDS} for btn in row]
            for row in result['inline_keyboard']
        ]
    return result


def send_telegram_message(text: str, reply_markup: dict = None) -> dict:
    """發送 Telegram 消息

    Returns:
        API 回應 dict（成功時含 'ok': True，失敗時為空 dict {}）
    """
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        data['reply_markup'] = _strip_unsupported_button_fields(reply_markup)
    return _telegram_request('sendMessage', data, json_body=True)


def send_telegram_message_silent(text: str, reply_markup: dict = None):
    """發送靜默 Telegram 消息（不響鈴）"""
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_notification': True
    }
    if reply_markup:
        data['reply_markup'] = _strip_unsupported_button_fields(reply_markup)
    _telegram_request('sendMessage', data, json_body=True)


def send_telegram_message_to(chat_id: str, text: str, parse_mode: str = None):
    """發送消息到指定 chat"""
    data = {
        'chat_id': chat_id,
        'text': text
    }
    if parse_mode:
        data['parse_mode'] = parse_mode
    _telegram_request('sendMessage', data, timeout=10, json_body=True)


def update_message(message_id: int, text: str, remove_buttons: bool = False):
    """更新 Telegram 消息

    Args:
        message_id: 訊息 ID
        text: 新的訊息內容
        remove_buttons: 是否移除按鈕
    """
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if remove_buttons:
        data['reply_markup'] = {'inline_keyboard': []}
    _telegram_request('editMessageText', data, json_body=True)


def answer_callback(callback_id: str, text: str, show_alert: bool = False):
    """回應 Telegram callback

    Args:
        callback_id: Telegram callback query ID
        text: 顯示文字（toast 或 alert）
        show_alert: True → 顯示模態 alert popup（使用者需主動關閉）
                    False → 顯示普通 toast notification（預設）
    """
    data = {
        'callback_query_id': callback_id,
        'text': text,
    }
    if show_alert:
        data['show_alert'] = True
    _telegram_request('answerCallbackQuery', data)


def update_and_answer(message_id: int, text: str, callback_id: str, callback_text: str):
    """並行更新訊息 + 回應 callback（省約 500ms）"""
    requests = [
        ('editMessageText', {
            'chat_id': APPROVED_CHAT_ID,
            'message_id': message_id,
            'text': text,
            'parse_mode': 'Markdown'
        }, 5, False),
        ('answerCallbackQuery', {
            'callback_query_id': callback_id,
            'text': callback_text
        }, 5, False)
    ]
    start_time = time.time()
    _telegram_requests_parallel(requests)
    logger.debug("update_and_answer parallel: %.0fms", (time.time() - start_time) * 1000, extra={"src_module": "telegram", "operation": "update_and_answer"})


def send_chat_action(action: str = 'typing') -> None:
    """發送 typing/upload 等狀態給 Telegram（fire-and-forget，失敗不影響主流程）

    Args:
        action: Telegram chat action (預設 'typing')
    """
    try:
        _telegram_request('sendChatAction', {
            'chat_id': APPROVED_CHAT_ID,
            'action': action,
        })
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.debug('send_chat_action ignored error: %s', e, extra={"src_module": "telegram", "operation": "send_chat_action", "error": str(e)})


def send_message_with_entities(text: str, entities: list, reply_markup: dict = None, silent: bool = False) -> dict:
    """Send a Telegram message using entities mode (no parse_mode).

    Uses Telegram's entities API instead of parse_mode='Markdown', which
    avoids all Markdown escape issues. Offsets/lengths must be in UTF-16
    code units (see telegram_entities.py for correct calculation).

    Args:
        text:         Plain text content of the message.
        entities:     List of Telegram entity dicts with type/offset/length.
        reply_markup: Optional inline keyboard or reply keyboard markup.
        silent:       If True, sends without notification sound.

    Returns:
        Telegram API response dict.
    """
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'entities': entities,
    }
    if silent:
        data['disable_notification'] = True
    if reply_markup:
        data['reply_markup'] = _strip_unsupported_button_fields(reply_markup)
    return _telegram_request('sendMessage', data, json_body=True)


def pin_message(message_id: int, disable_notification: bool = True) -> bool:
    """Pin a message in the approved chat. Returns True on success, False on failure (best-effort).

    Args:
        message_id: Telegram message ID to pin
        disable_notification: If True, pin silently without notifying users (default: True)

    Returns:
        True if successful, False on failure (best-effort)
    """
    try:
        result = _telegram_request('pinChatMessage', {
            'chat_id': APPROVED_CHAT_ID,
            'message_id': message_id,
            'disable_notification': disable_notification,
        })
        return result.get('ok', False)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ValueError, RuntimeError) as e:
        logger.warning("Failed to pin message %s (ignored): %s", message_id, e, extra={"src_module": "telegram", "operation": "pin_message", "message_id": message_id, "error": str(e)})
        return False


def unpin_message(message_id: int) -> bool:
    """Unpin a message in the approved chat. Returns True on success, False on failure (best-effort).

    Args:
        message_id: Telegram message ID to unpin

    Returns:
        True if successful, False on failure (best-effort)
    """
    try:
        result = _telegram_request('unpinChatMessage', {
            'chat_id': APPROVED_CHAT_ID,
            'message_id': message_id,
        })
        return result.get('ok', False)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ValueError, RuntimeError) as e:
        logger.warning("Failed to unpin message %s (ignored): %s", message_id, e, extra={"src_module": "telegram", "operation": "unpin_message", "message_id": message_id, "error": str(e)})
        return False
