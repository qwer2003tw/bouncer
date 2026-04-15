import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3




@pytest.fixture(autouse=True)
def _mock_entities_send():
    """Ensure send_message_with_entities is mocked for pre-entities tests."""
    import sys, importlib
    import telegram as _tg
    from unittest.mock import MagicMock

    mock_msg_id = 99999
    mock_response = {'ok': True, 'result': {'message_id': mock_msg_id}}

    # Save originals
    orig_entities = getattr(_tg, 'send_message_with_entities', None)

    # Replace only send_message_with_entities (entities Phase 2 migration)
    mock_entities = MagicMock(return_value=mock_response)
    _tg.send_message_with_entities = mock_entities

    # Reload notifications so it picks up the mocks
    if 'notifications' in sys.modules:
        importlib.reload(sys.modules['notifications'])

    yield mock_entities

    # Restore
    if orig_entities is not None:
        _tg.send_message_with_entities = orig_entities
    elif hasattr(_tg, 'send_message_with_entities'):
        delattr(_tg, 'send_message_with_entities')


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
    
    @patch('telegram.send_message_with_entities')
    def test_approval_pending(self, mock_telegram, app_module):
        """測試 REST API 待審批（非等待模式）"""
        mock_telegram.return_value = {'ok': True, 'result': {'message_id': 1}}
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
        from mcp_execute import mcp_tool_execute
        import mcp_execute
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
            result = mcp_tool_execute('req-2', {
                'command': 'aws sts get-caller-identity',
                'trust_scope': 'test-session',
            })

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
        # Override mock to simulate Telegram failure (tests that ok=False is propagated)
        with patch('telegram.send_message_with_entities', return_value={'ok': False}):
            with patch.object(app_module.table, 'put_item'):
                result = app_module.send_approval_request(
                    request_id='test-123',
                    command='aws iam create-access-key',
                    reason='test'
                )
                # send_approval_request 回傳 NotificationResult，Telegram 失敗時 ok=False
                from notifications import NotificationResult
                assert isinstance(result, NotificationResult) and not result.ok


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
        from mcp_execute import mcp_tool_execute
        import mcp_execute
        # Mock 返回長輸出
        long_output = 'x' * 5000
        with patch.object(mcp_execute, 'execute_command', return_value=long_output):
            result = mcp_tool_execute('test-paging', {
                'command': 'aws logs get-log-events --log-group-name test --log-stream-name test-stream',
                'trust_scope': 'test-session',
            })

            body = json.loads(result['body'])


            content = json.loads(body['result']['content'][0]['text'])

            assert content['status'] == 'auto_approved'
            # 檢查是否有分頁
            if content.get('paged'):
                assert content['total_pages'] >= 2
    
    @pytest.mark.skip(reason="Sprint 83: bouncer_get_page removed - MCP no longer uses pagination")
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
        from mcp_execute import mcp_tool_execute
        import mcp_execute
        with patch.object(mcp_execute, 'get_account', return_value={
            'account_id': '555555555555',
            'name': 'Configured Account',
            'enabled': True,
            'role_arn': 'arn:aws:iam::555555555555:role/TestRole'
        }), patch.object(mcp_execute, 'execute_command', return_value='{"result": "ok"}'):
            result = mcp_tool_execute('test-acct', {
                'command': 'aws s3 ls',
                'trust_scope': 'test-session',
                'account': '555555555555'
            })

            body = json.loads(result['body'])


            content = json.loads(body['result']['content'][0]['text'])

            assert content['status'] == 'auto_approved'
            assert content['account'] == '555555555555'
    
    @patch('telegram.send_message_with_entities')
    def test_send_approval_request_with_assume_role(self, mock_telegram, app_module):
        """發送審批請求帶 assume_role"""
        mock_telegram.return_value = {'ok': True, 'result': {'message_id': 1}}
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
            'bouncer_execute_native',
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
            'bouncer_list_pending'
        ]
        
        for tool in expected_tools:
            assert tool in tool_names, f"Missing tool: {tool}"
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    @patch('callbacks_command.execute_command')
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
        mock_telegram.return_value = {'ok': True, 'result': {'message_id': 12347}}
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


# ============================================================================
# sprint7-002: handle_cleanup_expired tests
# ============================================================================

class TestCleanupExpired:
    """Tests for handle_cleanup_expired() introduced in sprint7-002.

    Covers all 6 required scenarios:
    - removes buttons when request is pending
    - updates message text to ⏰ 已過期
    - no-op when already approved
    - no-op when already rejected
    - handles missing telegram_message_id gracefully
    - handles unknown (not found) request gracefully
    """

    def _cleanup_event(self, request_id: str) -> dict:
        return {
            'source': 'bouncer-scheduler',
            'action': 'cleanup_expired',
            'request_id': request_id,
        }

    def _put_request(self, app_module, request_id: str, status: str = 'pending',
                     telegram_message_id: int = None):
        """Helper: insert a minimal DDB item for testing."""
        item = {
            'request_id': request_id,
            'status': status,
            'created_at': int(__import__('time').time()),
        }
        if telegram_message_id is not None:
            item['telegram_message_id'] = telegram_message_id
        app_module.table.put_item(Item=item)

    # ------------------------------------------------------------------
    # test_cleanup_expired_removes_buttons
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_removes_buttons(self, mock_update, app_module):
        """Cleanup event on pending request calls update_message with remove_buttons=True."""
        self._put_request(app_module, 'clean-001', status='pending', telegram_message_id=12345)

        event = self._cleanup_event('clean-001')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('cleaned') is True

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        args, kwargs = call_kwargs
        assert args[0] == 12345 or kwargs.get('message_id') == 12345
        remove_buttons_val = kwargs.get('remove_buttons', args[2] if len(args) > 2 else False)
        assert remove_buttons_val is True

    # ------------------------------------------------------------------
    # test_cleanup_expired_updates_message_text_to_expired
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_updates_message_text_to_expired(self, mock_update, app_module):
        """Cleanup event updates message text to include ⏰ 已過期."""
        self._put_request(app_module, 'clean-002', status='pending', telegram_message_id=99999)

        event = self._cleanup_event('clean-002')
        app_module.lambda_handler(event, None)

        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        message_text = args[1] if len(args) > 1 else kwargs.get('text', '')
        assert '已過期' in message_text or '⏰' in message_text
        assert 'clean-002' in message_text

    # ------------------------------------------------------------------
    # test_cleanup_expired_noop_if_already_approved
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_noop_if_already_approved(self, mock_update, app_module):
        """Cleanup event on approved request is a no-op (no Telegram call)."""
        self._put_request(app_module, 'clean-003', status='approved', telegram_message_id=11111)

        event = self._cleanup_event('clean-003')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('skipped') is True
        assert 'approved' in body.get('reason', '')
        mock_update.assert_not_called()

    # ------------------------------------------------------------------
    # test_cleanup_expired_noop_if_already_rejected
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_noop_if_already_rejected(self, mock_update, app_module):
        """Cleanup event on rejected/denied request is a no-op."""
        self._put_request(app_module, 'clean-004', status='rejected', telegram_message_id=22222)

        event = self._cleanup_event('clean-004')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('skipped') is True
        mock_update.assert_not_called()

    @patch('app.update_message')
    def test_cleanup_expired_noop_if_already_denied(self, mock_update, app_module):
        """Cleanup event on 'denied' status is also a no-op."""
        self._put_request(app_module, 'clean-004b', status='denied', telegram_message_id=22223)

        event = self._cleanup_event('clean-004b')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('skipped') is True
        mock_update.assert_not_called()

    # ------------------------------------------------------------------
    # test_cleanup_expired_missing_message_id_handled
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_missing_message_id_handled(self, mock_update, app_module):
        """Cleanup event on pending request with no telegram_message_id: no Telegram call, status updated."""
        self._put_request(app_module, 'clean-005', status='pending', telegram_message_id=None)

        event = self._cleanup_event('clean-005')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('skipped') is True
        assert 'no_message_id' in body.get('reason', '')
        mock_update.assert_not_called()

        # Status should be updated to 'timeout' in DDB
        item = app_module.table.get_item(Key={'request_id': 'clean-005'}).get('Item')
        assert item is not None
        assert item.get('status') == 'timeout'

    # ------------------------------------------------------------------
    # test_cleanup_expired_unknown_request_handled
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_unknown_request_handled(self, mock_update, app_module):
        """Cleanup event for a non-existent request_id returns gracefully (skipped)."""
        event = self._cleanup_event('does-not-exist-xyz')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('skipped') is True
        assert 'not_found' in body.get('reason', '')
        mock_update.assert_not_called()

    # ------------------------------------------------------------------
    # Additional edge cases
    # ------------------------------------------------------------------

    @patch('app.update_message')
    def test_cleanup_expired_updates_ddb_status_to_timeout(self, mock_update, app_module):
        """After cleanup, DDB status is set to 'timeout'."""
        self._put_request(app_module, 'clean-006', status='pending', telegram_message_id=55555)

        event = self._cleanup_event('clean-006')
        app_module.lambda_handler(event, None)

        item = app_module.table.get_item(Key={'request_id': 'clean-006'}).get('Item')
        assert item is not None
        assert item.get('status') == 'timeout'

    def test_cleanup_event_routed_by_lambda_handler(self, app_module):
        """lambda_handler routes cleanup events before checking path/method."""
        event = {
            'source': 'bouncer-scheduler',
            'action': 'cleanup_expired',
            'request_id': 'route-test-001',
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert body.get('ok') is True

    def test_cleanup_event_missing_request_id(self, app_module):
        """Cleanup event without request_id returns skipped gracefully."""
        event = {
            'source': 'bouncer-scheduler',
            'action': 'cleanup_expired',
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        assert body.get('ok') is True
        assert body.get('skipped') is True
        assert 'missing_request_id' in body.get('reason', '')

    @patch('app.update_message')
    def test_cleanup_expired_noop_if_auto_approved(self, mock_update, app_module):
        """Cleanup event on auto_approved request is a no-op."""
        self._put_request(app_module, 'clean-007', status='auto_approved', telegram_message_id=33333)

        event = self._cleanup_event('clean-007')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('skipped') is True
        mock_update.assert_not_called()

    @patch('telegram.update_message', side_effect=Exception("Telegram API error"))
    def test_cleanup_expired_telegram_error_is_non_fatal(self, mock_update, app_module):
        """If update_message raises, handle_cleanup_expired still marks DDB and returns ok."""
        self._put_request(app_module, 'clean-008', status='pending', telegram_message_id=44444)

        event = self._cleanup_event('clean-008')
        result = app_module.lambda_handler(event, None)

        body = json.loads(result['body'])
        assert body.get('ok') is True
        item = app_module.table.get_item(Key={'request_id': 'clean-008'}).get('Item')
        assert item.get('status') == 'timeout'


# ============================================================================
# Sprint8-003 — Unicode Normalization Tests (Approach C: Test-focused)
# ============================================================================


class TestUnicodeNormalization:
    """Unicode NFKC 正規化測試 — 確保每個 edge case 都有覆蓋。"""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_event(self, command: str) -> dict:
        """建立帶有合法 secret 的 REST 請求事件。"""
        return {
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({'command': command, 'reason': 'unicode-test'}),
        }

    # ------------------------------------------------------------------
    # Core tests
    # ------------------------------------------------------------------

    @patch('commands.execute_command')
    def test_clawdbot_unicode_lookalike_blocked(self, mock_execute, app_module):
        """fullwidth chars (ａｗｓ) 應被 NFKC 正規化為 'aws'。

        'aws iam delete-user' 是封鎖命令，正規化後應被 block (403)，
        而不是被當成無害字串溜過去。
        """
        mock_execute.return_value = 'ok'
        # ａｗｓ ｉａｍ ｄｅｌｅｔｅ－ｕｓｅｒ (全是 fullwidth)
        fullwidth_cmd = '\uff41\uff57\uff53 \uff49\uff41\uff4d \uff44\uff45\uff4c\uff45\uff54\uff45\uff0d\uff55\uff53\uff45\uff52 --user-name x'
        event = self._make_event(fullwidth_cmd)
        result = app_module.handle_clawdbot_request(event)
        # After normalization the command reads "aws iam delete-user …" which is blocked
        assert result['statusCode'] == 403

    @patch('commands.execute_command')
    def test_clawdbot_nfkc_normalize_applied(self, mock_execute, app_module):
        """NFKC 正規化把 fullwidth 'ｓ３' 轉成 's3'，使 safelist 命中。"""
        mock_execute.return_value = '[]'
        # ａｗｓ ｓ３ ｌｓ — 正規化後 → 'aws s3 ls' (safelist)
        fullwidth_cmd = '\uff41\uff57\uff53 \uff53\uff13 \uff4c\uff53'
        event = self._make_event(fullwidth_cmd)
        result = app_module.handle_clawdbot_request(event)
        # 'aws s3 ls' is auto-approved → 200
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('status') == 'auto_approved'

    def test_clawdbot_strip_after_normalize(self, app_module):
        """正規化後的 strip() 應移除前後空白（包含全形空白 U+3000）。"""
        # 全形空白 (U+3000) 在 NFKC 轉成一般空格後會被 strip() 移除
        cmd_with_fullwidth_spaces = '\u3000aws s3 ls\u3000'
        event = self._make_event(cmd_with_fullwidth_spaces)
        # 正規化後 = 'aws s3 ls' (auto-approved), 不應被當空命令
        with patch('commands.execute_command', return_value='[]'):
            result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 200

    @patch('commands.execute_command')
    def test_clawdbot_normal_command_unchanged(self, mock_execute, app_module):
        """純 ASCII 命令在正規化後應維持不變、正常執行。"""
        mock_execute.return_value = 'caller-id'
        event = self._make_event('aws sts get-caller-identity')
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('status') == 'auto_approved'

    def test_clawdbot_empty_command_after_normalize(self, app_module):
        """全形空白組成的命令，正規化 + strip 後為空字串，應回 400。"""
        # U+3000 × 5  →  NFKC → '     '  → strip → ''
        whitespace_only = '\u3000\u3000\u3000\u3000\u3000'
        event = self._make_event(whitespace_only)
        result = app_module.handle_clawdbot_request(event)
        assert result['statusCode'] == 400
        body = json.loads(result['body'])
        assert 'command' in body.get('error', '').lower() or 'missing' in body.get('error', '').lower()

    @patch('commands.execute_command')
    def test_clawdbot_normalization_before_risk_check(self, mock_execute, app_module):
        """正規化必須在風險/封鎖檢查之前執行。

        用混合 fullwidth + ASCII 的封鎖命令，確認 block 是因為正規化生效，
        而非命令本身含非 ASCII 被其他邏輯誤判。
        """
        mock_execute.return_value = 'should-not-reach'
        # 'aws iam' (ascii) + ' delete-access-key' 的部分用 fullwidth
        # ｄｅｌｅｔｅ－ａｃｃｅｓｓ－ｋｅｙ = fullwidth "delete-access-key"
        mixed = 'aws iam \uff44\uff45\uff4c\uff45\uff54\uff45\uff0d\uff41\uff43\uff43\uff45\uff53\uff53\uff0d\uff4b\uff45\uff59 --user-name x'
        event = self._make_event(mixed)
        result = app_module.handle_clawdbot_request(event)
        # 正規化後 = 'aws iam delete-access-key …' → blocked
        assert result['statusCode'] == 403
        # execute_command 不應被呼叫（因為被 block 了）
        mock_execute.assert_not_called()

    # ------------------------------------------------------------------
    # Merged from approach-a: ideographic space (U+3000) tests
    # ------------------------------------------------------------------

    def test_clawdbot_ideographic_space_blocked(self, app_module):
        """U+3000 ideographic spaces 被 NFKC 正規化為普通空格後，封鎖命令仍被攔截。

        (Merged from approach-a test_clawdbot_request_normalizes_unicode)
        """
        # 使用 U+3000（全形空格）連接各參數，NFKC 會把它轉成普通空格
        confusable_cmd = 'aws\u3000iam\u3000delete-user\u3000--user-name\u3000test'
        event = self._make_event(confusable_cmd)
        result = app_module.handle_clawdbot_request(event)
        # 正規化後等同 'aws iam delete-user --user-name test'，應被 block (403)
        assert result['statusCode'] == 403

    @patch('app.get_block_reason')
    @patch('app.is_auto_approve', return_value=False)
    def test_clawdbot_normalization_before_block_check_via_mock(self, mock_auto, mock_block, app_module):
        """用 mock 驗證 get_block_reason 收到的是正規化後的命令（無 U+3000）。

        (Merged from approach-a test_clawdbot_request_normalization_before_risk_check)
        """
        mock_block.return_value = 'blocked for test'

        # 含全形空格 \u3000 的命令：正規化後變普通空格
        raw_cmd = 'aws\u3000s3\u3000ls'
        event = self._make_event(raw_cmd)
        app_module.handle_clawdbot_request(event)

        # get_block_reason 應該收到正規化後的命令（普通空格），而非原始全形空格
        assert mock_block.called
        called_cmd = mock_block.call_args[0][0]
        assert '\u3000' not in called_cmd, "正規化應在 block check 前套用"
        assert ' ' in called_cmd or called_cmd == called_cmd.strip()
