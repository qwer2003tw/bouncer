"""
test_telegram_main.py — Telegram webhook 與指令測試
Extracted from test_bouncer.py batch-b
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3


# ============================================================================
# Telegram Webhook 測試
# ============================================================================

class TestTelegramWebhook:
    """Telegram Webhook 測試"""
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    @patch('callbacks.execute_command')
    def test_approve_callback(self, mock_execute, mock_answer, mock_update, app_module):
        """測試審批通過 callback"""
        mock_execute.return_value = 'Done'
        
        # 建立 pending 請求
        request_id = 'webhook_test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 start-instances --instance-ids i-123',
            'status': 'pending_approval',
            'created_at': int(time.time())
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        # 驗證狀態更新
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'approved'
        assert 'result' in item
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_deny_callback(self, mock_answer, mock_update, app_module):
        """測試拒絕 callback"""
        request_id = 'deny_test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 terminate-instances --instance-ids i-123',
            'status': 'pending_approval',
            'created_at': int(time.time())
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb456',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 888}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    def test_unauthorized_user(self, mock_answer, app_module):
        """測試未授權用戶"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb789',
                    'from': {'id': 999999},  # 未授權
                    'data': 'approve:test123',
                    'message': {'message_id': 777}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 403


# ============================================================================
# Telegram 模組測試
# ============================================================================

class TestTelegramModule:
    """Telegram 模組測試"""
    
    def test_escape_markdown_special_chars(self, app_module):
        """Markdown 特殊字元跳脫（V1 官方支援反斜線 escape）"""
        from telegram import escape_markdown
        assert escape_markdown('*bold*') == '\\*bold\\*'
        assert escape_markdown('_italic_') == '\\_italic\\_'
        assert escape_markdown('`code`') == '\\`code\\`'
        assert escape_markdown('[link') == '\\[link'
        assert escape_markdown('back\\slash') == 'back\\\\slash'
    
    def test_escape_markdown_none(self, app_module):
        """None 輸入應返回 None"""
        from telegram import escape_markdown
        assert escape_markdown(None) is None
    
    def test_escape_markdown_empty(self, app_module):
        """空字串應返回空字串"""
        from telegram import escape_markdown
        assert escape_markdown('') == ''
    
    def test_escape_markdown_no_special(self, app_module):
        """無特殊字元不變"""
        from telegram import escape_markdown
        assert escape_markdown('hello world') == 'hello world'
    
    def test_telegram_requests_parallel_empty(self, app_module):
        """空請求列表"""
        from telegram import _telegram_requests_parallel
        result = _telegram_requests_parallel([])
        assert result == []


# ============================================================================
# Telegram 命令處理測試
# ============================================================================

class TestTelegramCommands:
    """Telegram 命令處理測試"""
    
    def test_handle_accounts_command(self, app_module):
        """測試 /accounts 命令"""
        with patch.object(app_module, 'list_accounts', return_value=[
            {'account_id': '123456789012', 'name': 'Test', 'enabled': True}
        ]), patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_accounts_command('12345')
            assert result['statusCode'] == 200
    
    def test_handle_help_command(self, app_module):
        """測試 /help 命令"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_help_command('12345')
            assert result['statusCode'] == 200


# ============================================================================
# Telegram Command Handler 測試
# ============================================================================

class TestTelegramCommandHandler:
    """Telegram 命令處理測試"""
    
    def test_handle_telegram_command_no_text(self, app_module):
        """無 text 欄位"""
        result = app_module.handle_telegram_command({'chat': {'id': 123}})
        assert result['statusCode'] == 200
    
    def test_handle_telegram_command_unknown(self, app_module):
        """未知命令"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_telegram_command({
                'chat': {'id': 123},
                'text': '/unknown'
            })
            assert result['statusCode'] == 200


# ============================================================================
# Telegram Webhook Handler 測試
# ============================================================================

class TestTelegramWebhookHandler:
    """Telegram webhook 處理測試"""
    
    def test_handle_telegram_webhook_empty_update(self, app_module):
        """空 update"""
        event = {'body': '{}'}
        result = app_module.handle_telegram_webhook(event)
        assert result['statusCode'] == 200
    
    def test_handle_telegram_webhook_with_message(self, app_module):
        """有 message 的 update"""
        event = {'body': json.dumps({
            'message': {
                'chat': {'id': 123},
                'text': 'hello'
            }
        })}
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_telegram_webhook(event)
            assert result['statusCode'] == 200


# ============================================================================
# Telegram Commands 測試補充
# ============================================================================

class TestTelegramCommandsAdditional:
    """Telegram Commands 補充測試"""
    
    def test_handle_trust_command_empty(self, app_module):
        """/trust 命令沒有活躍時段"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_trust_command('12345')
            assert result['statusCode'] == 200
    
    def test_handle_pending_command_with_items(self, app_module):
        """/pending 命令有待審批項目"""
        # 建立 pending 項目
        app_module.table.put_item(Item={
            'request_id': 'pending-cmd-test',
            'command': 'aws ec2 start-instances',
            'status': 'pending',
            'source': 'test',
            'created_at': int(time.time())
        })
        
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_pending_command('999999999')
            assert result['statusCode'] == 200


# ============================================================================
# Telegram 模組完整測試
# ============================================================================

class TestTelegramModuleFull:
    """Telegram 模組完整測試"""
    
    def test_send_telegram_message(self, app_module):
        """發送 Telegram 訊息"""
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            
            # 直接呼叫 telegram 模組的函數
            from telegram import send_telegram_message
            send_telegram_message('Test message')
            
            mock_urlopen.assert_called()
    
    def test_update_message(self, app_module):
        """更新 Telegram 訊息"""
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            
            app_module.update_message(123, 'Updated text')
            mock_urlopen.assert_called()
    
    def test_answer_callback(self, app_module):
        """回答 callback query"""
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            
            app_module.answer_callback('cb123', 'Done!')
            mock_urlopen.assert_called()


# ============================================================================
# Telegram 更多測試
# ============================================================================

class TestTelegramMore:
    """Telegram 更多測試"""
    
    @patch('urllib.request.urlopen')
    def test_send_telegram_message_error(self, mock_urlopen, app_module):
        """發送失敗"""
        from telegram import send_telegram_message
        mock_urlopen.side_effect = Exception('Network error')
        # 不應該拋出異常
        send_telegram_message('test message')
    
    @patch('urllib.request.urlopen')
    def test_answer_callback_error(self, mock_urlopen, app_module):
        """callback 回答失敗"""
        from telegram import answer_callback
        mock_urlopen.side_effect = Exception('Network error')
        answer_callback('callback-id', 'text')


# ============================================================================
# Telegram Message 功能測試
# ============================================================================

class TestTelegramMessageFunctions:
    """Telegram Message 功能測試"""
    
    def test_send_approval_request(self, app_module):
        """發送審批請求"""
        with patch('telegram.send_telegram_message') as mock_send:
            app_module.send_approval_request(
                'test-req-123',
                'aws ec2 start-instances --instance-ids i-123',
                'Test reason',
                timeout=300,
                source='test-source',
                account_id='111111111111',
                account_name='Test Account'
            )
            mock_send.assert_called_once()
    
    def test_send_approval_request_dangerous(self, app_module):
        """發送高危命令審批請求"""
        with patch('telegram.send_telegram_message') as mock_send:
            app_module.send_approval_request(
                'test-req-456',
                'aws ec2 terminate-instances --instance-ids i-123',  # 高危
                'Test reason',
                timeout=300
            )
            mock_send.assert_called_once()


# ============================================================================
# Webhook 訊息測試
# ============================================================================

class TestWebhookMessage:
    """Webhook 訊息測試"""
    
    def test_webhook_text_message(self, app_module):
        """收到文字訊息"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'message': {
                    'message_id': 123,
                    'from': {'id': 999999999},
                    'chat': {'id': 999999999},
                    'text': 'hello'
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
    
    def test_webhook_empty_body(self, app_module):
        """空的 webhook body"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': '{}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
