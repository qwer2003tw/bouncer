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


class TestRateLimiting:
    """Rate Limiting 測試"""
    
    def test_rate_limit_not_exceeded(self, app_module):
        """正常請求不應該觸發 rate limit"""
        # 第一次請求應該通過
        try:
            app_module.check_rate_limit('test-source-1')
        except (app_module.RateLimitExceeded, app_module.PendingLimitExceeded):
            pytest.fail("Rate limit should not be exceeded on first request")

    def test_rate_limit_disabled(self, app_module):
        """停用 rate limit 時應該跳過檢查"""
        with patch('rate_limit.RATE_LIMIT_ENABLED', False):
            # 即使呼叫多次也不應該觸發
            for _ in range(20):
                app_module.check_rate_limit('test-source-2')


# ============================================================================
# Output Paging 測試
# ============================================================================



class TestRateLimitEdgeCases:
    """Rate limit 邊界測試"""
    
    def test_check_rate_limit_new_source(self, app_module):
        """新 source 不應被限制"""
        import uuid
        unique_source = f'test-source-{uuid.uuid4()}'
        try:
            app_module.check_rate_limit(unique_source)
        except Exception as e:
            # 可能因為 DynamoDB 查詢失敗，但不應該是 rate limit 錯誤
            assert 'rate' not in str(e).lower() or 'limit' not in str(e).lower()


# ============================================================================
# Constants 驗證測試
# ============================================================================



class TestRateLimitErrors:
    """Rate Limit 錯誤路徑測試"""
    
    def test_rate_limit_exceeded_error(self, app_module):
        """Rate limit 超過應返回錯誤"""
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
                        'command': 'aws ec2 start-instances --instance-ids i-123',
                        'trust_scope': 'test-session',
                        'source': 'test-source'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        # Mock check_rate_limit 拋出 RateLimitExceeded
        with patch.object(mcp_execute, 'check_rate_limit', 
                          side_effect=mcp_execute.RateLimitExceeded('Rate limit exceeded')):
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'rate_limited'
            assert body['result']['isError'] == True
    
    def test_pending_limit_exceeded_error(self, app_module):
        """Pending limit 超過應返回錯誤"""
        import mcp_tools
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
                        'command': 'aws ec2 start-instances --instance-ids i-123',
                        'trust_scope': 'test-session',
                        'source': 'test-source'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        # Mock check_rate_limit 拋出 PendingLimitExceeded
        with patch('mcp_execute.check_rate_limit',
                          side_effect=app_module.PendingLimitExceeded('Too many pending')):
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'pending_limit_exceeded'
            assert 'hint' in content


# ============================================================================
# Telegram Callback Handlers 測試 (796-1114)
# ============================================================================



class TestRateLimitFull:
    """Rate Limit 完整測試"""
    
    def test_check_rate_limit_first_request(self, app_module):
        """第一次請求不應被限制"""
        import uuid
        source = f'rate-limit-test-{uuid.uuid4()}'
        
        # 不應拋出異常
        app_module.check_rate_limit(source)
    
    def test_check_rate_limit_none_source(self, app_module):
        """None source 應該跳過檢查"""
        # 不應拋出異常
        app_module.check_rate_limit(None)


# ============================================================================
# Deployer 完整測試
# ============================================================================



class TestRateLimitMore:
    """Rate Limit 更多測試"""
    
    def test_check_rate_limit_new_source(self, app_module):
        """新來源的 rate limit 檢查"""
        from rate_limit import check_rate_limit, RateLimitExceeded, PendingLimitExceeded
        # 不應該拋出異常
        try:
            check_rate_limit('brand-new-source-' + str(time.time()))
        except (RateLimitExceeded, PendingLimitExceeded):
            pytest.fail("Should not raise limit exception for new source")
    
    def test_check_rate_limit_repeated(self, app_module):
        """重複來源的 rate limit 檢查"""
        from rate_limit import check_rate_limit
        source = 'repeat-source-' + str(time.time())
        # 多次呼叫不應該拋出異常（在限制內）
        check_rate_limit(source)
        check_rate_limit(source)



