"""
test_callbacks_main.py — Callback handlers 測試
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
# Telegram Callback Handlers 測試
# ============================================================================

class TestTelegramCallbackHandlers:
    """Telegram callback handlers 測試"""
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_revoke_trust_success(self, mock_update, mock_answer, app_module):
        """撤銷信任時段成功"""
        trust_id = 'trust-123'
        
        # 先建立信任時段
        app_module.table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'source': 'test-source',
            'trust_scope': 'test-source',
            'expires_at': int(time.time()) + 600
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'revoke_trust:{trust_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called()
    
    @patch('app.answer_callback')
    def test_callback_request_not_found(self, mock_answer, app_module):
        """請求不存在"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': 'approve:nonexistent-id',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 404
        mock_answer.assert_called_with('cb123', '❌ 請求已過期或不存在')
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_request_already_processed(self, mock_update, mock_answer, app_module):
        """請求已處理過"""
        request_id = 'processed-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'approved',  # 已處理
            'source': 'test',
            'reason': 'test'
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
        mock_answer.assert_called_with('cb123', '⚠️ 此請求已處理過')
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_request_expired(self, mock_update, mock_answer, app_module):
        """請求已過期"""
        request_id = 'expired-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'ttl': int(time.time()) - 100  # 已過期
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
        mock_answer.assert_called_with('cb123', '⏰ 此請求已過期')
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    @patch('callbacks.execute_command')
    def test_callback_approve_trust(self, mock_execute, mock_update, mock_answer, app_module):
        """批准並建立信任時段"""
        mock_execute.return_value = 'Instance started'
        
        request_id = 'trust-approve-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 start-instances --instance-ids i-123',
            'status': 'pending_approval',
            'source': 'test-source',
            'reason': 'test',
            'account_id': '111111111111',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'approve_trust:{request_id}',
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
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_add_account_approve(self, mock_update, mock_answer, app_module):
        """批准新增帳號"""
        request_id = 'add-account-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'add_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'role_arn': 'arn:aws:iam::111111111111:role/TestRole',
            'status': 'pending_approval',
            'source': 'test-source',
            'ttl': int(time.time()) + 300
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
        
        # Mock accounts_table
        import db
        with patch('db.accounts_table') as mock_accounts, \
             patch.object(db, 'accounts_table', mock_accounts):
            mock_accounts.put_item = MagicMock()
            result = app_module.lambda_handler(event, None)
            assert result['statusCode'] == 200
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_add_account_deny(self, mock_update, mock_answer, app_module):
        """拒絕新增帳號"""
        request_id = 'add-account-deny-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'add_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'status': 'pending_approval',
            'source': 'test-source',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_remove_account_approve(self, mock_update, mock_answer, app_module):
        """批准移除帳號"""
        request_id = 'remove-account-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'remove_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'status': 'pending_approval',
            'source': 'test-source',
            'ttl': int(time.time()) + 300
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
        
        # Mock accounts_table
        import db
        with patch('db.accounts_table') as mock_accounts, \
             patch.object(db, 'accounts_table', mock_accounts):
            mock_accounts.delete_item = MagicMock()
            result = app_module.lambda_handler(event, None)
            assert result['statusCode'] == 200
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_remove_account_deny(self, mock_update, mock_answer, app_module):
        """拒絕移除帳號"""
        request_id = 'remove-account-deny-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'remove_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'status': 'pending_approval',
            'source': 'test-source',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_upload_approve(self, mock_update, mock_answer, app_module):
        """批准上傳"""
        import base64
        request_id = 'upload-123'
        content = base64.b64encode(b'test content').decode()
        
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'test-bucket',
            'key': 'test/file.txt',
            'content': content,
            'content_type': 'text/plain',
            'content_size': 12,
            'status': 'pending_approval',
            'source': 'test-source',
            'reason': 'test',
            'ttl': int(time.time()) + 300
        })
        
        # Mock S3 upload
        with patch('boto3.client') as mock_boto:
            mock_s3 = MagicMock()
            mock_s3.meta.region_name = 'us-east-1'
            mock_boto.return_value = mock_s3
            
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
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_upload_deny(self, mock_update, mock_answer, app_module):
        """拒絕上傳"""
        request_id = 'upload-deny-123'
        
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'test-bucket',
            'key': 'test/file.txt',
            'content_size': 12,
            'status': 'pending_approval',
            'source': 'test-source',
            'reason': 'test',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'


# ============================================================================
# Pending Command Handler 測試
# ============================================================================

class TestPendingCommandHandler:
    """Pending 命令處理測試"""
    
    def test_handle_pending_command_empty(self, app_module):
        """無 pending 請求"""
        with patch.object(app_module.table, 'query', return_value={'Items': []}), \
             patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_pending_command('12345')
            assert result['statusCode'] == 200


# ============================================================================
# Callback Handlers 完整測試
# ============================================================================

class TestCallbackHandlersFull:
    """Callback Handlers 完整測試"""
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_deny_command(self, mock_update, mock_answer, app_module):
        """拒絕命令執行"""
        request_id = 'deny-cmd-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 terminate-instances --instance-ids i-123',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Test',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_deploy_deny(self, mock_update, mock_answer, app_module):
        """拒絕部署"""
        request_id = 'deploy-deny-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'deploy',
            'project_id': 'test-project',
            'project_name': 'Test Project',
            'branch': 'main',
            'stack_name': 'test-stack',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test deploy',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    @patch('deployer.start_deploy')
    def test_callback_deploy_approve(self, mock_start, mock_update, mock_answer, app_module):
        """批准部署"""
        mock_start.return_value = {'status': 'started', 'deploy_id': 'deploy-123'}
        
        request_id = 'deploy-approve-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'deploy',
            'project_id': 'test-project',
            'project_name': 'Test Project',
            'branch': 'main',
            'stack_name': 'test-stack',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test deploy',
            'ttl': int(time.time()) + 300
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


# ============================================================================
# Callback Handlers 測試
# ============================================================================

class TestCallbackHandlers:
    """Callback Handlers 測試"""
    
    @patch('app.execute_command')
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_approve(self, mock_answer, mock_update, mock_exec, app_module):
        """批准請求"""
        mock_exec.return_value = '{"result": "ok"}'
        
        request_id = 'approve-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-approve',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 1000}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        # 驗證狀態更新
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item['status'] in ['approved', 'executed']
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_reject(self, mock_answer, mock_update, app_module):
        """拒絕請求"""
        request_id = 'reject-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 stop-instances',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-reject',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',  # 正確的 action 是 deny
                    'message': {'message_id': 1001}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        # 驗證狀態更新
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item['status'] == 'denied'
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_expired_request(self, mock_answer, mock_update, app_module):
        """已過期的請求"""
        request_id = 'expired-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 stop-instances',
            'status': 'expired',
            'source': 'test',
            'reason': 'test',
            'created_at': int(time.time()) - 1000,
            'ttl': int(time.time()) - 100
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-expired',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 1002}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200


# ============================================================================
# Already Processed Display 測試
# ============================================================================

class TestAlreadyProcessedDisplay:
    """Tests for the 'already processed' callback display logic"""

    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_already_processed_uses_display_summary(self, mock_answer, mock_update, app_module):
        """Already-processed callback: only toast, original message preserved (not overwritten)"""
        request_id = 'display-summary-test-1'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload_batch',
            'status': 'approved',
            'source': 'test-bot',
            'reason': 'test',
            'file_count': 9,
            'display_summary': 'upload_batch (9 個檔案, 245.00 KB)',
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-ds-1',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 888}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called_with('cb-ds-1', '⚠️ 此請求已處理過')

        # Fix: update_message must NOT be called for already-handled requests
        # Original message should be preserved; only a toast notification is shown
        mock_update.assert_not_called()

    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_already_processed_legacy_fallback(self, mock_answer, mock_update, app_module):
        """Already-processed callback: update_message NOT called (original message preserved)"""
        request_id = 'legacy-no-summary-1'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'approved',
            'source': 'test-bot',
            'reason': 'test',
            # No display_summary field (legacy item)
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-legacy-1',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 887}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called_with('cb-legacy-1', '⚠️ 此請求已處理過')

        # Fix: update_message must NOT be called — original message is preserved
        mock_update.assert_not_called()

    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_already_processed_legacy_upload_batch(self, mock_answer, mock_update, app_module):
        """Legacy upload_batch already-processed: update_message NOT called"""
        request_id = 'legacy-batch-1'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload_batch',
            'status': 'approved',
            'source': 'test-bot',
            'reason': 'test',
            'file_count': 5,
            # No display_summary (legacy)
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-legacy-batch-1',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 886}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called_with('cb-legacy-batch-1', '⚠️ 此請求已處理過')

        # Fix: update_message must NOT be called — original message is preserved
        mock_update.assert_not_called()

    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_already_processed_no_crash_on_empty_item(self, mock_answer, mock_update, app_module):
        """Already-processed callback doesn't crash on items with minimal fields"""
        request_id = 'minimal-item-1'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'status': 'denied',
            # No command, no action, no display_summary, no source, no reason
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-minimal-1',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 885}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        # Should not crash
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200


# ============================================================================
# Orphan Approval Cleanup 測試
# ============================================================================

class TestOrphanApprovalCleanup:
    """Regression tests for P1-1: cleanup DDB record when Telegram notification fails."""

    @patch('telegram.send_telegram_message')
    def test_execute_telegram_success_ddb_has_record_returns_pending(self, mock_telegram, mock_dynamodb, app_module):
        """Telegram 成功 → DDB 有 record，回 pending_approval ✅"""
        mock_telegram.return_value = {'ok': True, 'result': {'message_id': 1}}

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 900,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 start-instances --instance-ids i-p1-1a',
                        'trust_scope': 'test-scope',
                        'reason': 'P1-1 regression success test',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])

        assert content['status'] == 'pending_approval', f"Expected pending_approval, got: {content}"
        assert 'request_id' in content

        # Verify DDB record exists
        ddb_item = mock_dynamodb.Table('clawdbot-approval-requests').get_item(
            Key={'request_id': content['request_id']}
        ).get('Item')
        assert ddb_item is not None, "DDB record should exist when Telegram succeeds"
        assert ddb_item['status'] == 'pending_approval'

    @patch('telegram.send_telegram_message')
    def test_execute_telegram_failure_ddb_no_record_returns_error(self, mock_telegram, mock_dynamodb, app_module):
        """Telegram 失敗（empty response）→ DDB 無 record，回 error ✅"""
        mock_telegram.return_value = {}  # Telegram failure returns empty dict

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 901,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 stop-instances --instance-ids i-p1-1b',
                        'trust_scope': 'test-scope',
                        'reason': 'P1-1 regression failure test',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])

        assert content['status'] == 'error', f"Expected error, got: {content}"
        assert 'Telegram' in content.get('error', '') or 'notification' in content.get('error', '').lower()

        # Verify DDB record was cleaned up (no orphan record)
        scan_result = mock_dynamodb.Table('clawdbot-approval-requests').scan(
            FilterExpression='#cmd = :cmd AND #status = :status',
            ExpressionAttributeNames={'#cmd': 'command', '#status': 'status'},
            ExpressionAttributeValues={
                ':cmd': 'aws ec2 stop-instances --instance-ids i-p1-1b',
                ':status': 'pending_approval',
            }
        )
        orphan_items = [i for i in scan_result.get('Items', [])
                        if i.get('reason') == 'P1-1 regression failure test']
        assert len(orphan_items) == 0, f"Orphan DDB records found: {orphan_items}"

    @patch('telegram.send_telegram_message')
    def test_execute_telegram_exception_ddb_no_record_returns_error(self, mock_telegram, mock_dynamodb, app_module):
        """Telegram 失敗（exception）→ DDB 無 record，回 error ✅"""
        mock_telegram.side_effect = Exception("Telegram connection refused")

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 902,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 reboot-instances --instance-ids i-p1-1c',
                        'trust_scope': 'test-scope',
                        'reason': 'P1-1 regression exception test',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])

        assert content['status'] == 'error', f"Expected error, got: {content}"

        # Verify no orphan DDB record
        scan_result = mock_dynamodb.Table('clawdbot-approval-requests').scan(
            FilterExpression='#cmd = :cmd AND #status = :status',
            ExpressionAttributeNames={'#cmd': 'command', '#status': 'status'},
            ExpressionAttributeValues={
                ':cmd': 'aws ec2 reboot-instances --instance-ids i-p1-1c',
                ':status': 'pending_approval',
            }
        )
        orphan_items = [i for i in scan_result.get('Items', [])
                        if i.get('reason') == 'P1-1 regression exception test']
        assert len(orphan_items) == 0, f"Orphan DDB records found: {orphan_items}"

    @patch('telegram.send_telegram_message')
    def test_upload_telegram_failure_ddb_no_record_returns_error(self, mock_telegram, mock_dynamodb, app_module):
        """Upload Telegram 失敗 → DDB 無 record，回 error ✅"""
        import base64
        mock_telegram.return_value = {}  # Telegram failure

        content_b64 = base64.b64encode(b'test file content').decode()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 903,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_upload',
                    'arguments': {
                        'filename': 'test-p1-1.txt',
                        'content': content_b64,
                        'content_type': 'text/plain',
                        'trust_scope': 'test-scope',
                        'reason': 'P1-1 upload failure test',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])

        assert content['status'] == 'error', f"Expected error, got: {content}"
        assert 'Telegram' in content.get('error', '') or 'notification' in content.get('error', '').lower()

        # Verify no orphan DDB record for upload
        scan_result = mock_dynamodb.Table('clawdbot-approval-requests').scan(
            FilterExpression='#reason = :reason AND #status = :status',
            ExpressionAttributeNames={'#reason': 'reason', '#status': 'status'},
            ExpressionAttributeValues={
                ':reason': 'P1-1 upload failure test',
                ':status': 'pending_approval',
            }
        )
        orphan_items = scan_result.get('Items', [])
        assert len(orphan_items) == 0, f"Orphan upload DDB records found: {orphan_items}"

    @patch('telegram.send_telegram_message')
    def test_upload_telegram_success_ddb_has_record_returns_pending(self, mock_telegram, mock_dynamodb, app_module):
        """Upload Telegram 成功 → DDB 有 record，回 pending_approval ✅"""
        import base64
        mock_telegram.return_value = {'ok': True, 'result': {'message_id': 2}}

        content_b64 = base64.b64encode(b'another test file').decode()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 904,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_upload',
                    'arguments': {
                        'filename': 'test-p1-1-ok.txt',
                        'content': content_b64,
                        'content_type': 'text/plain',
                        'trust_scope': 'test-scope',
                        'reason': 'P1-1 upload success test',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])

        assert content['status'] == 'pending_approval', f"Expected pending_approval, got: {content}"
        assert 'request_id' in content

        # Verify DDB record exists
        ddb_item = mock_dynamodb.Table('clawdbot-approval-requests').get_item(
            Key={'request_id': content['request_id']}
        ).get('Item')
        assert ddb_item is not None, "DDB record should exist when Telegram succeeds"
        assert ddb_item['status'] == 'pending_approval'
