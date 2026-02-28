#!/usr/bin/env python3
"""
Bouncer MCP Server
AWS 命令審批執行系統 - stdio MCP Server 版本

使用方式：
    python -m mcp_server.server

環境變數：
    BOUNCER_TELEGRAM_TOKEN - Telegram Bot Token
    BOUNCER_CHAT_ID - 審批者的 Telegram Chat ID
    BOUNCER_CREDENTIALS_FILE - AWS credentials 檔案路徑（可選）
    BOUNCER_DB_PATH - SQLite 資料庫路徑（可選）
"""

import logging
import os
import sys
import json
import time
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

from .db import get_db
from .classifier import classify_command, execute_command, get_safelist, get_blocked_patterns
from .telegram import (
    TelegramConfig,
    TelegramClient,
    TelegramPoller,
    ApprovalWaiter
)


# ============================================================================
# 版本與配置
# ============================================================================

VERSION = '1.0.0'
SERVER_NAME = 'bouncer'

# 預設配置
DEFAULT_TIMEOUT = 300  # 5 分鐘
MAX_TIMEOUT = 3600     # 1 小時（EC2 沒有 Lambda 的 15 分鐘限制）


# ============================================================================
# MCP Tool 定義
# ============================================================================

TOOLS = [
    {
        'name': 'bouncer_execute',
        'description': '執行 AWS CLI 命令。安全命令（describe/list/get）自動執行，危險命令需要 Telegram 審批。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：aws ec2 describe-instances）'
                },
                'reason': {
                    'type': 'string',
                    'description': '執行原因（用於審批記錄）',
                },
                'timeout': {
                    'type': 'integer',
                    'description': f'審批等待超時（秒），預設 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}',
                }
            },
            'required': ['command']
        }
    },
    {
        'name': 'bouncer_status',
        'description': '查詢審批請求的狀態',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'request_id': {
                    'type': 'string',
                    'description': '請求 ID'
                }
            },
            'required': ['request_id']
        }
    },
    {
        'name': 'bouncer_list_rules',
        'description': '列出命令分類規則（safelist 前綴和 blocked patterns）',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_stats',
        'description': '取得審批統計資訊',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    }
]


# ============================================================================
# MCP Server
# ============================================================================

class BouncerMCPServer:
    """Bouncer MCP Server - stdio 版本"""

    def __init__(self):
        # 載入配置
        self.telegram_token = os.environ.get('BOUNCER_TELEGRAM_TOKEN', '')
        self.chat_id = os.environ.get('BOUNCER_CHAT_ID', '')
        self.credentials_file = os.environ.get('BOUNCER_CREDENTIALS_FILE')

        db_path = os.environ.get('BOUNCER_DB_PATH')
        self.db = get_db(Path(db_path) if db_path else None)

        # Telegram 整合
        self.telegram_client: Optional[TelegramClient] = None
        self.telegram_poller: Optional[TelegramPoller] = None
        self.approval_waiter = ApprovalWaiter()

        if self.telegram_token and self.chat_id:
            self._init_telegram()
        else:
            logger.warning("[Bouncer] Warning: Telegram not configured, approval commands will timeout")

    def _init_telegram(self):
        """初始化 Telegram 整合"""
        config = TelegramConfig(
            bot_token=self.telegram_token,
            chat_id=self.chat_id
        )
        self.telegram_client = TelegramClient(config)
        self.telegram_poller = TelegramPoller(
            client=self.telegram_client,
            on_approval=self._on_approval,
            authorized_user_id=self.chat_id
        )
        self.telegram_poller.start()

    def _on_approval(self, request_id: str, action: str, user_id: str):
        """Telegram callback 處理"""
        # 更新資料庫
        if action == 'approve':
            # 先標記為 approved，等待執行
            request = self.db.get_request(request_id)
            if request and request['status'] == 'pending':
                # 執行命令
                output, exit_code = execute_command(
                    request['command'],
                    credentials_file=self.credentials_file
                )

                self.db.update_request(
                    request_id,
                    status='approved',
                    result=output,
                    exit_code=exit_code,
                    approved_by=user_id
                )

                # 更新 Telegram 消息
                if self.telegram_client and request.get('telegram_message_id'):
                    result_preview = output[:1500] if len(output) > 1500 else output
                    self.telegram_client.update_message(
                        request['telegram_message_id'],
                        f"✅ 已批准並執行\n\n"
                        f"📋 命令：\n`{request['command']}`\n\n"
                        f"📤 結果：\n```\n{result_preview}\n```"
                    )

                self.db.log_action(request_id, 'approved', user_id)
                self.db.log_action(request_id, 'executed', 'system', {
                    'exit_code': exit_code,
                    'output_length': len(output)
                })

        elif action == 'deny':
            request = self.db.get_request(request_id)
            if request and request['status'] == 'pending':
                self.db.update_request(
                    request_id,
                    status='denied',
                    approved_by=user_id
                )

                # 更新 Telegram 消息
                if self.telegram_client and request.get('telegram_message_id'):
                    self.telegram_client.update_message(
                        request['telegram_message_id'],
                        f"❌ 已拒絕\n\n📋 命令：\n`{request['command']}`"
                    )

                self.db.log_action(request_id, 'denied', user_id)

        # 通知等待中的 thread
        self.approval_waiter.notify(request_id, action, user_id)

    def run(self):
        """主迴圈 - 讀取 stdin，處理 JSON-RPC，寫入 stdout"""
        logger.info(f"[Bouncer] MCP Server v{VERSION} started")

        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                    response = self._handle_request(request)
                    self._write_response(response)
                except json.JSONDecodeError as e:
                    self._write_response(self._error(None, -32700, f'Parse error: {e}'))
                except Exception as e:
                    logger.error(f"[Bouncer] Error: {e}")
                    self._write_response(self._error(None, -32603, f'Internal error: {e}'))

        finally:
            if self.telegram_poller:
                self.telegram_poller.stop()

    def _write_response(self, response: Dict):
        """寫入 JSON-RPC response 到 stdout"""
        print(json.dumps(response), flush=True)

    def _handle_request(self, request: Dict) -> Dict:
        """處理 JSON-RPC 請求"""
        jsonrpc = request.get('jsonrpc')
        method = request.get('method', '')
        params = request.get('params', {})
        req_id = request.get('id')

        if jsonrpc != '2.0':
            return self._error(req_id, -32600, 'Invalid Request: jsonrpc must be "2.0"')

        # MCP 標準方法
        if method == 'initialize':
            return self._result(req_id, {
                'protocolVersion': '2024-11-05',
                'serverInfo': {
                    'name': SERVER_NAME,
                    'version': VERSION
                },
                'capabilities': {
                    'tools': {}
                }
            })

        elif method == 'notifications/initialized':
            # Client 確認初始化完成，不需要回應
            return self._result(req_id, {})

        elif method == 'tools/list':
            return self._result(req_id, {'tools': TOOLS})

        elif method == 'tools/call':
            tool_name = params.get('name', '')
            arguments = params.get('arguments', {})
            return self._handle_tool_call(req_id, tool_name, arguments)

        else:
            return self._error(req_id, -32601, f'Method not found: {method}')

    def _handle_tool_call(self, req_id, tool_name: str, arguments: Dict) -> Dict:
        """處理 tool 呼叫"""

        if tool_name == 'bouncer_execute':
            return self._tool_execute(req_id, arguments)

        elif tool_name == 'bouncer_status':
            return self._tool_status(req_id, arguments)

        elif tool_name == 'bouncer_list_rules':
            return self._tool_list_rules(req_id)

        elif tool_name == 'bouncer_stats':
            return self._tool_stats(req_id)

        else:
            return self._error(req_id, -32602, f'Unknown tool: {tool_name}')

    # =========================================================================
    # Tool Implementations
    # =========================================================================

    def _tool_execute(self, req_id, arguments: Dict) -> Dict:
        """bouncer_execute tool"""
        command = arguments.get('command', '').strip()
        reason = arguments.get('reason', 'No reason provided')
        timeout = min(arguments.get('timeout', DEFAULT_TIMEOUT), MAX_TIMEOUT)

        if not command:
            return self._tool_error(req_id, 'Missing required parameter: command')

        # 分類命令
        classification = classify_command(command)

        # Layer 1: BLOCKED
        if classification == 'BLOCKED':
            return self._tool_result(req_id, {
                'status': 'blocked',
                'command': command,
                'classification': classification,
                'error': 'Command blocked for security reasons'
            }, is_error=True)

        # Layer 2: SAFELIST（自動執行）
        if classification == 'SAFELIST':
            output, exit_code = execute_command(
                command,
                credentials_file=self.credentials_file
            )

            # 記錄到資料庫
            request_id = self._generate_request_id(command)
            self.db.create_request(
                request_id=request_id,
                command=command,
                reason=reason,
                classification=classification
            )
            self.db.update_request(
                request_id,
                status='approved',
                result=output,
                exit_code=exit_code,
                approved_by='system'
            )

            return self._tool_result(req_id, {
                'status': 'auto_approved',
                'command': command,
                'classification': classification,
                'output': output,
                'exit_code': exit_code,
                'request_id': request_id
            })

        # Layer 3: APPROVAL（需要人工審批）
        if not self.telegram_client:
            return self._tool_error(req_id, 'Telegram not configured, cannot request approval')

        request_id = self._generate_request_id(command)

        # 建立請求記錄
        self.db.create_request(
            request_id=request_id,
            command=command,
            reason=reason,
            classification=classification,
            expires_in=timeout
        )

        # 註冊等待
        self.approval_waiter.register(request_id)

        # 發送 Telegram 審批請求
        message_id = self.telegram_client.send_approval_request(
            request_id=request_id,
            command=command,
            reason=reason,
            timeout_seconds=timeout
        )

        if message_id:
            self.db.update_request(request_id, telegram_message_id=message_id)

        # 等待審批結果（blocking）
        start_time = time.time()
        result = self.approval_waiter.wait(request_id, timeout=timeout)
        elapsed = int(time.time() - start_time)

        # 清理
        self.approval_waiter.cleanup(request_id)

        # 取得最新狀態
        request = self.db.get_request(request_id)

        if result and result['action'] == 'approve':
            return self._tool_result(req_id, {
                'status': 'approved',
                'command': command,
                'classification': classification,
                'output': request.get('result', ''),
                'exit_code': request.get('exit_code', 0),
                'request_id': request_id,
                'approved_by': result['user_id'],
                'elapsed_seconds': elapsed
            })

        elif result and result['action'] == 'deny':
            return self._tool_result(req_id, {
                'status': 'denied',
                'command': command,
                'classification': classification,
                'request_id': request_id,
                'denied_by': result['user_id'],
                'elapsed_seconds': elapsed
            }, is_error=True)

        else:
            # Timeout
            self.db.update_request(request_id, status='timeout')
            return self._tool_result(req_id, {
                'status': 'timeout',
                'command': command,
                'classification': classification,
                'request_id': request_id,
                'message': f'Approval timed out after {timeout} seconds',
                'elapsed_seconds': elapsed
            }, is_error=True)

    def _tool_status(self, req_id, arguments: Dict) -> Dict:
        """bouncer_status tool"""
        request_id = arguments.get('request_id', '')

        if not request_id:
            return self._tool_error(req_id, 'Missing required parameter: request_id')

        request = self.db.get_request(request_id)

        if not request:
            return self._tool_result(req_id, {
                'error': 'Request not found',
                'request_id': request_id
            }, is_error=True)

        return self._tool_result(req_id, request)

    def _tool_list_rules(self, req_id) -> Dict:
        """bouncer_list_rules tool"""
        return self._tool_result(req_id, {
            'safelist_prefixes': get_safelist(),
            'blocked_patterns': get_blocked_patterns()
        })

    def _tool_stats(self, req_id) -> Dict:
        """bouncer_stats tool"""
        stats = self.db.get_stats()
        return self._tool_result(req_id, stats)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _generate_request_id(self, command: str) -> str:
        """產生唯一請求 ID"""
        data = f"{command}{time.time()}{os.urandom(8).hex()}"
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    def _result(self, req_id, result: Any) -> Dict:
        """構造 JSON-RPC 成功回應"""
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'result': result
        }

    def _error(self, req_id, code: int, message: str) -> Dict:
        """構造 JSON-RPC 錯誤回應"""
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {
                'code': code,
                'message': message
            }
        }

    def _tool_result(self, req_id, data: Dict, is_error: bool = False) -> Dict:
        """構造 MCP tool 結果"""
        return self._result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(data, indent=2, default=str)
            }],
            'isError': is_error
        })

    def _tool_error(self, req_id, message: str) -> Dict:
        """構造 MCP tool 錯誤"""
        return self._tool_result(req_id, {'error': message}, is_error=True)


# ============================================================================
# Entry Point
# ============================================================================

def main():
    server = BouncerMCPServer()
    server.run()


if __name__ == '__main__':
    main()
