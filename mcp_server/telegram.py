"""
Bouncer MCP Server - Telegram Integration
Long polling 版本（非 webhook）
"""

import json
import logging
import time
import threading
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    """Telegram 配置"""
    bot_token: str
    chat_id: str  # Steven 的 chat ID，用於發送審批請求
    polling_interval: float = 1.0  # 輪詢間隔（秒）
    request_timeout: float = 10.0  # API 請求超時


class TelegramClient:
    """Telegram Bot API Client"""

    def __init__(self, config: TelegramConfig):
        self.config = config
        self._base_url = f"https://api.telegram.org/bot{config.bot_token}"

    def _request(
        self,
        method: str,
        data: Optional[Dict] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """發送 API 請求"""
        url = f"{self._base_url}/{method}"
        timeout = timeout or self.config.request_timeout

        if data:
            # URL encode the data
            encoded_data = urllib.parse.urlencode(data).encode('utf-8')
            req = urllib.request.Request(url, data=encoded_data, method='POST')
        else:
            req = urllib.request.Request(url)

        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            return {'ok': False, 'error': error_body}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def send_approval_request(
        self,
        request_id: str,
        command: str,
        reason: str,
        timeout_seconds: int = 300
    ) -> Optional[int]:
        """
        發送審批請求

        Returns:
            message_id if successful, None otherwise
        """
        # 截斷過長的命令
        cmd_preview = command if len(command) <= 500 else command[:500] + '...'
        timeout_min = timeout_seconds // 60

        text = (
            f"🔐 *AWS 執行請求*\n\n"
            f"📋 *命令：*\n`{cmd_preview}`\n\n"
            f"💬 *原因：* {reason}\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"⏰ *{timeout_min} 分鐘後過期*"
        )

        keyboard = {
            'inline_keyboard': [[
                {'text': '✅ 批准執行', 'callback_data': f'approve:{request_id}'},
                {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
            ]]
        }

        result = self._request('sendMessage', {
            'chat_id': self.config.chat_id,
            'text': text,
            'parse_mode': 'Markdown',
            'reply_markup': json.dumps(keyboard)
        })

        if result.get('ok'):
            return result.get('result', {}).get('message_id')
        else:
            logger.error(f"[Telegram] Failed to send message: {result.get('error')}")
            return None

    def update_message(
        self,
        message_id: int,
        text: str
    ) -> bool:
        """更新消息內容"""
        result = self._request('editMessageText', {
            'chat_id': self.config.chat_id,
            'message_id': message_id,
            'text': text,
            'parse_mode': 'Markdown'
        })
        return result.get('ok', False)

    def answer_callback(
        self,
        callback_id: str,
        text: str
    ) -> bool:
        """回應 callback query"""
        result = self._request('answerCallbackQuery', {
            'callback_query_id': callback_id,
            'text': text
        })
        return result.get('ok', False)

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = 30
    ) -> list:
        """
        Long polling 取得更新

        Args:
            offset: 從這個 update_id 之後開始
            timeout: long polling 超時（秒）

        Returns:
            List of updates
        """
        params = {
            'timeout': timeout,
            'allowed_updates': json.dumps(['callback_query'])
        }
        if offset:
            params['offset'] = offset

        result = self._request('getUpdates', params, timeout=timeout + 5)

        if result.get('ok'):
            return result.get('result', [])
        return []


class TelegramPoller:
    """
    Telegram Long Polling 背景執行緒

    持續輪詢 Telegram API，收到 callback 時通知等待中的請求
    """

    def __init__(
        self,
        client: TelegramClient,
        on_approval: Callable[[str, str, str], None],  # (request_id, action, user_id)
        authorized_user_id: str
    ):
        self.client = client
        self.on_approval = on_approval
        self.authorized_user_id = authorized_user_id

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_update_id: Optional[int] = None

    def start(self):
        """啟動 polling 執行緒"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("[TelegramPoller] Started")

    def stop(self):
        """停止 polling 執行緒"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[TelegramPoller] Stopped")

    def _poll_loop(self):
        """Polling 主迴圈"""
        while self._running:
            try:
                updates = self.client.get_updates(
                    offset=self._last_update_id,
                    timeout=30
                )

                for update in updates:
                    self._handle_update(update)
                    # 更新 offset 以避免重複處理
                    self._last_update_id = update.get('update_id', 0) + 1

            except Exception as e:
                logger.error(f"[TelegramPoller] Error: {e}")
                time.sleep(5)  # 錯誤後等待再重試

    def _handle_update(self, update: Dict):
        """處理單一 update"""
        callback = update.get('callback_query')
        if not callback:
            return

        # 驗證使用者
        user_id = str(callback.get('from', {}).get('id', ''))
        if user_id != self.authorized_user_id:
            self.client.answer_callback(
                callback['id'],
                '❌ 你沒有審批權限'
            )
            return

        # 解析 callback data
        data = callback.get('data', '')
        if ':' not in data:
            return

        action, request_id = data.split(':', 1)

        if action in ('approve', 'deny'):
            # 先回應 callback（避免 Telegram 顯示 loading）
            self.client.answer_callback(
                callback['id'],
                '✅ 處理中...' if action == 'approve' else '❌ 已拒絕'
            )

            # 通知等待中的請求
            self.on_approval(request_id, action, user_id)


# ============================================================================
# 等待機制
# ============================================================================

class ApprovalWaiter:
    """
    等待審批結果的同步機制

    Tool thread 呼叫 wait() 會 block，
    Polling thread 收到 callback 時呼叫 notify() 解除等待
    """

    def __init__(self):
        self._events: Dict[str, threading.Event] = {}
        self._results: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def register(self, request_id: str):
        """註冊一個等待中的請求"""
        with self._lock:
            self._events[request_id] = threading.Event()

    def wait(self, request_id: str, timeout: float) -> Optional[Dict]:
        """
        等待審批結果

        Returns:
            {'action': 'approve'|'deny', 'user_id': str} or None if timeout
        """
        event = self._events.get(request_id)
        if not event:
            return None

        if event.wait(timeout=timeout):
            with self._lock:
                return self._results.pop(request_id, None)
        return None

    def notify(self, request_id: str, action: str, user_id: str):
        """通知等待中的請求"""
        with self._lock:
            self._results[request_id] = {
                'action': action,
                'user_id': user_id
            }
            event = self._events.get(request_id)
            if event:
                event.set()

    def cleanup(self, request_id: str):
        """清理已完成的請求"""
        with self._lock:
            self._events.pop(request_id, None)
            self._results.pop(request_id, None)
