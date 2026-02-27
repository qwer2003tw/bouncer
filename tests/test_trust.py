"""
test_trust.py — Trust session 功能測試
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
# Trust Session 測試
# ============================================================================

class TestTrustSession:
    """Trust Session 測試"""
    
    def test_should_trust_approve_no_session(self, app_module):
        """沒有 Trust Session 時應該返回 False"""
        should_trust, session, reason = app_module.should_trust_approve(
            'aws ec2 describe-instances',
            'test-source',
            '111111111111'
        )
        assert should_trust is False
        assert session is None
    
    def test_trust_excluded_services(self, app_module):
        """高危服務應該被排除"""
        # IAM 命令不應該被信任
        assert app_module.is_trust_excluded('aws iam list-users') is True
        assert app_module.is_trust_excluded('aws sts get-caller-identity') is True
        assert app_module.is_trust_excluded('aws kms list-keys') is True
        
        # 安全命令可以被信任
        assert app_module.is_trust_excluded('aws ec2 describe-instances') is False
        assert app_module.is_trust_excluded('aws s3 ls') is False
    
    def test_trust_excluded_actions(self, app_module):
        """高危操作應該被排除"""
        # 刪除操作
        assert app_module.is_trust_excluded('aws ec2 delete-vpc --vpc-id vpc-123') is True
        assert app_module.is_trust_excluded('aws s3 rm s3://bucket/key') is True
        
        # 終止操作
        assert app_module.is_trust_excluded('aws ec2 terminate-instances --instance-ids i-123') is True
        
        # 停止操作
        assert app_module.is_trust_excluded('aws ec2 stop-instances --instance-ids i-123') is True
    
    def test_trust_excluded_flags(self, app_module):
        """危險旗標應該被排除"""
        # --force
        assert app_module.is_trust_excluded('aws s3 rm s3://bucket --force') is True
        
        # --recursive
        assert app_module.is_trust_excluded('aws s3 rm s3://bucket --recursive') is True
        
        # --skip-final-snapshot
        assert app_module.is_trust_excluded('aws rds delete-db-instance --skip-final-snapshot') is True
        
        # 安全命令
        assert app_module.is_trust_excluded('aws s3 ls s3://bucket') is False


# ============================================================================
# Trust 排除規則測試
# ============================================================================

class TestTrustExcluded:
    """Trust 排除規則測試"""
    
    def test_is_trust_excluded_iam(self, app_module):
        """IAM 命令應被排除"""
        from trust import is_trust_excluded
        assert is_trust_excluded('aws iam create-user --user-name test') is True
    
    def test_is_trust_excluded_kms(self, app_module):
        """KMS 命令應被排除"""
        from trust import is_trust_excluded
        assert is_trust_excluded('aws kms create-key') is True
    
    def test_is_trust_excluded_delete(self, app_module):
        """delete 操作應被排除"""
        from trust import is_trust_excluded
        assert is_trust_excluded('aws s3 rm s3://bucket/key') is True
        assert is_trust_excluded('aws ec2 delete-security-group --group-id sg-123') is True
    
    def test_is_trust_excluded_terminate(self, app_module):
        """terminate 操作應被排除"""
        from trust import is_trust_excluded
        assert is_trust_excluded('aws ec2 terminate-instances --instance-ids i-123') is True
    
    def test_is_trust_excluded_force_flag(self, app_module):
        """--force 旗標應被排除"""
        from trust import is_trust_excluded
        assert is_trust_excluded('aws s3 rb s3://bucket --force') is True
    
    def test_is_trust_excluded_safe_command(self, app_module):
        """安全命令不應被排除"""
        from trust import is_trust_excluded
        assert is_trust_excluded('aws s3 ls') is False
        assert is_trust_excluded('aws ec2 describe-instances') is False


# ============================================================================
# Trust Command Handler 測試
# ============================================================================

class TestTrustCommandHandler:
    """Trust 命令處理測試"""
    
    def test_handle_trust_command(self, app_module):
        """trust 命令"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_trust_command('12345')
            assert result['statusCode'] == 200


# ============================================================================
# Trust 模組補充測試
# ============================================================================

class TestTrustModuleAdditional:
    """Trust 模組補充測試"""
    
    def test_create_trust_session(self, app_module):
        """建立信任時段"""
        trust_id = app_module.create_trust_session('test-source', '111111111111', '999999999')
        assert trust_id is not None
        
        # 驗證可以在 DynamoDB 中找到
        item = app_module.table.get_item(Key={'request_id': trust_id}).get('Item')
        assert item is not None
        assert item['type'] == 'trust_session'
    
    def test_should_trust_approve_with_active_session(self, app_module):
        """有活躍信任時段時應該自動批准"""
        source = 'test-trust-source'
        account_id = '111111111111'
        
        # 建立信任時段
        app_module.create_trust_session(source, account_id, '999999999')
        
        # 測試安全命令是否會被信任
        should_trust, session, reason = app_module.should_trust_approve(
            'aws s3 cp file.txt s3://bucket/',  # 非高危命令
            source,
            account_id
        )
        assert should_trust is True
        assert session is not None
    
    def test_should_trust_approve_excluded_command(self, app_module):
        """高危命令不應被信任"""
        source = 'test-trust-source-2'
        account_id = '111111111111'
        
        # 建立信任時段
        app_module.create_trust_session(source, account_id, '999999999')
        
        # 測試高危命令
        should_trust, session, reason = app_module.should_trust_approve(
            'aws ec2 terminate-instances --instance-ids i-123',  # 高危
            source,
            account_id
        )
        assert should_trust is False
    
    def test_revoke_trust_session(self, app_module):
        """撤銷信任時段"""
        # 建立
        trust_id = app_module.create_trust_session('revoke-source', '111111111111', '999999999')
        
        # 撤銷
        result = app_module.revoke_trust_session(trust_id)
        assert result is True
        
        # 確認已撤銷
        item = app_module.table.get_item(Key={'request_id': trust_id}).get('Item')
        assert item is None or item.get('expires_at', float('inf')) < time.time()


# ============================================================================
# Trust 模組完整測試
# ============================================================================

class TestTrustModuleFull:
    """Trust 模組完整測試"""
    
    def test_increment_trust_command_count(self, app_module):
        """增加信任命令計數"""
        # 先建立信任時段
        trust_id = app_module.create_trust_session('count-test', '111111111111', '999999999')
        
        # 增加計數
        new_count = app_module.increment_trust_command_count(trust_id)
        assert new_count == 1
        
        # 再增加
        new_count = app_module.increment_trust_command_count(trust_id)
        assert new_count == 2
    
    def test_should_trust_approve_excluded_iam(self, app_module):
        """IAM 命令不應被信任批准"""
        # 建立信任時段
        source = 'iam-test-source'
        app_module.create_trust_session(source, '111111111111', '999999999')
        
        # IAM 命令不應被信任
        should_trust, session, reason = app_module.should_trust_approve(
            'aws iam list-users',
            source,
            '111111111111'
        )
        assert should_trust is False


# ============================================================================
# Trust 更多測試
# ============================================================================

class TestTrustMore:
    """Trust 更多測試"""
    
    def test_mcp_trust_status_empty(self, app_module):
        """無信任時段"""
        result = app_module.mcp_tool_trust_status('test-1', {})
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['active_sessions'] == 0
    
    def test_mcp_trust_revoke_missing_id(self, app_module):
        """撤銷缺少 ID"""
        result = app_module.mcp_tool_trust_revoke('test-1', {})
        body = json.loads(result['body'])
        assert 'error' in body


# ============================================================================
# Trust Session 自動批准測試
# ============================================================================

class TestTrustAutoApprove:
    """Trust Session 自動批准測試"""
    
    @patch('telegram.send_telegram_message_silent')
    def test_trust_auto_approve_flow(self, mock_silent, app_module):
        """信任時段內的自動批准流程"""
        import mcp_execute
        import mcp_tools
        source = 'trust-auto-test'
        account_id = '111111111111'
        
        # 建立信任時段
        trust_id = app_module.create_trust_session(source, account_id, '999999999')
        
        # 執行命令（應該被自動批准）
        with patch.object(mcp_execute, 'execute_command', return_value='{"result": "ok"}'):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test-1',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_execute',
                        'arguments': {
                            'command': 'aws s3 cp file.txt s3://bucket/',
                            'trust_scope': source,
                            'source': source,
                            'account': account_id
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            # Mock get_account 返回有效帳號
            with patch.object(mcp_execute, 'get_account', return_value={
                'account_id': account_id,
                'name': 'Test',
                'enabled': True,
                'role_arn': None
            }):
                result = app_module.lambda_handler(event, None)
                body = json.loads(result['body'])
                
                content = json.loads(body['result']['content'][0]['text'])
                assert content['status'] == 'trust_auto_approved'
                assert 'trust_session' in content


# ============================================================================
# Trust Session 邊界條件測試
# ============================================================================

class TestTrustSessionLimits:
    """測試信任時段的邊界條件"""

    def test_trust_session_expired(self, app_module):
        """信任時段已過期 → should_trust_approve 返回 False"""
        from trust import should_trust_approve

        # 建立已過期的信任時段
        app_module.table.put_item(Item={
            'request_id': 'trust-0d41c6bf4532be5b-111111111111',
            'type': 'trust_session',
            'source': 'test-source-expired',
            'trust_scope': 'test-source-expired',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()) - 700,
            'expires_at': int(time.time()) - 100,  # 已過期
            'command_count': 0,
        })

        should, session, reason = should_trust_approve(
            'aws ec2 describe-instances', 'test-source-expired', '111111111111'
        )
        assert should is False
        assert 'expired' in reason.lower() or 'No active' in reason

    def test_trust_session_command_limit_reached(self, app_module):
        """命令數達上限 → should_trust_approve 返回 False"""
        from trust import should_trust_approve
        from constants import TRUST_SESSION_MAX_COMMANDS

        # 建立已達上限的信任時段
        app_module.table.put_item(Item={
            'request_id': 'trust-efb587eb4f037ac7-111111111111',
            'type': 'trust_session',
            'source': 'test-source-maxed',
            'trust_scope': 'test-source-maxed',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': TRUST_SESSION_MAX_COMMANDS,  # 已達上限
        })

        should, session, reason = should_trust_approve(
            'aws ec2 describe-instances', 'test-source-maxed', '111111111111'
        )
        assert should is False
        assert 'limit' in reason.lower()

    def test_trust_session_excluded_high_risk(self, app_module):
        """高危命令排除 → 即使在信任中也返回 False"""
        from trust import should_trust_approve

        # 建立有效的信任時段
        app_module.table.put_item(Item={
            'request_id': 'trust-042fefdf8d5cf4b5-111111111111',
            'type': 'trust_session',
            'source': 'test-source-excluded',
            'trust_scope': 'test-source-excluded',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': 0,
        })

        # IAM 操作即使在信任中也應被排除
        should, session, reason = should_trust_approve(
            'aws iam create-user --user-name hacker', 'test-source-excluded', '111111111111'
        )
        assert should is False
        assert 'excluded' in reason.lower() or 'trust' in reason.lower()


# ============================================================================
# Trust session expiry and limits (T-4)
# ============================================================================

class TestTrustSessionExpiry:
    """Trust session expiry and limits (T-4)."""

    @pytest.fixture(autouse=True)
    def setup(self, app_module):
        self.app = app_module
        import trust as trust_mod
        self.trust = trust_mod
        # Reset trust module table reference
        trust_mod._table = None

    def test_expired_trust_session_not_approved(self, app_module):
        """Expired trust session should NOT auto-approve."""
        # Create an already-expired trust session
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-37b1ddd649ff2758-111111111111',
            'type': 'trust_session',
            'source': 'expire-test',
            'trust_scope': 'expire-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()) - 700,
            'expires_at': int(time.time()) - 10,
            'command_count': 0,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws s3 ls', 'expire-test', '111111111111'
        )
        assert should is False

    def test_max_commands_trust_session_not_approved(self, app_module):
        """Trust session at max commands should NOT auto-approve."""
        from constants import TRUST_SESSION_MAX_COMMANDS
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-18bb6f0eae17a70a-111111111111',
            'type': 'trust_session',
            'source': 'maxcmd-test',
            'trust_scope': 'maxcmd-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': TRUST_SESSION_MAX_COMMANDS,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws s3 ls', 'maxcmd-test', '111111111111'
        )
        assert should is False
        assert 'limit' in reason.lower()

    def test_excluded_command_not_trusted(self, app_module):
        """High-risk commands should NOT be trusted even in active session."""
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-cc46a32017401146-111111111111',
            'type': 'trust_session',
            'source': 'exclude-test',
            'trust_scope': 'exclude-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': 0,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws iam create-user --user-name hacker', 'exclude-test', '111111111111'
        )
        assert should is False

    def test_valid_trust_session_approved(self, app_module):
        """Valid trust session with safe command should auto-approve."""
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-b52d169fa85badb4-111111111111',
            'type': 'trust_session',
            'source': 'valid-test',
            'trust_scope': 'valid-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': 0,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws s3 cp file.txt s3://bucket/', 'valid-test', '111111111111'
        )
        assert should is True
        assert 'active' in reason.lower()


# ============================================================================
# Callback Trust 測試
# ============================================================================

class TestCallbackTrust:
    """Callback Trust 測試"""
    
    @patch('app.execute_command')
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_approve_with_trust(self, mock_answer, mock_update, mock_exec, app_module):
        """批准並建立信任"""
        mock_exec.return_value = '{"result": "ok"}'
        
        request_id = 'trust-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending_approval',
            'source': 'test-trust-source',
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
                    'id': 'cb-trust',
                    'from': {'id': 999999999},
                    'data': f'approve_trust:{request_id}',
                    'message': {'message_id': 2000}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
