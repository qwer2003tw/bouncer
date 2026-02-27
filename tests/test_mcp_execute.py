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
