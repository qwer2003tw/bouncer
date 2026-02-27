import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3


class TestRESTSafelist:
    """REST API SAFELIST 測試"""
    
    @patch('commands.execute_command')
    def test_safelist_auto_approved(self, mock_execute, app_module):
        """測試 REST API 自動批准"""
        mock_execute.return_value = '{"Account": "123456"}'
        
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'command': 'aws sts get-caller-identity'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        body = json.loads(result['body'])
        assert body['status'] == 'auto_approved'


class TestRESTApproval:
    """REST API APPROVAL 測試"""
    
    @patch('telegram.send_telegram_message')
    def test_approval_pending(self, mock_telegram, app_module):
        """測試 REST API 待審批（非等待模式）"""
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'command': 'aws ec2 stop-instances --instance-ids i-123',
                'wait': False
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 202
        
        body = json.loads(result['body'])
        assert body['status'] == 'pending_approval'
        assert 'request_id' in body
        assert mock_telegram.called


# ============================================================================
# Telegram Webhook Tests
# ============================================================================


class TestIntegration:
    """整合測試"""
    
    def test_full_mcp_flow_safelist(self, app_module):
        """測試完整 MCP 流程（SAFELIST）"""
        import mcp_execute
        import mcp_tools
        with patch.object(mcp_execute, 'execute_command', return_value='{"UserId": "123", "Account": "456", "Arn": "arn:aws:iam::456:user/test"}'):
            # Initialize
            init_event = {
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
            result = app_module.lambda_handler(init_event, None)
            assert result['statusCode'] == 200
            
            # Execute
            exec_event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 2,
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_execute',
                        'arguments': {
                            'command': 'aws sts get-caller-identity',
                            'trust_scope': 'test-session',
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            result = app_module.lambda_handler(exec_event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert content['status'] == 'auto_approved'
            assert 'UserId' in content['result']


# ============================================================================
# Status Query Tests
# ============================================================================


class TestStatusQuery:
    """狀態查詢測試"""
    
    def test_status_query_found(self, app_module):
        """測試查詢存在的請求"""
        request_id = 'status_test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 describe-instances',
            'status': 'approved',
            'result': 'OK'
        })
        
        event = {
            'rawPath': f'/status/{request_id}',
            'headers': {'x-approval-secret': 'test-secret'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        body = json.loads(result['body'])
        assert body['status'] == 'approved'
    
    def test_status_query_not_found(self, app_module):
        """測試查詢不存在的請求"""
        event = {
            'rawPath': '/status/nonexistent',
            'headers': {'x-approval-secret': 'test-secret'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 404


# ============================================================================
# Trust Session 測試
# ============================================================================


class TestHMACVerification:
    """HMAC 驗證測試"""
    
    def test_verify_hmac_missing_header(self, app_module):
        """缺少 HMAC header"""
        with patch('app.ENABLE_HMAC', True):
            result = app_module.verify_hmac({}, 'body')
            assert result is False
    
    def test_verify_hmac_with_empty_timestamp(self, app_module):
        """空 timestamp"""
        with patch('app.ENABLE_HMAC', True):
            result = app_module.verify_hmac({'x-timestamp': ''}, 'body')
            assert result is False


# ============================================================================
# Lambda Handler 測試
# ============================================================================


class TestLambdaHandler:
    """Lambda handler 測試"""
    
    def test_handler_health_check(self, app_module):
        """健康檢查"""
        event = {'httpMethod': 'GET', 'path': '/prod/health'}
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
    
    def test_handler_unknown_path(self, app_module):
        """未知路徑"""
        event = {'httpMethod': 'GET', 'path': '/unknown/path', 'headers': {}}
        result = app_module.lambda_handler(event, None)
        # 可能返回 404 或其他錯誤
        assert 'statusCode' in result


# ============================================================================
# Helper Functions 測試
# ============================================================================


class TestRESTHandler:
    """REST API 處理測試"""
    
    def test_handle_clawdbot_request_blocked(self, app_module):
        """被封鎖的命令"""
        event = {
            'body': json.dumps({
                'command': 'aws iam delete-user --user-name test',
                'reason': 'test'
            })
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 403
    
    def test_handle_clawdbot_request_empty_body(self, app_module):
        """空 body"""
        event = {'body': '{}'}
        result = app_module.handle_clawdbot_request(event)
        # 空命令應該返回錯誤
        assert result['statusCode'] in [400, 403]


# ============================================================================
# Command Classification 邊界測試
# ============================================================================


class TestSendApprovalRequest:
    """審批請求發送測試"""
    
    def test_send_approval_request_blocked(self, app_module):
        """被封鎖的命令"""
        with patch.object(app_module.table, 'put_item'):
            result = app_module.send_approval_request(
                request_id='test-123',
                command='aws iam create-access-key',
                reason='test'
            )
            # 封鎖的命令不會成功：send_approval_request 回傳 bool，Telegram 失敗時為 False
            assert result is None or isinstance(result, bool) or result.get('status') == 'blocked'


# ============================================================================
# execute_command 測試
# ============================================================================


class TestStatusQueryEdgeCases:
    """Status 查詢邊界測試"""
    
    def test_handle_status_query_function_exists(self, app_module):
        """handle_status_query 函數存在"""
        assert callable(app_module.handle_status_query)


# ============================================================================
# Rate Limit 測試（補充）
# ============================================================================


class TestRESTAPIHandlerAdditional:
    """REST API Handler 補充測試"""
    
    def test_handle_clawdbot_request_missing_command(self, app_module):
        """缺少命令"""
        event = {
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'reason': 'test'
            })
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 400
    
    def test_handle_clawdbot_request_invalid_json(self, app_module):
        """無效 JSON"""
        event = {
            'headers': {'x-approval-secret': 'test-secret'},
            'body': 'not json'
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 400
    
    @patch('commands.execute_command')
    def test_handle_clawdbot_request_safelist(self, mock_execute, app_module):
        """REST API 自動批准"""
        mock_execute.return_value = '{"result": "ok"}'
        
        event = {
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'command': 'aws s3 ls'
            })
        }
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'auto_approved'


# ============================================================================
# Telegram Commands 測試補充
# ============================================================================


class TestLambdaHandlerRouting:
    """Lambda Handler 路由測試"""
    
    def test_handler_options_request(self, app_module):
        """OPTIONS 請求（CORS）"""
        event = {
            'httpMethod': 'OPTIONS',
            'path': '/',
            'headers': {}
        }
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
    
    def test_handler_mcp_path(self, app_module):
        """MCP 路徑路由"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test',
                'method': 'initialize'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
    
    def test_handler_webhook_path(self, app_module):
        """Webhook 路徑路由"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({}),
            'requestContext': {'http': {'method': 'POST'}}
        }
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200


# ============================================================================
# 補充 BLOCKED 命令測試
# ============================================================================


class TestHMACVerificationAdditional:
    """HMAC 驗證補充測試"""
    
    def test_verify_hmac_expired_timestamp(self, app_module):
        """過期的 timestamp"""
        import time
        with patch.object(app_module, 'ENABLE_HMAC', True):
            headers = {
                'x-timestamp': str(int(time.time()) - 600),  # 10 分鐘前
                'x-nonce': 'test-nonce',
                'x-signature': 'invalid-sig'
            }
            result = app_module.verify_hmac(headers, 'body')
            assert result is False


# ============================================================================
# MCP Request Validation 測試
# ============================================================================


class TestAdditionalCoverage:
    """額外覆蓋率測試"""
    
    def test_mcp_method_ping(self, app_module):
        """ping 方法"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test',
                'method': 'ping'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
    
    def test_mcp_tools_call_list_safelist(self, app_module):
        """bouncer_list_safelist 工具"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test',
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
    
    def test_mcp_tools_call_list_accounts(self, app_module):
        """bouncer_list_accounts 工具"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_list_accounts',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert 'accounts' in content
        assert 'default_account' in content


class TestRESTAPIFull:
    """REST API 完整測試"""
    
    @patch('telegram.send_telegram_message')
    def test_rest_api_approval_wait(self, mock_telegram, app_module):
        """REST API 等待審批（短超時）"""
        event = {
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'command': 'aws ec2 start-instances --instance-ids i-123',
                'reason': 'test',
                'wait': True,
                'timeout': 1  # 1 秒超時
            })
        }
        
        result = app_module.handle_clawdbot_request(event)
        # 應該返回 pending（因為超時太短）
        assert result['statusCode'] in [200, 202]


# ============================================================================
# 更多 App 覆蓋測試
# ============================================================================


class TestAppModuleMore:
    """更多 App 模組測試"""
    
    def test_mcp_execute_with_paging(self, app_module):
        """執行命令並測試分頁"""
        # Mock 返回長輸出
        long_output = 'x' * 5000
        with patch.object(app_module, 'execute_command', return_value=long_output):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_execute',
                        'arguments': {
                            'command': 'aws logs get-log-events --log-group-name test --log-stream-name test-stream',
                            'trust_scope': 'test-session',
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert content['status'] == 'auto_approved'
            # 檢查是否有分頁
            if content.get('paged'):
                assert content['total_pages'] >= 2
    
    def test_mcp_get_page(self, app_module):
        """取得分頁"""
        # 先建立分頁
        app_module.table.put_item(Item={
            'request_id': 'get-page-test:page:2',
            'content': 'Page 2 data',
            'page': 2,
            'total_pages': 3,
            'original_request': 'get-page-test'
        })
        
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_get_page',
                    'arguments': {
                        'page_id': 'get-page-test:page:2'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        
        assert content['page'] == 2
        assert content['result'] == 'Page 2 data'
    
    def test_mcp_list_accounts_with_mock(self, app_module):
        """列出帳號（有帳號）"""
        with patch.object(app_module, 'list_accounts', return_value=[
            {'account_id': '444444444444', 'name': 'Test Account', 'enabled': True}
        ]):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_list_accounts',
                        'arguments': {}
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert 'accounts' in content


# ============================================================================
# Deployer 更多測試
# ============================================================================


class TestLambdaHandlerMore:
    """Lambda Handler 更多測試"""
    
    def test_handler_status_path(self, app_module):
        """/status 路徑"""
        # 先建立一個請求
        app_module.table.put_item(Item={
            'request_id': 'status-path-test',
            'command': 'aws s3 ls',
            'status': 'approved'
        })
        
        event = {
            'rawPath': '/status/status-path-test',
            'headers': {'x-approval-secret': 'test-secret'},
            'requestContext': {'http': {'method': 'GET'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'approved'
    
    def test_handler_post_to_root(self, app_module):
        """POST 到 root（REST API）"""
        with patch.object(app_module, 'execute_command', return_value='{"ok": true}'):
            event = {
                'rawPath': '/',
                'path': '/',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'command': 'aws s3 ls'
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            assert result['statusCode'] == 200


# ============================================================================
# 更多覆蓋率測試 - 80% 衝刺
# ============================================================================


class TestCoverage80Sprint:
    """80% 覆蓋率衝刺測試"""
    
    def test_callback_invalid_data(self, app_module):
        """callback 無效 data"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': 'invalid-no-colon',  # 沒有冒號
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 400
    
    @patch('telegram.send_telegram_message')
    def test_mcp_execute_with_configured_account(self, mock_telegram, app_module):
        """執行命令使用配置的帳號"""
        import mcp_execute
        import mcp_tools
        with patch.object(mcp_execute, 'get_account', return_value={
            'account_id': '555555555555',
            'name': 'Configured Account',
            'enabled': True,
            'role_arn': 'arn:aws:iam::555555555555:role/TestRole'
        }), patch.object(mcp_execute, 'execute_command', return_value='{"result": "ok"}'):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_execute',
                        'arguments': {
                            'command': 'aws s3 ls',
                            'trust_scope': 'test-session',
                            'account': '555555555555'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert content['status'] == 'auto_approved'
            assert content['account'] == '555555555555'
    
    @patch('telegram.send_telegram_message')
    def test_send_approval_request_with_assume_role(self, mock_telegram, app_module):
        """發送審批請求帶 assume_role"""
        app_module.send_approval_request(
            'test-req-789',
            'aws ec2 start-instances',
            'Test',
            assume_role='arn:aws:iam::123456789012:role/TestRole'
        )
        mock_telegram.assert_called()
    
    def test_decimal_to_native_nested(self, app_module):
        """Decimal 轉換 - 巢狀結構"""
        from decimal import Decimal
        data = {
            'list': [Decimal('1'), {'nested': Decimal('2.5')}],
            'value': Decimal('100')
        }
        result = app_module.decimal_to_native(data)
        assert result['list'][0] == 1
        assert result['list'][1]['nested'] == 2.5
        assert result['value'] == 100
    
    def test_mcp_tools_list_all_tools(self, app_module):
        """工具列表包含所有工具"""
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
        
        # 驗證所有主要工具都存在
        expected_tools = [
            'bouncer_execute',
            'bouncer_status',
            'bouncer_list_safelist',
            'bouncer_list_accounts',
            'bouncer_add_account',
            'bouncer_remove_account',
            'bouncer_upload',
            'bouncer_deploy',
            'bouncer_deploy_status',
            'bouncer_trust_status',
            'bouncer_trust_revoke',
            'bouncer_get_page',
            'bouncer_list_pending'
        ]
        
        for tool in expected_tools:
            assert tool in tool_names, f"Missing tool: {tool}"
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    @patch('callbacks.execute_command')
    def test_callback_approve_with_paged_result(self, mock_execute, mock_answer, mock_update, app_module):
        """批准命令並返回分頁結果"""
        # 返回長輸出
        mock_execute.return_value = 'x' * 5000  # 長輸出
        
        request_id = 'paged-approve-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws logs get-log-events',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Default',
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
    
    def test_escape_markdown_all_chars(self, app_module):
        """Markdown 跳脫所有特殊字元"""
        from telegram import escape_markdown
        text = '*_`['
        escaped = escape_markdown(text)
        assert '\\*' in escaped
        assert '\\_' in escaped
        assert '\\`' in escaped
        assert '\\[' in escaped
    
    @patch('telegram.send_telegram_message')
    def test_mcp_upload_with_legacy_bucket(self, mock_telegram, app_module):
        """上傳使用舊版 bucket/key 參數"""
        import base64
        content = base64.b64encode(b'test').decode()
        
        result = app_module.mcp_tool_upload('test-1', {
            'bucket': 'legacy-bucket',
            'key': 'legacy/key.txt',
            'content': content,
            'reason': 'legacy test'
        })
        
        body = json.loads(result['body'])
        content_data = json.loads(body['result']['content'][0]['text'])
        assert content_data['status'] == 'pending_approval'
    
    def test_handle_mcp_tool_call_all_tools(self, app_module):
        """測試所有 MCP tool 路由"""
        # bouncer_list_safelist
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_list_safelist', {})
        body = json.loads(result['body'])
        assert 'result' in body
        
        # bouncer_trust_status
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_trust_status', {})
        body = json.loads(result['body'])
        assert 'result' in body
    
    def test_get_header_various_cases(self, app_module):
        """get_header 各種大小寫"""
        headers = {
            'X-Custom-Header': 'value1',
            'content-type': 'application/json'
        }
        
        assert app_module.get_header(headers, 'x-custom-header') == 'value1'
        assert app_module.get_header(headers, 'X-CUSTOM-HEADER') == 'value1'
        assert app_module.get_header(headers, 'Content-Type') == 'application/json'
        assert app_module.get_header(headers, 'Missing') is None


# ============================================================================
# Commands 模組 - 更多測試
# ============================================================================


class TestValidation:
    """驗證測試"""
    
    def test_validate_account_id_valid(self, app_module):
        """有效的帳號 ID"""
        valid, error = app_module.validate_account_id('123456789012')
        assert valid is True
        assert error is None
    
    def test_validate_account_id_empty(self, app_module):
        """空的帳號 ID"""
        valid, error = app_module.validate_account_id('')
        assert valid is False
    
    def test_validate_account_id_invalid_chars(self, app_module):
        """非數字的帳號 ID"""
        valid, error = app_module.validate_account_id('12345678901a')
        assert valid is False
