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


class TestOutputPaging:
    """Output Paging 測試"""
    
    def test_short_output_not_paged(self, app_module):
        """短輸出不應該分頁"""
        result = app_module.store_paged_output('req-123', 'short output')
        assert result['paged'] is False
        assert result['result'] == 'short output'
    
    def test_long_output_is_paged(self, app_module):
        """長輸出應該分頁"""
        long_output = 'x' * 5000  # 超過 OUTPUT_MAX_INLINE
        result = app_module.store_paged_output('req-456', long_output)
        
        assert result['paged'] is True
        assert result['page'] == 1
        assert result['total_pages'] == 2  # 5000 / 3000 = 2 頁
        assert result['output_length'] == 5000
        assert result['next_page'] == 'req-456:page:2'
        assert len(result['result']) == 3000  # 第一頁
    
    def test_get_paged_output(self, app_module):
        """測試取得分頁輸出"""
        # 先存一個長輸出
        long_output = 'A' * 3000 + 'B' * 3000  # 6000 字元，2 頁
        app_module.store_paged_output('req-789', long_output)
        
        # 取得第 2 頁
        result = app_module.get_paged_output('req-789:page:2')
        
        assert 'error' not in result
        assert result['page'] == 2
        assert result['total_pages'] == 2
        assert result['result'] == 'B' * 3000
        assert result['next_page'] is None  # 最後一頁
    
    def test_get_nonexistent_page(self, app_module):
        """測試取得不存在的分頁"""
        result = app_module.get_paged_output('nonexistent:page:99')
        assert 'error' in result


# ============================================================================
# 命令分類測試（補充）
# ============================================================================



class TestPagingModule:
    """Paging 模組測試"""
    
    def test_store_paged_output_short(self, app_module):
        """短輸出不分頁"""
        result = app_module.store_paged_output('test-req-1', 'short output')
        assert result['paged'] is False
        assert result['result'] == 'short output'
    
    def test_store_paged_output_long(self, app_module):
        """長輸出分頁"""
        long_output = 'x' * 5000  # 超過 OUTPUT_MAX_INLINE
        result = app_module.store_paged_output('test-req-2', long_output)
        assert result['paged'] is True
        assert result['page'] == 1
        assert result['total_pages'] >= 2


# ============================================================================
# Commands 模組測試（補充）
# ============================================================================



class TestPagingModuleFull:
    """Paging 模組完整測試"""
    
    def test_store_paged_output_multiple_pages(self, app_module):
        """儲存多頁輸出"""
        # 建立需要分成多頁的長輸出
        long_output = 'A' * 3500 + 'B' * 3500 + 'C' * 3500  # 約 10500 字元
        result = app_module.store_paged_output('multi-page-test', long_output)
        
        assert result['paged'] == True
        assert result['total_pages'] >= 3
        assert result['page'] == 1
        assert result['next_page'] == 'multi-page-test:page:2'
    
    def test_get_paged_output_success(self, app_module):
        """成功取得分頁"""
        # 先存一個分頁
        app_module.table.put_item(Item={
            'request_id': 'page-get-test:page:2',
            'content': 'Page 2 content',
            'page': 2,
            'total_pages': 3,
            'original_request': 'page-get-test'
        })
        
        result = app_module.get_paged_output('page-get-test:page:2')
        
        assert 'error' not in result
        assert result['page'] == 2
        assert result['total_pages'] == 3
        assert result['result'] == 'Page 2 content'
        assert result['next_page'] == 'page-get-test:page:3'
    
    def test_get_paged_output_last_page(self, app_module):
        """取得最後一頁（沒有 next_page）"""
        app_module.table.put_item(Item={
            'request_id': 'last-page-test:page:3',
            'content': 'Last page',
            'page': 3,
            'total_pages': 3,
            'original_request': 'last-page-test'
        })
        
        result = app_module.get_paged_output('last-page-test:page:3')
        
        assert result['page'] == 3
        assert result['next_page'] is None


# ============================================================================
# Telegram 模組完整測試
# ============================================================================



class TestPagingMore:
    """Paging 更多測試"""
    
    def test_send_remaining_pages(self, app_module):
        """發送剩餘分頁"""
        # 建立分頁資料
        app_module.table.put_item(Item={
            'request_id': 'send-pages-test:page:2',
            'content': 'Page 2',
            'page': 2,
            'total_pages': 2
        })
        
        with patch('paging.send_telegram_message_silent') as mock_send:
            from paging import send_remaining_pages
            send_remaining_pages('send-pages-test', 2)
            # 應該嘗試發送（即使失敗）
    
    def test_send_remaining_pages_single(self, app_module):
        """單頁不需要發送"""
        with patch('paging.send_telegram_message_silent') as mock_send:
            from paging import send_remaining_pages
            send_remaining_pages('single-page', 1)
            mock_send.assert_not_called()


# ============================================================================
# Lambda Handler 更多路由測試
# ============================================================================



class TestPagingMoreExtended:
    """Paging 更多測試"""
    
    def test_get_page_not_found(self, app_module):
        """MCP get_page 找不到"""
        result = app_module.mcp_tool_get_page('test-1', {'page_id': 'nonexistent-page-xyz'})
        body = json.loads(result['body'])
        # 頁面不存在應該返回 isError 或錯誤訊息
        content = json.loads(body['result']['content'][0]['text'])
        assert 'error' in content or body['result'].get('isError') is True




class TestPagedOutputAdditional:
    """Paged Output 補充測試"""
    
    def test_get_paged_output_invalid_format(self, app_module):
        """無效格式的 page_id"""
        result = app_module.get_paged_output('invalid-format')
        assert 'error' in result
    
    def test_store_paged_output_with_pages(self, app_module):
        """儲存需要分頁的輸出"""
        long_output = 'x' * 10000  # 超過 OUTPUT_MAX_INLINE
        result = app_module.store_paged_output('paged-test', long_output)
        
        assert result['paged'] == True
        assert result['total_pages'] >= 3


# ============================================================================
# Telegram Message 功能測試
# ============================================================================


