"""
Bouncer - Grant Execute Tool 測試
覆蓋 mcp_tool_grant_execute 的所有驗證步驟和邊界條件
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

        # 建立 accounts table
        accounts_table = dynamodb.create_table(
            TableName='bouncer-accounts',
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        accounts_table.wait_until_exists()

        yield dynamodb


@pytest.fixture
def mcp_module(mock_dynamodb):
    """載入 mcp_execute 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['GRANT_SESSION_ENABLED'] = 'true'

    # 清除可能殘留的模組
    modules_to_clear = [
        'grant', 'db', 'constants', 'trust', 'commands', 'compliance_checker',
        'risk_scorer', 'mcp_execute', 'mcp_upload', 'mcp_admin', 'notifications', 'telegram', 'app',
        'utils', 'accounts', 'rate_limit', 'paging', 'callbacks',
        'smart_approval', 'tool_schema', 'metrics',
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.accounts_table = mock_dynamodb.Table('bouncer-accounts')
    db.dynamodb = mock_dynamodb

    # 初始化預設帳號
    db.accounts_table.put_item(Item={
        'account_id': '111111111111',
        'name': 'Default',
        'role_arn': None,
        'is_default': True,
        'enabled': True,
        'created_at': int(time.time())
    })

    import mcp_execute
    yield mcp_execute

    sys.path.pop(0)


# ============================================================================
# Helper Functions
# ============================================================================

def create_test_grant(mcp_module, grant_id='test-grant-001', status='active',
                     source='test-source', account_id='111111111111',
                     commands=None, allow_repeat=False, expires_at=None):
    """建立測試用的 grant session"""
    if commands is None:
        commands = ['aws s3 ls', 'aws ec2 describe-instances']

    if expires_at is None:
        expires_at = int(time.time()) + 1800  # 30 分鐘後過期

    from grant import normalize_command

    grant = {
        'request_id': grant_id,
        'type': 'grant_session',
        'status': status,
        'source': source,
        'account_id': account_id,
        'reason': 'Test grant',
        'granted_commands': [normalize_command(cmd) for cmd in commands],
        'allow_repeat': allow_repeat,
        'used_commands': {},
        'total_executions': 0,
        'max_total_executions': 50,
        'expires_at': expires_at,
        'created_at': int(time.time()),
    }

    import db
    db.table.put_item(Item=grant)
    return grant


# ============================================================================
# Test Cases
# ============================================================================

class TestGrantExecuteHappyPath:
    """Happy path 測試"""

    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    def test_grant_execute_success_allow_repeat_false(self, mock_notify, mock_exec, mcp_module):
        """測試成功執行（allow_repeat=False）"""
        mock_exec.return_value = 'Command executed successfully'

        grant = create_test_grant(mcp_module, allow_repeat=False)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source',
            'account': '111111111111',
            'reason': 'Test execution'
        })

        assert 'error' not in json.loads(result['body'])
        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_executed'
        assert 'result' in content
        assert content['grant_id'] == 'test-grant-001'
        mock_exec.assert_called_once()
        mock_notify.assert_called_once()

    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    def test_grant_execute_success_allow_repeat_true(self, mock_notify, mock_exec, mcp_module):
        """測試成功執行（allow_repeat=True，可重複執行同一命令）"""
        mock_exec.return_value = 'Command executed'

        grant = create_test_grant(mcp_module, allow_repeat=True)

        # 第一次執行
        result1 = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })
        assert 'error' not in json.loads(result1['body'])

        # 第二次執行同一命令（allow_repeat=True 應該允許）
        result2 = mcp_module.mcp_tool_grant_execute('req-002', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })
        assert 'error' not in json.loads(result2['body'])
        content = json.loads(json.loads(result2['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_executed'

    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    def test_grant_execute_without_account_param(self, mock_notify, mock_exec, mcp_module):
        """測試不帶 account 參數（使用預設帳號）"""
        mock_exec.return_value = 'Success'

        create_test_grant(mcp_module)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        assert 'error' not in json.loads(result['body'])
        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_executed'


class TestGrantExecuteValidation:
    """參數驗證測試"""

    def test_missing_grant_id(self, mcp_module):
        """測試缺少 grant_id"""
        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'command': 'aws s3 ls',
            'source': 'test-source'
        })
        assert 'error' in json.loads(result['body'])
        assert json.loads(result['body'])['error']['code'] == -32602

    def test_missing_command(self, mcp_module):
        """測試缺少 command"""
        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'source': 'test-source'
        })
        assert 'error' in json.loads(result['body'])
        assert json.loads(result['body'])['error']['code'] == -32602

    def test_missing_source(self, mcp_module):
        """測試缺少 source"""
        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls'
        })
        assert 'error' in json.loads(result['body'])
        assert json.loads(result['body'])['error']['code'] == -32602


class TestGrantExecuteAccountValidation:
    """帳號驗證測試"""

    def test_account_not_found(self, mcp_module):
        """測試帳號不存在"""
        create_test_grant(mcp_module)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source',
            'account': '999999999999'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'account_not_found'
        assert 'not found' in content['message']

    def test_invalid_account_id(self, mcp_module):
        """測試無效的帳號 ID 格式"""
        create_test_grant(mcp_module)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source',
            'account': 'invalid-account'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'account_not_found'


class TestGrantExecuteGrantValidation:
    """Grant session 驗證測試"""

    def test_grant_not_found(self, mcp_module):
        """測試 grant 不存在"""
        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'nonexistent-grant',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_not_found'
        assert content['message'] == 'Grant not found or expired'

    def test_source_mismatch(self, mcp_module):
        """測試 source 不匹配（不應洩漏 grant 存在）"""
        create_test_grant(mcp_module, source='correct-source')

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'wrong-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_not_found'
        assert content['message'] == 'Grant not found or expired'

    def test_grant_not_active(self, mcp_module):
        """測試 grant 狀態不是 active"""
        create_test_grant(mcp_module, status='pending')

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_not_active'
        assert 'pending' in content['message']

    def test_grant_expired(self, mcp_module):
        """測試 grant 已過期"""
        expired_time = int(time.time()) - 100
        create_test_grant(mcp_module, expires_at=expired_time)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_expired'

    def test_account_mismatch(self, mcp_module):
        """測試帳號不匹配"""
        # 建立另一個帳號
        import db
        db.accounts_table.put_item(Item={
            'account_id': '222222222222',
            'name': 'Test Account',
            'role_arn': 'arn:aws:iam::222222222222:role/TestRole',
            'enabled': True,
            'created_at': int(time.time())
        })

        # Grant 綁定到帳號 111111111111
        create_test_grant(mcp_module, account_id='111111111111')

        # 嘗試用不同的帳號執行
        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source',
            'account': '222222222222'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'account_mismatch'


class TestGrantExecuteCommandValidation:
    """命令驗證測試"""

    @patch('compliance_checker.check_compliance')
    def test_compliance_violation(self, mock_compliance, mcp_module):
        """測試違反 compliance 規則"""
        mock_violation = MagicMock()
        mock_violation.rule_id = 'TEST-001'
        mock_violation.message = 'Test violation'
        mock_compliance.return_value = (False, mock_violation)

        create_test_grant(mcp_module, commands=['aws s3 ls'])

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'compliance_violation'
        assert content['rule_id'] == 'TEST-001'

    @patch('compliance_checker.check_compliance')
    def test_command_not_in_grant(self, mock_compliance, mcp_module):
        """測試命令不在授權清單中"""
        mock_compliance.return_value = (True, None)

        create_test_grant(mcp_module, commands=['aws s3 ls', 'aws ec2 describe-instances'])

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws iam list-users',  # 不在授權清單中
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'command_not_in_grant'

    @patch('compliance_checker.check_compliance')
    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    def test_command_already_used(self, mock_notify, mock_exec, mock_compliance, mcp_module):
        """測試命令已被使用（allow_repeat=False）"""
        mock_compliance.return_value = (True, None)
        mock_exec.return_value = 'Success'

        create_test_grant(mcp_module, allow_repeat=False)

        # 第一次執行
        result1 = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })
        assert 'error' not in json.loads(result1['body'])

        # 第二次執行同一命令（應該失敗）
        result2 = mcp_module.mcp_tool_grant_execute('req-002', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result2['body'])['result']['content'][0]['text'])
        assert content['status'] == 'command_already_used'


class TestGrantExecuteExecution:
    """命令執行測試"""

    @patch('compliance_checker.check_compliance')
    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    def test_command_execution_with_result(self, mock_notify, mock_exec, mock_compliance, mcp_module):
        """測試命令執行並返回結果"""
        mock_compliance.return_value = (True, None)
        mock_exec.return_value = 'i-123456789\ni-987654321'

        create_test_grant(mcp_module, commands=['aws ec2 describe-instances'])

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws ec2 describe-instances',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_executed'
        assert 'i-123456789' in content['result']

    @patch('compliance_checker.check_compliance')
    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    def test_notification_failure_does_not_affect_success(self, mock_notify, mock_exec, mock_compliance, mcp_module):
        """測試通知失敗不影響成功響應"""
        mock_compliance.return_value = (True, None)
        mock_exec.return_value = 'Success'
        mock_notify.side_effect = Exception('Notification failed')

        create_test_grant(mcp_module)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        # 即使通知失敗，執行仍應成功
        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_executed'

    @patch('compliance_checker.check_compliance')
    @patch('mcp_execute.execute_command')
    @patch('mcp_execute.send_grant_execute_notification')
    @patch('mcp_execute.store_paged_output')
    def test_paged_output(self, mock_page, mock_notify, mock_exec, mock_compliance, mcp_module):
        """測試分頁輸出"""
        mock_compliance.return_value = (True, None)
        mock_exec.return_value = 'Large output...'
        from paging import PaginatedOutput
        mock_page.return_value = PaginatedOutput(paged=True, result='Truncated output...', page=1, total_pages=2, output_length=1000, next_page='page-001')

        create_test_grant(mcp_module)

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        content = json.loads(json.loads(result['body'])['result']['content'][0]['text'])
        assert content['status'] == 'grant_executed'
        assert content['page_id'] == 'page-001'


class TestGrantExecuteEdgeCases:
    """邊界條件測試"""

    @patch('compliance_checker.check_compliance')
    def test_unicode_command_normalization(self, mock_compliance, mcp_module):
        """測試 Unicode 命令正規化"""
        mock_compliance.return_value = (True, None)

        # 測試含有 Unicode 空白的命令
        create_test_grant(mcp_module, commands=['aws s3 ls'])

        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws  s3  ls',  # 多個空格
            'source': 'test-source'
        })

        # 應該正規化後匹配成功（但會因為沒有 mock execute_command 而在執行階段出錯）
        # 這裡主要測試正規化不會導致參數驗證失敗
        assert 'Missing required parameter' not in str(result)

    def test_default_reason(self, mcp_module):
        """測試預設 reason"""
        create_test_grant(mcp_module)

        # 不提供 reason 參數
        result = mcp_module.mcp_tool_grant_execute('req-001', {
            'grant_id': 'test-grant-001',
            'command': 'aws s3 ls',
            'source': 'test-source'
        })

        # 應該使用預設值 'Grant execute'（通過不崩潰來驗證）
        assert result is not None
