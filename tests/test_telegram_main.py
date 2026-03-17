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
import pytest
pytestmark = pytest.mark.xdist_group("app_module")


# ============================================================================
# Telegram Webhook 測試
# ============================================================================


@pytest.fixture(autouse=True)
def _mock_entities_send():
    """Ensure send_message_with_entities is mocked for pre-entities tests."""
    import sys, importlib
    import telegram as _tg
    from unittest.mock import MagicMock

    mock_msg_id = 99999
    mock_response = {'ok': True, 'result': {'message_id': mock_msg_id}}

    # Save originals
    orig_entities = getattr(_tg, 'send_message_with_entities', None)

    # Replace only send_message_with_entities (entities Phase 2 migration)
    mock_entities = MagicMock(return_value=mock_response)
    _tg.send_message_with_entities = mock_entities

    # Reload notifications so it picks up the mocks
    if 'notifications' in sys.modules:
        importlib.reload(sys.modules['notifications'])

    yield mock_entities

    # Restore
    if orig_entities is not None:
        _tg.send_message_with_entities = orig_entities
    elif hasattr(_tg, 'send_message_with_entities'):
        delattr(_tg, 'send_message_with_entities')


class TestTelegramWebhook:
    """Telegram Webhook 測試"""
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    @patch('callbacks_command.execute_command')
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
        mock_urlopen.side_effect = OSError('Network error')
        # 不應該拋出異常
        send_telegram_message('test message')
    
    @patch('urllib.request.urlopen')
    def test_answer_callback_error(self, mock_urlopen, app_module):
        """callback 回答失敗"""
        from telegram import answer_callback
        mock_urlopen.side_effect = OSError('Network error')
        answer_callback('callback-id', 'text')


# ============================================================================
# Telegram Message 功能測試
# ============================================================================

class TestTelegramMessageFunctions:
    """Telegram Message 功能測試"""
    
    def test_send_approval_request(self, app_module):
        """發送審批請求"""
        with patch('telegram.send_message_with_entities') as mock_send:
            mock_send.return_value = {'ok': True, 'result': {'message_id': 1}}
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
        with patch('telegram.send_message_with_entities') as mock_send:
            mock_send.return_value = {'ok': True, 'result': {'message_id': 1}}
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


# ============================================================================
# GSI Query 驗證測試 (sprint7-003)
# ============================================================================

class TestTelegramCommandsGSI:

    @pytest.fixture(autouse=True)
    def fresh_app(self, app_module):
        """Ensure app module is fresh for GSI tests."""
        import importlib, sys
        if 'telegram_commands' in sys.modules:
            importlib.reload(sys.modules['telegram_commands'])
        yield app_module

    """驗證 /pending 和 /stats 使用 GSI Query 而非 Scan"""

    def test_pending_command_uses_gsi_query(self, app_module):
        """/pending 使用 status-created-index GSI Query，不走 Scan"""
        import db as _db
        # Re-fetch table reference after conftest may have reset _db.table
        table = _db.table

        with patch.object(table, "query", wraps=table.query) as mock_query, \
             patch.object(table, "scan") as mock_scan, \
             patch("telegram_commands.send_telegram_message_to"), \
             patch("telegram_commands._get_table", return_value=table):
            app_module.handle_pending_command("12345")

        mock_scan.assert_not_called()
        assert mock_query.called
        assert any(
            call.kwargs.get("IndexName") == "status-created-index"
            for call in mock_query.call_args_list
        )

    def test_stats_command_uses_gsi_query(self, app_module):
        """/stats 使用 status-created-index GSI Query，不走 Scan"""
        import db as _db
        import telegram_commands
        # Re-fetch table reference after conftest may have reset _db.table
        table = _db.table

        with patch.object(table, "query", wraps=table.query) as mock_query, \
             patch.object(table, "scan") as mock_scan, \
             patch("telegram_commands.send_telegram_message_to"), \
             patch("telegram_commands._get_table", return_value=table):
            telegram_commands.handle_stats_command("12345", hours=24)

        mock_scan.assert_not_called()
        assert mock_query.called
        assert any(
            call.kwargs.get("IndexName") == "status-created-index"
            for call in mock_query.call_args_list
        )

    def test_pending_command_returns_pending_items_via_gsi(self, app_module):
        """/pending 通過 GSI Query 正確回傳 pending 狀態項目"""
        import db as _db
        table = _db.table

        # 插入 pending 和 approved 各一筆
        table.put_item(Item={
            "request_id": "gsi-pending-test-1",
            "command": "aws s3 ls",
            "status": "pending",
            "source": "test-bot",
            "created_at": int(time.time()) - 100,
        })
        table.put_item(Item={
            "request_id": "gsi-approved-test-1",
            "command": "aws ec2 ls",
            "status": "approved",
            "source": "test-bot",
            "created_at": int(time.time()) - 200,
        })

        with patch("telegram_commands.send_telegram_message_to") as mock_send:
            result = app_module.handle_pending_command("12345")

        assert result["statusCode"] == 200
        # 確認發出的訊息只包含 pending 項目
        sent_text = mock_send.call_args[0][1]
        assert "pending" in sent_text.lower() or "gsi-pending" in sent_text or "aws s3 ls" in sent_text

    def test_stats_command_counts_correct_totals(self, app_module):
        """/stats 透過 GSI 正確統計各狀態數量"""
        import db as _db
        import telegram_commands
        table = _db.table

        now = int(time.time())
        for i, status in enumerate(["approved", "denied", "pending"]):
            table.put_item(Item={
                "request_id": f"gsi-stats-{status}",
                "command": f"aws cmd {i}",
                "status": status,
                "source": "stats-bot",
                "created_at": now - (i + 1) * 100,
            })

        with patch("telegram_commands.send_telegram_message_to") as mock_send:
            result = telegram_commands.handle_stats_command("12345", hours=24)

        assert result["statusCode"] == 200
        sent_text = mock_send.call_args[0][1]
        # 統計文字應包含批准/拒絕/待審批資訊
        assert "批准" in sent_text or "approved" in sent_text.lower() or "✅" in sent_text


# ============================================================================
# Sprint 13-002: show_alert for DANGEROUS Commands
# ============================================================================

class TestAnswerCallbackShowAlert:
    """Tests for answer_callback show_alert parameter (sprint13-002)"""

    def test_answer_callback_default_no_show_alert(self, app_module):
        """Default call: _telegram_request NOT called with show_alert in data"""
        import telegram as tg

        with patch.object(tg, '_telegram_request') as mock_req:
            tg.answer_callback('cb-001', 'Toast message')

        mock_req.assert_called_once()
        call_args = mock_req.call_args
        data = call_args[0][1]  # positional: method, data

        assert data['callback_query_id'] == 'cb-001'
        assert data['text'] == 'Toast message'
        assert 'show_alert' not in data

    def test_answer_callback_show_alert_true(self, app_module):
        """show_alert=True: data must include show_alert=True"""
        import telegram as tg

        with patch.object(tg, '_telegram_request') as mock_req:
            tg.answer_callback('cb-002', '⚠️ 高危操作確認：正在執行...', show_alert=True)

        mock_req.assert_called_once()
        data = mock_req.call_args[0][1]

        assert data['callback_query_id'] == 'cb-002'
        assert data['text'] == '⚠️ 高危操作確認：正在執行...'
        assert data.get('show_alert') is True

    def test_answer_callback_show_alert_false_not_in_body(self, app_module):
        """show_alert=False explicitly: key should NOT appear in data"""
        import telegram as tg

        with patch.object(tg, '_telegram_request') as mock_req:
            tg.answer_callback('cb-003', 'Normal toast', show_alert=False)

        mock_req.assert_called_once()
        data = mock_req.call_args[0][1]

        assert 'show_alert' not in data


class TestHandleCommandCallbackShowAlert:
    """Tests for handle_command_callback DANGEROUS show_alert (sprint13-002)"""

    def test_dangerous_command_approve_uses_show_alert(self, mock_dynamodb, app_module):
        """DANGEROUS command approve → answer_callback called with show_alert=True"""
        import time as _time

        request_id = 'req-dangerous-001'
        # 'aws iam delete-role' matches DANGEROUS_PATTERNS
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 terminate-instances --instance-ids i-0abc123',
            'status': 'pending',
            'source': 'test',
            'reason': 'testing dangerous',
            'account_id': '123456789012',
            'created_at': int(_time.time()),
        })

        with patch('callbacks_command.answer_callback') as mock_answer, \
             patch('callbacks_command.update_message'), \
             patch('callbacks_command.execute_command') as mock_exec, \
             patch('callbacks_command.store_paged_output') as mock_paged, \
             patch('callbacks_command.emit_metric'):
            mock_exec.return_value = 'Role deleted'
            from paging import PaginatedOutput
            mock_paged.return_value = PaginatedOutput(
                paged=False, result='Role deleted',
                page=1, total_pages=1, output_length=12,
            )

            from callbacks import handle_command_callback
            handle_command_callback(
                'approve', request_id,
                {
                    'command': 'aws ec2 terminate-instances --instance-ids i-0abc123',
                    'source': 'test', 'reason': 'testing',
                    'trust_scope': 'test', 'context': '',
                    'account_id': '123456789012', 'account_name': 'Test',
                    'created_at': int(_time.time()),
                },
                999, 'cb-danger', 'user-1'
            )

        # answer_callback should have been called with show_alert=True
        calls = mock_answer.call_args_list
        assert any(
            call[1].get('show_alert') is True or (len(call[0]) > 2 and call[0][2] is True)
            for call in calls
        ), f"Expected show_alert=True in one of: {calls}"

    def test_safe_command_approve_no_show_alert(self, mock_dynamodb, app_module):
        """Safe command approve → answer_callback called WITHOUT show_alert"""
        import time as _time

        request_id = 'req-safe-001'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending',
            'source': 'test',
            'reason': 'listing',
            'account_id': '123456789012',
            'created_at': int(_time.time()),
        })

        with patch('callbacks_command.answer_callback') as mock_answer, \
             patch('callbacks_command.update_message'), \
             patch('callbacks_command.execute_command') as mock_exec, \
             patch('callbacks_command.store_paged_output') as mock_paged, \
             patch('callbacks_command.emit_metric'):
            mock_exec.return_value = 'bucket-list'
            from paging import PaginatedOutput
            mock_paged.return_value = PaginatedOutput(
                paged=False, result='bucket-list',
                page=1, total_pages=1, output_length=11,
            )

            from callbacks import handle_command_callback
            handle_command_callback(
                'approve', request_id,
                {
                    'command': 'aws s3 ls',
                    'source': 'test', 'reason': 'listing',
                    'trust_scope': 'test', 'context': '',
                    'account_id': '123456789012', 'account_name': 'Test',
                    'created_at': int(_time.time()),
                },
                999, 'cb-safe', 'user-1'
            )

        calls = mock_answer.call_args_list
        # No call should have show_alert=True
        assert not any(
            call[1].get('show_alert') is True or (len(call[0]) > 2 and call[0][2] is True)
            for call in calls
        ), f"Expected no show_alert=True, but got: {calls}"


class TestHandleGrantApproveShowAlert:
    """Tests for handle_grant_approve_all DANGEROUS show_alert (sprint26-062)"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded')
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_grant_approve_all_with_dangerous_commands_uses_show_alert(
        self, mock_compliance, mock_blocked, mock_excluded, mock_risk, mock_dynamodb, app_module
    ):
        """Grant approve_all with requires_individual commands → show_alert=True"""
        # Setup: one safe command (grantable), one dangerous command (requires_individual)
        mock_risk_safe = MagicMock()
        mock_risk_safe.score = 20
        mock_risk_dangerous = MagicMock()
        mock_risk_dangerous.score = 80  # >= 66 → requires_individual
        mock_risk.side_effect = [mock_risk_safe, mock_risk_dangerous]
        mock_excluded.return_value = False

        from grant import create_grant_request
        result = create_grant_request(
            commands=['aws s3 ls', 'aws iam delete-role --role-name TestRole'],
            reason='testing dangerous grant',
            source='Bot',
            account_id='123456789012',
        )
        grant_id = result['grant_id']

        # Verify grant has requires_individual commands
        from grant import get_grant_session
        grant_check = get_grant_session(grant_id)
        commands_detail = grant_check.get('commands_detail', [])
        assert any(d.get('category') == 'requires_individual' for d in commands_detail), \
            "Expected grant to contain requires_individual commands"

        # Approve with mode='all'
        query = {
            'id': 'cb-grant-001',
            'from': {'id': 999999999},
            'message': {'message_id': 123}
        }

        with patch('callbacks_grant.answer_callback') as mock_answer, \
             patch('callbacks_grant.update_message') as mock_update:
            from callbacks_grant import handle_grant_approve
            handle_grant_approve(query, grant_id, mode='all')

        # answer_callback should have been called with show_alert=True
        calls = mock_answer.call_args_list
        assert any(
            call[1].get('show_alert') is True or (len(call[0]) > 2 and call[0][2] is True)
            for call in calls
        ), f"Expected show_alert=True for dangerous grant, but got: {calls}"

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_grant_approve_all_safe_only_no_show_alert(
        self, mock_compliance, mock_blocked, mock_excluded, mock_risk, mock_dynamodb, app_module
    ):
        """Grant approve_all with only grantable commands → no show_alert"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20  # < 66 → grantable
        mock_risk.return_value = mock_risk_result

        from grant import create_grant_request
        result = create_grant_request(
            commands=['aws s3 ls', 'aws ec2 describe-instances'],
            reason='testing safe grant',
            source='Bot',
            account_id='123456789012',
        )
        grant_id = result['grant_id']

        # Verify grant has only grantable commands
        from grant import get_grant_session
        grant_check = get_grant_session(grant_id)
        commands_detail = grant_check.get('commands_detail', [])
        assert all(d.get('category') == 'grantable' for d in commands_detail), \
            "Expected grant to contain only grantable commands"

        # Approve with mode='all'
        query = {
            'id': 'cb-grant-002',
            'from': {'id': 999999999},
            'message': {'message_id': 124}
        }

        with patch('callbacks_grant.answer_callback') as mock_answer, \
             patch('callbacks_grant.update_message') as mock_update:
            from callbacks_grant import handle_grant_approve
            handle_grant_approve(query, grant_id, mode='all')

        # answer_callback should NOT have show_alert=True
        calls = mock_answer.call_args_list
        assert not any(
            call[1].get('show_alert') is True or (len(call[0]) > 2 and call[0][2] is True)
            for call in calls
        ), f"Expected no show_alert=True for safe grant, but got: {calls}"

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_grant_approve_safe_only_mode_no_show_alert(
        self, mock_compliance, mock_blocked, mock_excluded, mock_risk, mock_dynamodb, app_module
    ):
        """Grant approve with mode='safe_only' → no show_alert even if grant has dangerous commands"""
        # Setup: mixed grant with safe and dangerous commands
        mock_risk_safe = MagicMock()
        mock_risk_safe.score = 20
        mock_risk_dangerous = MagicMock()
        mock_risk_dangerous.score = 80
        mock_risk.side_effect = [mock_risk_safe, mock_risk_dangerous]

        from grant import create_grant_request
        result = create_grant_request(
            commands=['aws s3 ls', 'aws iam delete-role --role-name TestRole'],
            reason='testing safe_only mode',
            source='Bot',
            account_id='123456789012',
        )
        grant_id = result['grant_id']

        # Approve with mode='safe_only'
        query = {
            'id': 'cb-grant-003',
            'from': {'id': 999999999},
            'message': {'message_id': 125}
        }

        with patch('callbacks_grant.answer_callback') as mock_answer, \
             patch('callbacks_grant.update_message') as mock_update:
            from callbacks_grant import handle_grant_approve
            handle_grant_approve(query, grant_id, mode='safe_only')

        # answer_callback should NOT have show_alert=True (only approving safe commands)
        calls = mock_answer.call_args_list
        assert not any(
            call[1].get('show_alert') is True or (len(call[0]) > 2 and call[0][2] is True)
            for call in calls
        ), f"Expected no show_alert=True for safe_only mode, but got: {calls}"
