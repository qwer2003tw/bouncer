"""Tests for utility functions and constants."""
import sys, os, json, pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

class TestHelperFunctions:
    """輔助函數測試"""
    
    def test_get_header_case_insensitive(self, app_module):
        """get_header 大小寫不敏感"""
        headers = {'Content-Type': 'application/json'}
        assert app_module.get_header(headers, 'content-type') == 'application/json'
        assert app_module.get_header(headers, 'CONTENT-TYPE') == 'application/json'
    
    def test_get_header_not_found(self, app_module):
        """get_header 找不到返回 None"""
        headers = {'X-Custom': 'value'}
        assert app_module.get_header(headers, 'X-Missing') is None
    
    def test_get_header_empty_dict(self, app_module):
        """get_header 空 dict"""
        assert app_module.get_header({}, 'any') is None


# ============================================================================
# MCP Result/Error 格式測試
# ============================================================================

class TestConstants:
    """常數驗證測試"""
    
    def test_blocked_patterns_not_empty(self, app_module):
        """BLOCKED_PATTERNS 不應為空"""
        from constants import BLOCKED_PATTERNS
        assert len(BLOCKED_PATTERNS) > 0
    
    def test_auto_approve_prefixes_not_empty(self, app_module):
        """AUTO_APPROVE_PREFIXES 不應為空"""
        from constants import AUTO_APPROVE_PREFIXES
        assert len(AUTO_APPROVE_PREFIXES) > 0
    
    def test_dangerous_patterns_not_empty(self, app_module):
        """DANGEROUS_PATTERNS 不應為空"""
        from constants import DANGEROUS_PATTERNS
        assert len(DANGEROUS_PATTERNS) > 0


# ============================================================================
# Account Validation Error Paths 測試 (562-592)
# ============================================================================

class TestHelperFunctionsAdditional:
    """Helper Functions 補充測試"""
    
    def test_generate_request_id(self, app_module):
        """產生請求 ID"""
        id1 = app_module.generate_request_id('aws s3 ls')
        id2 = app_module.generate_request_id('aws s3 ls')
        
        assert len(id1) == 12
        assert id1 != id2  # 應該每次產生不同的 ID
    
    def test_decimal_to_native_dict(self, app_module):
        """Decimal 轉換 - dict"""
        from decimal import Decimal
        data = {'count': Decimal('10'), 'value': Decimal('3.14')}
        result = app_module.decimal_to_native(data)
        assert result['count'] == 10
        assert result['value'] == 3.14
    
    def test_decimal_to_native_list(self, app_module):
        """Decimal 轉換 - list"""
        from decimal import Decimal
        data = [Decimal('1'), Decimal('2'), Decimal('3')]
        result = app_module.decimal_to_native(data)
        assert result == [1, 2, 3]
    
    def test_response_helper(self, app_module):
        """response 輔助函數"""
        result = app_module.response(200, {'test': 'data'})
        assert result['statusCode'] == 200
        assert 'Content-Type' in result['headers']
        body = json.loads(result['body'])
        assert body['test'] == 'data'


# ============================================================================
# Lambda Handler 路由測試
# ============================================================================

class TestDecimalConversion:
    """Decimal 轉換測試"""
    
    def test_decimal_to_native_int(self, app_module):
        """整數 Decimal"""
        result = app_module.decimal_to_native(Decimal('100'))
        assert result == 100
        assert isinstance(result, int)
    
    def test_decimal_to_native_float(self, app_module):
        """浮點數 Decimal"""
        result = app_module.decimal_to_native(Decimal('3.14'))
        assert result == 3.14
        assert isinstance(result, float)
    
    def test_decimal_to_native_dict(self, app_module):
        """字典中的 Decimal"""
        data = {'count': Decimal('5'), 'rate': Decimal('0.5')}
        result = app_module.decimal_to_native(data)
        assert result['count'] == 5
        assert result['rate'] == 0.5

