"""
Bouncer - Telegram API 模組
處理所有 Telegram 訊息發送、更新、callback 回應
"""
import json
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from constants import TELEGRAM_TOKEN, TELEGRAM_API_BASE, APPROVED_CHAT_ID
except ImportError:
    from src.constants import TELEGRAM_TOKEN, TELEGRAM_API_BASE, APPROVED_CHAT_ID

__all__ = [
    'escape_markdown',
    'send_telegram_message',
    'send_telegram_message_silent',
    'send_telegram_message_to',
    'update_message',
    'answer_callback',
    'update_and_answer',
    '_telegram_request',
    '_telegram_requests_parallel',
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
            print(f"[TIMING] Telegram {method}: {elapsed:.0f}ms")
            return result
    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        print(f"[TIMING] Telegram {method} error ({elapsed:.0f}ms): {e}")
        return {}


def escape_markdown(text: str) -> str:
    """轉義 Telegram Markdown 特殊字元"""
    if not text:
        return text
    for char in ['*', '_', '`', '[']:
        text = text.replace(char, '\\' + char)
    return text


def send_telegram_message(text: str, reply_markup: dict = None):
    """發送 Telegram 消息"""
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    _telegram_request('sendMessage', data)


def send_telegram_message_silent(text: str, reply_markup: dict = None):
    """發送靜默 Telegram 消息（不響鈴）"""
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_notification': True
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    _telegram_request('sendMessage', data)


def send_telegram_message_to(chat_id: str, text: str, parse_mode: str = None):
    """發送消息到指定 chat"""
    data = {
        'chat_id': chat_id,
        'text': text
    }
    if parse_mode:
        data['parse_mode'] = parse_mode
    _telegram_request('sendMessage', data)


def update_message(message_id: int, text: str):
    """更新 Telegram 消息"""
    data = {
        'chat_id': APPROVED_CHAT_ID,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    _telegram_request('editMessageText', data)


def answer_callback(callback_id: str, text: str):
    """回應 Telegram callback"""
    data = {
        'callback_query_id': callback_id,
        'text': text
    }
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
    print(f"[TIMING] update_and_answer parallel: {(time.time() - start_time) * 1000:.0f}ms")
