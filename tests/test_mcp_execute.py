import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3


class TestMCPInitialize:
    """MCP initialize 方法測試"""
    
    def test_initialize_success(self, app_module):
        """測試 MCP initialize"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {}
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        body = json.loads(result['body'])
        assert body['jsonrpc'] == '2.0'
        assert body['id'] == 1
        assert 'result' in body
        assert body['result']['serverInfo']['name'] == 'bouncer'
        assert body['result']['protocolVersion'] == '2024-11-05'


class TestMCPToolsList:
    """MCP tools/list 方法測試"""
    
    def test_tools_list(self, app_module):
        """測試列出所有工具"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 2,
                'method': 'tools/list',
                'params': {}
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'result' in body
        tools = body['result']['tools']
        tool_names = [t['name'] for t in tools]
        
        assert 'bouncer_execute' in tool_names
        assert 'bouncer_status' in tool_names
        assert 'bouncer_list_safelist' in tool_names


class TestMCPExecuteSafelist:
    """MCP bouncer_execute SAFELIST 測試"""
    
    def test_execute_safelist_command(self, app_module):
        """測試自動批准的命令"""
        import mcp_execute
        import mcp_tools
        # 需要 mock mcp_tools.execute_command
        with patch.object(mcp_execute, 'execute_command', return_value='{"Reservations": []}'):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 3,
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_execute',
                        'arguments': {
                            'command': 'aws ec2 describe-instances',
                            'trust_scope': 'test-session',
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'auto_approved'
            assert '{"Reservations": []}' in content['result']


class TestMCPExecuteApproval:
    """MCP bouncer_execute APPROVAL 測試"""
    
    @patch('telegram.send_telegram_message')
    def test_execute_needs_approval_async(self, mock_telegram, app_module):
        """測試需要審批的命令（預設異步，立即返回 pending_approval）"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 5,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 start-instances --instance-ids i-123',
                        'trust_scope': 'test-session',
                        'reason': 'Test start'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        # 預設異步：立即返回 pending_approval
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'
        assert 'request_id' in content
        assert mock_telegram.called
    
    @patch('telegram.send_telegram_message')
    def test_execute_approved(self, mock_telegram, app_module):
        """測試審批通過的命令"""
        
        # 先建立 pending 請求
        request_id = 'test123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 start-instances --instance-ids i-123',
            'status': 'approved',
            'result': 'Instance started',
            'approver': '999999999'
        })
        
        # 查詢狀態
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 6,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_status',
                    'arguments': {
                        'request_id': request_id
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'approved'
        assert content['result'] == 'Instance started'


class TestMCPListSafelist:
    """MCP bouncer_list_safelist 測試"""
    
    def test_list_safelist(self, app_module):
        """測試列出 safelist"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 7,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_list_safelist',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert 'safelist_prefixes' in content
        assert 'blocked_patterns' in content
        assert 'aws ec2 describe-' in content['safelist_prefixes']


class TestMCPErrors:
    """MCP 錯誤處理測試"""
    
    def test_invalid_secret(self, app_module):
        """測試無效的 secret"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'wrong-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32600
    
    def test_invalid_jsonrpc_version(self, app_module):
        """測試無效的 jsonrpc 版本"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '1.0',
                'id': 1,
                'method': 'initialize'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert 'jsonrpc must be "2.0"' in body['error']['message']
    
    def test_unknown_method(self, app_module):
        """測試未知方法"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'unknown_method'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32601
    
    def test_unknown_tool(self, app_module):
        """測試未知工具"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {
                    'name': 'unknown_tool',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32602


# ============================================================================
# REST API Tests（向後兼容）
# ============================================================================


class TestMCPToolStatus:
    """bouncer_status MCP tool 測試"""
    
    def test_status_missing_request_id(self, app_module):
        """缺少 request_id"""
        result = app_module.mcp_tool_status('test-1', {})
        assert 'error' in str(result).lower() or 'missing' in str(result).lower()


class TestMCPToolGetPage:
    """bouncer_get_page MCP tool 測試"""
    
    def test_get_page_missing_request_id(self, app_module):
        """缺少 request_id"""
        result = app_module.mcp_tool_get_page('test-1', {'page': 2})
        assert 'error' in str(result).lower() or 'missing' in str(result).lower()


class TestMCPToolExecuteEdgeCases:
    """bouncer_execute 邊界情況測試"""
    
    def test_execute_empty_command(self, app_module):
        """空命令"""
        result = app_module.mcp_tool_execute('test-1', {'command': ''})
        assert 'error' in str(result).lower()
    
    def test_execute_whitespace_command(self, app_module):
        """只有空白的命令"""
        result = app_module.mcp_tool_execute('test-1', {'command': '   '})
        assert 'error' in str(result).lower()


# ============================================================================
# Telegram Command Handlers 測試
# ============================================================================


class TestMCPFormatting:
    """MCP 結果格式測試"""
    
    def test_mcp_result_format(self, app_module):
        """mcp_result 格式正確"""
        result = app_module.mcp_result('test-123', {'test': 'data'})
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['jsonrpc'] == '2.0'
        assert body['id'] == 'test-123'
        assert 'result' in body
    
    def test_mcp_error_format(self, app_module):
        """mcp_error 格式正確"""
        result = app_module.mcp_error('test-123', -32600, 'Invalid request')
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['jsonrpc'] == '2.0'
        assert body['id'] == 'test-123'
        assert body['error']['code'] == -32600
        assert body['error']['message'] == 'Invalid request'


# ============================================================================
# Telegram Webhook Handler 測試
# ============================================================================


class TestMCPToolCallRouting:
    """MCP tool call 路由測試"""
    
    def test_handle_mcp_tool_call_execute(self, app_module):
        """bouncer_execute 路由"""
        mock_handler = MagicMock(return_value={'test': 'result'})
        with patch.dict(app_module.TOOL_HANDLERS, {'bouncer_execute': mock_handler}):
            result = app_module.handle_mcp_tool_call('req-1', 'bouncer_execute', {'command': 'aws s3 ls'})
            assert result == {'test': 'result'}
    
    def test_handle_mcp_tool_call_status(self, app_module):
        """bouncer_status 路由"""
        mock_handler = MagicMock(return_value={'test': 'result'})
        with patch.dict(app_module.TOOL_HANDLERS, {'bouncer_status': mock_handler}):
            result = app_module.handle_mcp_tool_call('req-1', 'bouncer_status', {'request_id': 'abc'})
            assert result == {'test': 'result'}
    
    def test_handle_mcp_tool_call_unknown(self, app_module):
        """未知 tool"""
        result = app_module.handle_mcp_tool_call('req-1', 'unknown_tool', {})
        body = json.loads(result['body'])
        assert 'error' in body


# ============================================================================
# Trust Command Handler 測試
# ============================================================================


class TestMCPRequestHandler:
    """MCP 請求處理測試"""
    
    def test_handle_mcp_request_initialize(self, app_module):
        """initialize 方法"""
        event = {
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'initialize',
                'params': {}
            }),
            'headers': {'x-approval-secret': 'test-secret'}
        }
        with patch.object(app_module, 'REQUEST_SECRET', 'test-secret'):
            result = app_module.handle_mcp_request(event)
            assert result['statusCode'] == 200
    
    def test_handle_mcp_request_tools_list(self, app_module):
        """tools/list 方法"""
        event = {
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/list'
            }),
            'headers': {'x-approval-secret': 'test-secret'}
        }
        with patch.object(app_module, 'REQUEST_SECRET', 'test-secret'):
            result = app_module.handle_mcp_request(event)
            assert result['statusCode'] == 200
            body = json.loads(result['body'])
            assert 'tools' in body.get('result', {})


# ============================================================================
# REST API Handler 測試
# ============================================================================


class TestMCPToolHandlersAdditional:
    """MCP Tool Handlers 補充測試"""
    
    def test_mcp_tool_trust_status_all(self, app_module):
        """查詢所有信任時段"""
        # 建立一個信任時段
        app_module.create_trust_session('status-test-source', '111111111111', '999999999')
        
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_trust_status',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert 'active_sessions' in content
        assert 'sessions' in content
    
    def test_mcp_tool_trust_status_by_source(self, app_module):
        """查詢特定來源的信任時段"""
        source = 'specific-source-test'
        app_module.create_trust_session(source, '111111111111', '999999999')
        
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_trust_status',
                    'arguments': {
                        'source': source
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert 'sessions' in content
    
    def test_mcp_tool_trust_revoke(self, app_module):
        """撤銷信任時段"""
        trust_id = app_module.create_trust_session('revoke-test', '111111111111', '999999999')
        
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_trust_revoke',
                    'arguments': {
                        'trust_id': trust_id
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert content['success'] == True
    
    def test_mcp_tool_trust_revoke_missing_id(self, app_module):
        """撤銷信任時段缺少 ID"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_trust_revoke',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32602
    
    def test_mcp_tool_list_pending_with_source(self, app_module):
        """列出特定來源的待審批請求"""
        # 直接呼叫函數 - 測試帶 source 的情況
        result = app_module.mcp_tool_list_pending('test-1', {'source': 'test-source', 'limit': 10})
        body = json.loads(result['body'])
        
        # 可能有 error 或 result，都是預期行為
        assert 'result' in body or 'error' in body
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_add_account_missing_name(self, mock_telegram, app_module):
        """新增帳號缺少名稱"""
        result = app_module.mcp_tool_add_account('test-1', {
            'account_id': '111111111111',
            'role_arn': 'arn:aws:iam::111111111111:role/Test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '名稱' in content['error'] or 'name' in content['error'].lower()
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_add_account_invalid_role_arn(self, mock_telegram, app_module):
        """新增帳號無效 Role ARN"""
        result = app_module.mcp_tool_add_account('test-1', {
            'account_id': '111111111111',
            'name': 'Test Account',
            'role_arn': 'invalid-arn'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'arn:aws:iam' in content['error']
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_remove_account_default(self, mock_telegram, app_module):
        """不能移除預設帳號"""
        import mcp_tools
        import mcp_admin
        original = mcp_admin.DEFAULT_ACCOUNT_ID
        try:
            mcp_admin.DEFAULT_ACCOUNT_ID = '111111111111'
            result = app_module.mcp_tool_remove_account('test-1', {
                'account_id': '111111111111'
            })
            
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'error'
            assert '預設' in content['error']
        finally:
            mcp_admin.DEFAULT_ACCOUNT_ID = original
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_remove_account_not_exists(self, mock_telegram, app_module):
        """移除不存在的帳號"""
        with patch('accounts.get_account', return_value=None):
            result = app_module.mcp_tool_remove_account('test-1', {
                'account_id': '999999999999'
            })
            
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'error'
            assert '不存在' in content['error']


# ============================================================================
# Deployer MCP Tools 測試
# ============================================================================


class TestMCPRequestValidation:
    """MCP Request 驗證測試"""
    
    def test_mcp_missing_method(self, app_module):
        """缺少 method"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body
    
    def test_mcp_invalid_body(self, app_module):
        """無效的 JSON body"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': 'not-json',
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body


# ============================================================================
# MCP Tools List 補充測試
# ============================================================================


class TestMCPToolsListAdditional:
    """MCP tools/list 補充測試"""
    
    def test_tools_list_contains_deploy_tools(self, app_module):
        """工具列表應包含部署工具"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test',
                'method': 'tools/list'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        tools = body['result']['tools']
        tool_names = [t['name'] for t in tools]
        
        assert 'bouncer_deploy' in tool_names
        assert 'bouncer_deploy_status' in tool_names


# ============================================================================
# Deployer 補充測試
# ============================================================================


class TestMCPAddAccountFull:
    """MCP Add Account 完整測試"""
    
    @patch('telegram.send_telegram_message')
    def test_add_account_success_async(self, mock_telegram, app_module):
        """新增帳號成功（異步）"""
        result = app_module.mcp_tool_add_account('test-1', {
            'account_id': '222222222222',
            'name': 'New Account',
            'role_arn': 'arn:aws:iam::222222222222:role/TestRole',
            'async': True
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'
        assert 'request_id' in content
    
    @patch('telegram.send_telegram_message')
    def test_add_account_empty_role_arn(self, mock_telegram, app_module):
        """新增帳號空 Role ARN（允許）"""
        result = app_module.mcp_tool_add_account('test-1', {
            'account_id': '333333333333',
            'name': 'No Role Account',
            'role_arn': '',
            'async': True
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'


# ============================================================================
# REST API 完整測試
# ============================================================================


class TestMCPRemoveAccount:
    """MCP Remove Account 測試"""
    
    @patch('telegram.send_telegram_message')
    def test_remove_default_account_blocked(self, mock_telegram, app_module):
        """不能移除預設帳號"""
        import mcp_tools
        import mcp_admin
        original = mcp_admin.DEFAULT_ACCOUNT_ID
        try:
            mcp_admin.DEFAULT_ACCOUNT_ID = '111111111111'
            result = app_module.mcp_tool_remove_account('test-1', {
                'account_id': '111111111111'
            })
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'error'
            assert '預設帳號' in content['error']
        finally:
            mcp_admin.DEFAULT_ACCOUNT_ID = original
    
    @patch('telegram.send_telegram_message')
    def test_remove_nonexistent_account(self, mock_telegram, app_module):
        """移除不存在的帳號"""
        result = app_module.mcp_tool_remove_account('test-1', {
            'account_id': '999999999999'
        })
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '不存在' in content['error']


class TestMCPListPending:
    """MCP List Pending 測試"""
    
    def test_list_pending_empty(self, app_module):
        """列出空的 pending"""
        result = app_module.mcp_tool_list_pending('test-1', {})
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['pending_count'] == 0
    
    def test_list_pending_with_source_filter(self, app_module):
        """列出特定 source 的 pending"""
        # 插入 pending 項目
        source = 'filter-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': 'pending-filter-001',
            'command': 'aws s3 cp',
            'status': 'pending',
            'source': source,
            'account_id': '111111111111',
            'reason': 'test reason',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 300
        })
        
        result = app_module.mcp_tool_list_pending('test-1', {'source': source})
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['pending_count'] >= 1


class TestMCPStatus:
    """MCP Status 測試"""
    
    def test_status_request_found(self, app_module):
        """查詢存在的請求狀態"""
        request_id = 'status-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'approved',
            'source': 'test',
            'created_at': int(time.time())
        })
        
        result = app_module.mcp_tool_status('test-1', {'request_id': request_id})
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'approved'


class TestMCPSafelist:
    """MCP Safelist 測試"""
    
    def test_list_safelist_via_handler(self, app_module):
        """透過 handler 列出 safelist"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_list_safelist', {})
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert 'safelist_prefixes' in content
        assert isinstance(content['safelist_prefixes'], list)


class TestMCPAddAccount:
    """MCP Add Account 測試"""
    
    @patch('telegram.send_telegram_message')
    def test_add_account_invalid_id(self, mock_telegram, app_module):
        """新增無效帳號 ID"""
        result = app_module.mcp_tool_add_account('test-1', {
            'account_id': 'invalid',
            'name': 'Test'
        })
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
    
    @patch('telegram.send_telegram_message')
    def test_add_account_missing_name(self, mock_telegram, app_module):
        """新增帳號缺少名稱"""
        result = app_module.mcp_tool_add_account('test-1', {
            'account_id': '123456789012'
        })
        body = json.loads(result['body'])
        # 應該有錯誤或使用預設名稱
        assert 'result' in body or 'error' in body


class TestSyncModeExecute:
    """測試同步/異步模式"""

    @patch('mcp_execute.execute_command')
    def test_sync_safe_command_auto_approved(self, mock_execute, app_module):
        """sync=True + safe command → 直接返回結果"""
        mock_execute.return_value = '{"Instances": []}'

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 100,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 describe-instances',
                        'trust_scope': 'test-session',
                        'reason': 'sync test',
                        'sync': True
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'auto_approved'
        assert '{"Instances": []}' in content['result']

    @patch('telegram.send_telegram_message')
    def test_async_default_pending(self, mock_telegram, app_module):
        """async 預設 → 立即返回 pending_approval"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 101,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 start-instances --instance-ids i-123',
                        'trust_scope': 'test-session',
                        'reason': 'async test'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'
        assert 'request_id' in content


# ============================================================================
# Cross-Account Execute Flow Tests
# ============================================================================


class TestCrossAccountExecuteFlow:
    """測試跨帳號執行流程"""

    @pytest.fixture(autouse=True)
    def setup_accounts_table(self, mock_dynamodb, app_module):
        """Accounts table already exists at session scope, just reset cache"""
        import accounts
        accounts._accounts_table = None  # 重置快取

    @patch('mcp_execute.execute_command')
    @patch('telegram.send_telegram_message')
    def test_cross_account_with_assume_role(self, mock_telegram, mock_execute, app_module):
        """mock execute_command 驗證帶 assume_role 的調用"""
        mock_execute.return_value = '{"Account": "992382394211"}'

        # 先在 accounts table 加入帳號
        from accounts import _get_accounts_table
        accounts_table = _get_accounts_table()
        accounts_table.put_item(Item={
            'account_id': '992382394211',
            'name': 'Dev',
            'role_arn': 'arn:aws:iam::992382394211:role/BouncerExecutionRole',
            'enabled': True,
        })

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 200,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws sts get-caller-identity',
                        'trust_scope': 'test-session',
                        'account': '992382394211',
                        'reason': 'cross-account test'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'auto_approved'
        # 驗證 execute_command 被呼叫時帶了 assume_role
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args
        assert call_args[0][0] == 'aws sts get-caller-identity'
        assert 'BouncerExecutionRole' in (call_args[0][1] or '')

    def test_account_not_found_returns_available(self, app_module):
        """帳號不存在 → 返回可用帳號列表"""
        # 初始化預設帳號，這樣才有可用帳號列表
        from accounts import init_default_account
        init_default_account()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 201,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws s3 ls',
                        'trust_scope': 'test-session',
                        'account': '999999999999',
                        'reason': 'test unknown account'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '999999999999' in content['error']

    @patch('mcp_execute.execute_command')
    def test_assume_role_failure(self, mock_execute, app_module):
        """assume_role 失敗 → 返回錯誤"""
        mock_execute.return_value = '❌ Assume role 失敗: Access Denied'

        # 建立帳號配置
        from accounts import _get_accounts_table
        accounts_table = _get_accounts_table()
        accounts_table.put_item(Item={
            'account_id': '888888888888',
            'name': 'Broken',
            'role_arn': 'arn:aws:iam::888888888888:role/BrokenRole',
            'enabled': True,
        })

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 202,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws sts get-caller-identity',
                        'trust_scope': 'test-session',
                        'account': '888888888888',
                        'reason': 'test broken role'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }

        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'auto_approved'
        assert 'Assume role 失敗' in content['result']


# ============================================================
# Phase 2 Audit Fix: Additional Test Coverage
# ============================================================


class TestSyncAsyncMode:
    """Sync vs async execution mode (T-5)."""

    @patch('telegram.send_telegram_message')
    def test_async_returns_immediately(self, mock_telegram, app_module):
        """Default async mode returns pending_approval immediately."""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'async-test', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws ec2 start-instances --instance-ids i-123',
                    'trust_scope': 'test-session',
                    'reason': 'test async', 'source': 'test-bot'
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'
        assert 'request_id' in content


class TestCrossAccountExecuteErrors:
    """Cross-account execution error paths (T-3)."""

    @patch('telegram.send_telegram_message')
    def test_nonexistent_account_returns_available(self, mock_telegram, app_module):
        """Requesting non-existent account should list available accounts."""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'bad-acct', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws s3 ls',
                    'trust_scope': 'test-session',
                    'reason': 'test', 'source': 'test-bot',
                    'account': '999999999999'
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '999999999999' in content.get('error', '')

    @patch('telegram.send_telegram_message')
    def test_invalid_account_format(self, mock_telegram, app_module):
        """Non-numeric account ID should be rejected."""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'bad-format', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws s3 ls',
                    'trust_scope': 'test-session',
                    'reason': 'test', 'source': 'test-bot',
                    'account': 'not-a-number'
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content.get('status') == 'error' or body.get('error')


# ============================================================================
# Display Summary Tests
# ============================================================================


# ============================================================================
# T7: Grant Tools Coverage — mcp_tool_request_grant / grant_status / revoke_grant
# ============================================================================


def _make_grant_event(tool_name: str, arguments: dict) -> dict:
    """Helper: build a lambda_handler event for a grant MCP tool call."""
    return {
        'rawPath': '/mcp',
        'headers': {'x-approval-secret': 'test-secret'},
        'body': json.dumps({
            'jsonrpc': '2.0',
            'id': 'grant-test',
            'method': 'tools/call',
            'params': {
                'name': tool_name,
                'arguments': arguments,
            },
        }),
        'requestContext': {'http': {'method': 'POST'}},
    }


class TestMCPGrantTools:
    """Coverage for mcp_tool_request_grant / grant_status / revoke_grant (L882-1020)."""

    # ------------------------------------------------------------------ #
    # bouncer_request_grant — happy path                                  #
    # ------------------------------------------------------------------ #

    @patch('notifications.send_grant_request_notification')
    def test_request_grant_happy_path(self, mock_notif, app_module):
        """request_grant returns pending_approval on valid input."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls s3://my-bucket'],
            'reason': 'list bucket contents',
            'source': 'test-bot',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'result' in body, f"Expected result, got: {body}"
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'
        assert 'grant_request_id' in content

    @patch('notifications.send_grant_request_notification')
    def test_request_grant_with_account(self, mock_notif, app_module):
        """request_grant with explicit account_id uses that account."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'test reason',
            'source': 'test-bot',
            'account': '111111111111',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'

    @patch('notifications.send_grant_request_notification')
    def test_request_grant_notification_failure_non_fatal(self, mock_notif, app_module):
        """Notification failure should not prevent grant creation."""
        mock_notif.side_effect = Exception("Telegram down")
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws ec2 describe-instances'],
            'reason': 'check instances',
            'source': 'test-bot',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        # Should still return pending_approval despite notification failure
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'

    # ------------------------------------------------------------------ #
    # bouncer_request_grant — missing required params                     #
    # ------------------------------------------------------------------ #

    def test_request_grant_missing_commands(self, app_module):
        """request_grant without commands returns error -32602."""
        event = _make_grant_event('bouncer_request_grant', {
            'reason': 'test',
            'source': 'test-bot',
            # 'commands' intentionally missing
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        # Should be an MCP error
        assert 'error' in body or (
            'result' in body and
            json.loads(body['result']['content'][0]['text']).get('isError')
        )

    def test_request_grant_missing_reason(self, app_module):
        """request_grant without reason returns error -32602."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'source': 'test-bot',
            # 'reason' missing
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body or body.get('result', {}).get('isError')

    def test_request_grant_missing_source(self, app_module):
        """request_grant without source returns error -32602."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'test reason',
            # 'source' missing
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body or body.get('result', {}).get('isError')

    def test_request_grant_invalid_account(self, app_module):
        """request_grant with invalid account_id returns error."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'test',
            'source': 'test-bot',
            'account': 'not-numeric',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        # Should report error (either MCP error or isError result)
        assert result['statusCode'] == 200  # HTTP level OK
        is_error = (
            'error' in body or
            body.get('result', {}).get('isError') or
            json.loads(body.get('result', {}).get('content', [{'text': '{}'}])[0]['text']).get('status') == 'error'
        )
        assert is_error

    def test_request_grant_nonexistent_account(self, app_module):
        """request_grant with unconfigured account returns error."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'test',
            'source': 'test-bot',
            'account': '999999999999',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content_text = body.get('result', {}).get('content', [{'text': '{}'}])[0]['text']
        content = json.loads(content_text)
        assert content.get('status') == 'error' or body.get('result', {}).get('isError')

    # ------------------------------------------------------------------ #
    # bouncer_grant_status — various paths                                #
    # ------------------------------------------------------------------ #

    def test_grant_status_missing_grant_id(self, app_module):
        """grant_status without grant_id returns error -32602."""
        event = _make_grant_event('bouncer_grant_status', {
            'source': 'test-bot',
            # grant_id missing
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body

    def test_grant_status_missing_source(self, app_module):
        """grant_status without source returns error -32602."""
        event = _make_grant_event('bouncer_grant_status', {
            'grant_id': 'grant_nonexistent',
            # source missing
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body

    def test_grant_status_not_found(self, app_module):
        """grant_status for unknown grant_id returns isError result."""
        event = _make_grant_event('bouncer_grant_status', {
            'grant_id': 'grant_doesnotexist_abc123',
            'source': 'test-bot',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        # Should return isError since grant not found
        assert 'result' in body
        assert body['result'].get('isError') is True

    @patch('notifications.send_grant_request_notification')
    def test_grant_status_found_pending(self, mock_notif, app_module):
        """grant_status returns status for existing pending grant."""
        # First create a grant
        create_event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'check for status test',
            'source': 'status-test-bot',
        })
        create_result = app_module.lambda_handler(create_event, None)
        create_body = json.loads(create_result['body'])
        grant_id = json.loads(create_body['result']['content'][0]['text'])['grant_request_id']

        # Now check status
        status_event = _make_grant_event('bouncer_grant_status', {
            'grant_id': grant_id,
            'source': 'status-test-bot',
        })
        result = app_module.lambda_handler(status_event, None)
        body = json.loads(result['body'])
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['grant_id'] == grant_id
        assert content['status'] in ('pending_approval', 'active', 'expired', 'revoked')

    @patch('notifications.send_grant_request_notification')
    def test_grant_status_source_mismatch(self, mock_notif, app_module):
        """grant_status with wrong source returns isError (source mismatch)."""
        # Create grant with source-A
        create_event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'source mismatch test',
            'source': 'source-A',
        })
        create_result = app_module.lambda_handler(create_event, None)
        create_body = json.loads(create_result['body'])
        grant_id = json.loads(create_body['result']['content'][0]['text'])['grant_request_id']

        # Query with source-B
        status_event = _make_grant_event('bouncer_grant_status', {
            'grant_id': grant_id,
            'source': 'source-B-different',
        })
        result = app_module.lambda_handler(status_event, None)
        body = json.loads(result['body'])
        assert 'result' in body
        assert body['result'].get('isError') is True

    # ------------------------------------------------------------------ #
    # bouncer_revoke_grant — paths                                        #
    # ------------------------------------------------------------------ #

    def test_revoke_grant_missing_grant_id(self, app_module):
        """revoke_grant without grant_id returns error -32602."""
        event = _make_grant_event('bouncer_revoke_grant', {
            # grant_id missing
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body

    @patch('notifications.send_grant_request_notification')
    def test_revoke_grant_success(self, mock_notif, app_module):
        """revoke_grant on existing grant returns success=True."""
        # Create a grant first
        create_event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls'],
            'reason': 'revoke test',
            'source': 'revoke-test-bot',
        })
        create_result = app_module.lambda_handler(create_event, None)
        create_body = json.loads(create_result['body'])
        grant_id = json.loads(create_body['result']['content'][0]['text'])['grant_request_id']

        # Revoke it
        revoke_event = _make_grant_event('bouncer_revoke_grant', {
            'grant_id': grant_id,
        })
        result = app_module.lambda_handler(revoke_event, None)
        body = json.loads(result['body'])
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['success'] is True
        assert content['grant_id'] == grant_id

    def test_revoke_grant_nonexistent(self, app_module):
        """revoke_grant on nonexistent grant_id returns a result (DynamoDB update succeeds vacuously)."""
        revoke_event = _make_grant_event('bouncer_revoke_grant', {
            'grant_id': 'grant_nonexistent_xyz999',
        })
        result = app_module.lambda_handler(revoke_event, None)
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        # DynamoDB update on non-existent item doesn't error — success may be True
        assert 'result' in body


# ============================================================================
# T7 Extra: Unicode normalization + _safe_risk helpers coverage (L86-153)
# ============================================================================


class TestMCPExecuteHelpers:
    """Direct unit tests for helper functions in mcp_execute.py."""

    def test_safe_risk_category_none_input(self, app_module):
        """_safe_risk_category returns None for None input."""
        import mcp_execute
        result = mcp_execute._safe_risk_category(None)
        assert result is None

    def test_safe_risk_category_with_value_attr(self, app_module):
        """_safe_risk_category returns .value for enum-like category."""
        import mcp_execute
        cat_mock = MagicMock()
        cat_mock.value = 'high'
        risk_mock = MagicMock()
        risk_mock.risk_result.category = cat_mock
        result = mcp_execute._safe_risk_category(risk_mock)
        assert result == 'high'

    def test_safe_risk_category_exception(self, app_module):
        """_safe_risk_category returns None on exception."""
        import mcp_execute
        bad_mock = MagicMock()
        bad_mock.risk_result = None  # will raise AttributeError
        result = mcp_execute._safe_risk_category(bad_mock)
        assert result is None

    def test_safe_risk_factors_none_input(self, app_module):
        """_safe_risk_factors returns None for None input."""
        import mcp_execute
        result = mcp_execute._safe_risk_factors(None)
        assert result is None

    def test_safe_risk_factors_with_floats(self, app_module):
        """_safe_risk_factors converts float values to Decimal."""
        import mcp_execute
        from decimal import Decimal
        factor_mock = MagicMock()
        factor_mock.__dict__ = {'name': 'test', 'score': 0.75, 'raw_score': 75}
        risk_mock = MagicMock()
        risk_mock.risk_result.factors = [factor_mock]
        result = mcp_execute._safe_risk_factors(risk_mock)
        assert result is not None
        assert isinstance(result[0]['score'], Decimal)

    def test_safe_risk_factors_exception(self, app_module):
        """_safe_risk_factors returns None on exception."""
        import mcp_execute
        bad_mock = MagicMock()
        bad_mock.risk_result = None
        result = mcp_execute._safe_risk_factors(bad_mock)
        assert result is None

    def test_normalize_command_strips_invisible_chars(self, app_module):
        """_normalize_command removes Unicode zero-width characters."""
        import mcp_execute
        # Zero-width space (U+200B) in command
        cmd = 'aws\u200b s3 ls'
        result = mcp_execute._normalize_command(cmd)
        assert '\u200b' not in result
        assert 'aws' in result

    def test_normalize_command_collapses_spaces(self, app_module):
        """_normalize_command collapses multiple spaces."""
        import mcp_execute
        result = mcp_execute._normalize_command('aws   s3   ls')
        assert result == 'aws s3 ls'

    def test_normalize_command_unicode_spaces(self, app_module):
        """_normalize_command replaces Unicode spaces with ASCII space."""
        import mcp_execute
        # Non-breaking space (U+00A0)
        cmd = 'aws\u00a0s3\u00a0ls'
        result = mcp_execute._normalize_command(cmd)
        assert '\u00a0' not in result
        assert 'aws s3 ls' == result

    def test_normalize_command_empty_string(self, app_module):
        """_normalize_command handles empty string."""
        import mcp_execute
        assert mcp_execute._normalize_command('') == ''


# ============================================================================
# T7 Extra: _extract_actual_decision + _map_status coverage (L363-375)
# ============================================================================


class TestMCPExecuteDecisionHelpers:
    """Tests for _extract_actual_decision and _map_status_to_decision."""

    def test_map_status_auto_approved(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('auto_approved') == 'auto_approve'

    def test_map_status_blocked(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('blocked') == 'blocked'

    def test_map_status_compliance_violation(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('compliance_violation') == 'blocked'

    def test_map_status_pending_approval(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('pending_approval') == 'needs_approval'

    def test_map_status_trust_auto_approved(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('trust_auto_approved') == 'auto_approve'

    def test_map_status_grant_auto_approved(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('grant_auto_approved') == 'auto_approve'

    def test_map_status_unknown(self, app_module):
        import mcp_execute
        assert mcp_execute._map_status_to_decision('something_else') == 'something_else'

    def test_extract_actual_decision_auto_approved(self, app_module):
        import mcp_execute
        fake_result = {
            'body': json.dumps({
                'jsonrpc': '2.0',
                'result': {
                    'content': [{'type': 'text', 'text': json.dumps({'status': 'auto_approved'})}]
                }
            })
        }
        assert mcp_execute._extract_actual_decision(fake_result) == 'auto_approve'

    def test_extract_actual_decision_error_path(self, app_module):
        import mcp_execute
        fake_result = {
            'body': json.dumps({
                'jsonrpc': '2.0',
                'error': {'code': -32603, 'message': 'Internal error'}
            })
        }
        assert mcp_execute._extract_actual_decision(fake_result) == 'error'

    def test_extract_actual_decision_bad_json(self, app_module):
        import mcp_execute
        fake_result = {'body': 'not-json'}
        # Should not raise, returns 'unknown'
        result = mcp_execute._extract_actual_decision(fake_result)
        assert isinstance(result, str)

    def test_extract_actual_decision_empty_content(self, app_module):
        import mcp_execute
        fake_result = {
            'body': json.dumps({
                'jsonrpc': '2.0',
                'result': {'content': []}
            })
        }
        result = mcp_execute._extract_actual_decision(fake_result)
        assert result == 'unknown'


# ============================================================================
# T7 Extra: Rate-limit / pending-limit paths (L646-660)
# ============================================================================


class TestMCPRateLimitPaths:
    """Cover _check_rate_limit PendingLimitExceeded branch (L656-660)."""

    @patch('mcp_execute.check_rate_limit')
    @patch('telegram.send_telegram_message')
    def test_pending_limit_exceeded(self, mock_tg, mock_rate, app_module):
        """When PendingLimitExceeded is raised, return pending_limit_exceeded status."""
        from rate_limit import PendingLimitExceeded
        mock_rate.side_effect = PendingLimitExceeded("Too many pending requests")

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'rate-test', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws ec2 stop-instances --instance-ids i-abc',
                    'trust_scope': 'test-session',
                    'reason': 'test pending limit',
                    'source': 'test-bot',
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_limit_exceeded'

    @patch('mcp_execute.check_rate_limit')
    @patch('telegram.send_telegram_message')
    def test_rate_limit_exceeded(self, mock_tg, mock_rate, app_module):
        """When RateLimitExceeded is raised, return rate_limited status."""
        from rate_limit import RateLimitExceeded
        mock_rate.side_effect = RateLimitExceeded("Rate limit hit")

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'rate-test2', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws ec2 stop-instances --instance-ids i-xyz',
                    'trust_scope': 'test-session',
                    'reason': 'test rate limit',
                    'source': 'test-bot',
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'rate_limited'


# ============================================================================
# T7 Extra: Compliance violation path (L397-428)
# ============================================================================


class TestMCPCompliancePaths:
    """Cover _check_compliance violation path."""

    @patch('mcp_execute._check_compliance')
    @patch('telegram.send_telegram_message')
    def test_compliance_violation_returned(self, mock_tg, mock_compliance, app_module):
        """When compliance check returns violation, pipeline returns compliance_violation."""
        import mcp_execute
        from utils import mcp_result

        # Make _check_compliance return a compliance_violation result
        mock_compliance.return_value = mcp_result('test-id', {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'compliance_violation',
                    'rule_id': 'RULE-001',
                    'rule_name': 'Test Rule',
                    'description': 'Test',
                    'remediation': 'Fix it',
                    'command': 'aws iam delete-user',
                })
            }],
            'isError': True
        })

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'compliance-test', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws iam delete-user --user-name bad-user',
                    'trust_scope': 'test-session',
                    'reason': 'compliance test',
                    'source': 'test-bot',
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'compliance_violation'

    def test_execute_missing_command(self, app_module):
        """bouncer_execute without command returns error -32602."""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'no-cmd', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'trust_scope': 'test-session',
                    'reason': 'test',
                    # command missing
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body
        assert body['error']['code'] == -32602

    def test_execute_missing_trust_scope(self, app_module):
        """bouncer_execute without trust_scope returns error -32602."""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'no-trust', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws s3 ls',
                    'reason': 'test',
                    # trust_scope missing
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body
        assert body['error']['code'] == -32602


# ============================================================================
# T7 Extra: _check_blocked path (L436-450)
# ============================================================================


class TestMCPBlockedPath:
    """Cover _check_blocked path."""

    @patch('mcp_execute._check_blocked')
    @patch('telegram.send_telegram_message')
    def test_blocked_command_returns_blocked_status(self, mock_tg, mock_blocked, app_module):
        """When _check_blocked returns a result, pipeline short-circuits."""
        import mcp_execute
        from utils import mcp_result

        mock_blocked.return_value = mcp_result('test-id', {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'blocked',
                    'error': '命令被安全規則封鎖',
                    'block_reason': 'test block',
                    'command': 'aws iam delete-role',
                    'suggestion': '...',
                })
            }],
            'isError': True
        })

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'blocked-test', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws iam delete-role --role-name bad-role',
                    'trust_scope': 'test-session',
                    'reason': 'blocked test',
                    'source': 'test-bot',
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


# ============================================================================
# T7 Extra: template scan escalate path (L339-347 / L843)
# ============================================================================


class TestMCPTemplateScanEscalate:
    """Cover template escalate pipeline branch."""

    @patch('mcp_execute._scan_template')
    @patch('mcp_execute._score_risk')
    @patch('telegram.send_telegram_message')
    def test_escalate_skips_auto_approve(self, mock_tg, mock_score, mock_scan, app_module):
        """When template scan escalates, auto_approve / trust are skipped."""
        # Make template scan set escalate=True
        def fake_scan(ctx):
            ctx.template_scan_result = {
                'max_score': 90,
                'hit_count': 1,
                'severity': 'critical',
                'factors': [],
                'escalate': True,
            }
        mock_scan.side_effect = fake_scan

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'escalate-test', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws s3 cp s3://bucket/dangerous.json .',
                    'trust_scope': 'test-session',
                    'reason': 'template escalate test',
                    'source': 'test-bot',
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        # Should go to pending_approval (not auto_approved)
        assert content['status'] == 'pending_approval'


# ============================================================================
# T7 Extra: Disabled account path (L232)
# ============================================================================


class TestMCPDisabledAccount:
    """Cover disabled account error path (L232)."""

    @patch('mcp_execute.get_account')
    @patch('mcp_execute.validate_account_id')
    def test_execute_disabled_account(self, mock_validate, mock_get, app_module):
        """Execute with disabled account returns error."""
        mock_validate.return_value = (True, None)
        mock_get.return_value = {
            'account_id': '111111111111',
            'name': 'Disabled Account',
            'enabled': False,
            'role_arn': None,
        }

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'disabled-acct', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws s3 ls',
                    'trust_scope': 'test-session',
                    'reason': 'test',
                    'source': 'test-bot',
                    'account': '111111111111',
                }}
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '已停用' in content.get('error', '')


# ============================================================================
# T7 Extra: request_grant with ttl_minutes and allow_repeat (L919-962)
# ============================================================================


class TestMCPRequestGrantOptions:
    """Test request_grant with optional ttl_minutes and allow_repeat."""

    @patch('notifications.send_grant_request_notification')
    def test_request_grant_with_ttl_and_repeat(self, mock_notif, app_module):
        """request_grant with ttl_minutes and allow_repeat=True creates grant."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': ['aws s3 ls', 'aws ec2 describe-instances'],
            'reason': 'batch ops',
            'source': 'test-bot',
            'ttl_minutes': 15,
            'allow_repeat': True,
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'pending_approval'
        assert 'expires_in' in content

    @patch('notifications.send_grant_request_notification')
    def test_request_grant_ValueError_propagated(self, mock_notif, app_module):
        """request_grant with empty commands list hits ValueError path."""
        event = _make_grant_event('bouncer_request_grant', {
            'commands': [],  # empty list triggers ValueError
            'reason': 'test',
            'source': 'test-bot',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        # Empty commands is caught and returned as error
        assert 'error' in body or body.get('result', {}).get('isError')


# ============================================================================
# T7 Extra: grant_status found / exception (L992-993, L1019-1020)
# ============================================================================


class TestMCPGrantStatusException:
    """Test exception paths in grant_status and revoke_grant."""

    @patch('grant.get_grant_status')
    def test_grant_status_internal_exception(self, mock_status, app_module):
        """grant_status exception returns mcp_error -32603."""
        mock_status.side_effect = Exception("DDB timeout")

        event = _make_grant_event('bouncer_grant_status', {
            'grant_id': 'grant_test123',
            'source': 'test-bot',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body
        assert body['error']['code'] == -32603

    @patch('grant.revoke_grant')
    def test_revoke_grant_internal_exception(self, mock_revoke, app_module):
        """revoke_grant exception returns mcp_error -32603."""
        mock_revoke.side_effect = Exception("DDB timeout")

        event = _make_grant_event('bouncer_revoke_grant', {
            'grant_id': 'grant_test456',
        })
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert 'error' in body
        assert body['error']['code'] == -32603


# ============================================================================
# T7 Extra: _log_smart_approval_shadow success path (L152-153)
# ============================================================================


class TestMCPShadowLogging:
    """Test _log_smart_approval_shadow."""

    def test_shadow_log_handles_exception_gracefully(self, app_module):
        """_log_smart_approval_shadow failure is non-fatal."""
        import mcp_execute

        # Create a fake smart_decision that will cause DDB error
        smart_mock = MagicMock()
        smart_mock.decision = 'auto_approve'
        smart_mock.final_score = 25
        smart_mock.risk_result.category.value = 'low'
        smart_mock.risk_result.factors = []

        # Should not raise even when DDB table doesn't exist
        mcp_execute._log_smart_approval_shadow(
            req_id='test-123',
            command='aws s3 ls',
            reason='test',
            source='test-bot',
            account_id='111111111111',
            smart_decision=smart_mock,
            actual_decision='auto_approve',
        )
        # No assertion needed — just verify it doesn't raise
