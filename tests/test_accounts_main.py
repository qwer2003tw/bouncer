"""
Auto-generated test file split from test_bouncer.py
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


class TestAccounts:
    """帳號管理模組測試"""
    
    def test_validate_account_id_valid(self, app_module):
        """有效的 12 位帳號 ID"""
        valid, error = app_module.validate_account_id('123456789012')
        assert valid is True
        assert error is None
    
    def test_validate_account_id_empty(self, app_module):
        """空帳號 ID"""
        valid, error = app_module.validate_account_id('')
        assert valid is False
        assert '不能為空' in error
    
    def test_validate_account_id_not_digit(self, app_module):
        """非數字帳號 ID"""
        valid, error = app_module.validate_account_id('abc123456789')
        assert valid is False
        assert '數字' in error
    
    def test_validate_account_id_wrong_length(self, app_module):
        """長度不對的帳號 ID"""
        valid, error = app_module.validate_account_id('12345')
        assert valid is False
        assert '12 位' in error
    
    def test_validate_role_arn_empty(self, app_module):
        """空 Role ARN（允許）"""
        valid, error = app_module.validate_role_arn('')
        assert valid is True
        assert error is None
    
    def test_validate_role_arn_none(self, app_module):
        """None Role ARN（允許）"""
        valid, error = app_module.validate_role_arn(None)
        assert valid is True
        assert error is None
    
    def test_validate_role_arn_valid(self, app_module):
        """有效的 Role ARN"""
        valid, error = app_module.validate_role_arn('arn:aws:iam::123456789012:role/MyRole')
        assert valid is True
        assert error is None
    
    def test_validate_role_arn_invalid_prefix(self, app_module):
        """無效前綴"""
        valid, error = app_module.validate_role_arn('invalid:arn')
        assert valid is False
        assert 'arn:aws:iam' in error
    
    def test_validate_role_arn_missing_role(self, app_module):
        """缺少 :role/"""
        valid, error = app_module.validate_role_arn('arn:aws:iam::123456789012:user/MyUser')
        assert valid is False
        assert ':role/' in error


# ============================================================================
# Trust 模組測試（補充）
# ============================================================================



class TestAccountsModuleFull:
    """Accounts 模組完整測試"""
    
    def test_list_accounts_empty(self, app_module):
        """列出帳號（空）"""
        accounts = app_module.list_accounts()
        # 可能為空或有預設帳號
        assert isinstance(accounts, list)
    
    def test_init_default_account(self, app_module):
        """初始化預設帳號"""
        # 呼叫不應出錯
        app_module.init_default_account()


# ============================================================================
# MCP Tool - Add Account 完整測試  
# ============================================================================



class TestAccountsMore:
    """Accounts 更多測試"""
    
    def test_validate_account_id_too_short(self, app_module):
        """帳號 ID 太短"""
        from app import validate_account_id
        valid, error = validate_account_id('123')
        assert valid is False
        assert 'must be 12 digits' in error.lower() or '12' in error
    
    def test_validate_account_id_non_numeric(self, app_module):
        """帳號 ID 非數字"""
        from app import validate_account_id
        valid, error = validate_account_id('12345678901a')
        assert valid is False




class TestAccountValidationErrorPaths:
    """帳號驗證錯誤路徑測試"""
    
    def test_execute_invalid_account_id_format(self, app_module):
        """帳號 ID 格式錯誤"""
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
                        'command': 'aws s3 ls',
                        'trust_scope': 'test-session',
                        'account': 'invalid'  # 非 12 位數字 (注意：參數名是 account 不是 account_id)
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '12 位' in content['error'] or '數字' in content['error']
        assert body['result'].get('isError', False) == True
    
    def test_execute_account_not_configured(self, app_module):
        """帳號未配置"""
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
                        'command': 'aws s3 ls',
                        'trust_scope': 'test-session',
                        'account': '999999999999'  # 不存在的帳號
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        # Mock get_account 返回 None
        with patch('accounts.get_account', return_value=None), \
             patch.object(app_module, 'list_accounts', return_value=[{'account_id': '123456789012'}]):
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'error'
            assert '未配置' in content['error']
            assert 'available_accounts' in content
            assert body['result'].get('isError', False) == True
    
    def test_execute_account_disabled(self, app_module):
        """帳號已停用"""
        import mcp_tools
        import mcp_execute
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
                        'command': 'aws s3 ls',
                        'trust_scope': 'test-session',
                        'account': '123456789012'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        # Mock get_account 返回停用的帳號
        with patch.object(mcp_execute, 'get_account', return_value={
            'account_id': '123456789012',
            'name': 'Test',
            'enabled': False
        }):
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'error'
            assert '停用' in content['error']
            assert body['result'].get('isError', False) == True


# ============================================================================
# BLOCKED Command Path 測試 (627-631)
# ============================================================================


