"""
Bouncer - Grant Session 測試
覆蓋 grant.py 核心功能 + mcp_tools._check_grant_session pipeline
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
        yield dynamodb


@pytest.fixture
def grant_module(mock_dynamodb):
    """載入 grant 模組並注入 mock"""
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
        'risk_scorer', 'mcp_tools', 'notifications', 'telegram', 'app',
        'utils', 'accounts', 'rate_limit', 'paging', 'callbacks',
        'smart_approval', 'tool_schema', 'metrics',
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import grant
    yield grant

    sys.path.pop(0)


@pytest.fixture
def mcp_module(mock_dynamodb):
    """載入 mcp_tools 模組（用於測試 _check_grant_session pipeline）"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['GRANT_SESSION_ENABLED'] = 'true'

    modules_to_clear = [
        'grant', 'db', 'constants', 'trust', 'commands', 'compliance_checker',
        'risk_scorer', 'mcp_tools', 'notifications', 'telegram', 'app',
        'utils', 'accounts', 'rate_limit', 'paging', 'callbacks',
        'smart_approval', 'tool_schema', 'metrics',
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import mcp_tools
    yield mcp_tools

    sys.path.pop(0)


# ============================================================================
# normalize_command Tests
# ============================================================================

class TestNormalizeCommand:
    """normalize_command 測試"""

    def test_basic(self, grant_module):
        assert grant_module.normalize_command('aws s3 ls') == 'aws s3 ls'

    def test_strip_whitespace(self, grant_module):
        assert grant_module.normalize_command('  aws s3 ls  ') == 'aws s3 ls'

    def test_collapse_spaces(self, grant_module):
        assert grant_module.normalize_command('aws  s3   ls') == 'aws s3 ls'

    def test_lowercase(self, grant_module):
        assert grant_module.normalize_command('AWS S3 LS') == 'aws s3 ls'

    def test_mixed(self, grant_module):
        assert grant_module.normalize_command('  AWS  EC2  Describe-Instances  ') == 'aws ec2 describe-instances'

    def test_tabs_and_newlines(self, grant_module):
        assert grant_module.normalize_command('aws\ts3\nls') == 'aws s3 ls'

    def test_empty_string(self, grant_module):
        assert grant_module.normalize_command('') == ''

    def test_single_word(self, grant_module):
        assert grant_module.normalize_command('AWS') == 'aws'

    def test_with_parameters(self, grant_module):
        result = grant_module.normalize_command('aws ec2 describe-instances --instance-ids i-1234567890abcdef0')
        assert result == 'aws ec2 describe-instances --instance-ids i-1234567890abcdef0'


# ============================================================================
# create_grant_request Tests
# ============================================================================

class TestCreateGrantRequest:
    """create_grant_request 測試"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_basic_creation(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試基本 grant 建立"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls', 'aws ec2 describe-instances'],
            reason='部署前檢查',
            source='Private Bot',
            account_id='111111111111',
        )

        assert result['status'] == 'pending_approval'
        assert result['grant_id'].startswith('grant_')
        assert len(result['grant_id']) == 6 + 32  # "grant_" + 32 hex chars
        assert result['summary']['total'] == 2
        assert result['summary']['grantable'] == 2
        assert result['ttl_minutes'] == 30  # default

    def test_empty_commands(self, grant_module):
        """測試空命令清單"""
        with pytest.raises(ValueError, match="commands 不能為空"):
            grant_module.create_grant_request(
                commands=[],
                reason='test',
                source='Bot',
                account_id='111111111111',
            )

    def test_too_many_commands(self, grant_module):
        """測試超過上限的命令數量"""
        with pytest.raises(ValueError, match="commands 數量不能超過"):
            grant_module.create_grant_request(
                commands=[f'aws s3 ls s3://bucket-{i}' for i in range(21)],
                reason='test',
                source='Bot',
                account_id='111111111111',
            )

    def test_missing_reason(self, grant_module):
        """測試缺少 reason"""
        with pytest.raises(ValueError, match="reason 不能為空"):
            grant_module.create_grant_request(
                commands=['aws s3 ls'],
                reason='',
                source='Bot',
                account_id='111111111111',
            )

    def test_missing_source(self, grant_module):
        """測試缺少 source"""
        with pytest.raises(ValueError, match="source 不能為空"):
            grant_module.create_grant_request(
                commands=['aws s3 ls'],
                reason='test',
                source='',
                account_id='111111111111',
            )

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_ttl_clamping(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試 TTL 上下限"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 10
        mock_risk.return_value = mock_risk_result

        # TTL 超過上限
        result = grant_module.create_grant_request(
            commands=['aws s3 ls'],
            reason='test',
            source='Bot',
            account_id='111111111111',
            ttl_minutes=120,
        )
        assert result['ttl_minutes'] == 60  # clamped to max

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_allow_repeat(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試 allow_repeat 設定"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 10
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'],
            reason='test',
            source='Bot',
            account_id='111111111111',
            allow_repeat=True,
        )
        assert result['allow_repeat'] is True


# ============================================================================
# Precheck Category Tests
# ============================================================================

class TestPrecheckCategories:
    """預檢分類測試"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_grantable(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """低風險命令 → grantable"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'],
            reason='test', source='Bot', account_id='111111111111',
        )
        assert result['commands_detail'][0]['category'] == 'grantable'
        assert result['summary']['grantable'] == 1

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=True)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_blocked(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """封鎖清單命令 → blocked"""
        result = grant_module.create_grant_request(
            commands=['aws iam create-user --user-name hacker'],
            reason='test', source='Bot', account_id='111111111111',
        )
        assert result['commands_detail'][0]['category'] == 'blocked'
        assert result['summary']['blocked'] == 1

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=True)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_trust_excluded(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """Trust excluded 命令 → requires_individual"""
        result = grant_module.create_grant_request(
            commands=['aws iam list-users'],
            reason='test', source='Bot', account_id='111111111111',
        )
        assert result['commands_detail'][0]['category'] == 'requires_individual'
        assert result['summary']['requires_individual'] == 1

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_high_risk_score(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """高風險分數 → requires_individual"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 75
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws ec2 terminate-instances --instance-ids i-12345'],
            reason='test', source='Bot', account_id='111111111111',
        )
        assert result['commands_detail'][0]['category'] == 'requires_individual'

    def test_compliance_blocked(self, grant_module):
        """合規違規 → blocked"""
        mock_violation = MagicMock()
        mock_violation.rule_name = 'test-rule'

        with patch('compliance_checker.check_compliance', return_value=(False, mock_violation)):
            with patch('commands.is_blocked', return_value=False):
                result = grant_module.create_grant_request(
                    commands=['aws lambda add-permission --principal *'],
                    reason='test', source='Bot', account_id='111111111111',
                )
                assert result['commands_detail'][0]['category'] == 'blocked'

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked')
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_mixed_categories(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """混合分類"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result
        # First command safe, second blocked
        mock_blocked.side_effect = [False, True]

        result = grant_module.create_grant_request(
            commands=['aws s3 ls', 'aws iam create-user --user-name hacker'],
            reason='test', source='Bot', account_id='111111111111',
        )
        assert result['summary']['grantable'] == 1
        assert result['summary']['blocked'] == 1


# ============================================================================
# approve_grant Tests
# ============================================================================

class TestApproveGrant:
    """approve_grant 測試"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_approve_all(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試全部批准"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        # 建立 grant
        result = grant_module.create_grant_request(
            commands=['aws s3 ls', 'aws ec2 describe-instances'],
            reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']

        # 批准
        grant = grant_module.approve_grant(grant_id, '999999999', mode='all')
        assert grant is not None
        assert grant['status'] == 'active'
        assert len(grant['granted_commands']) == 2
        assert grant['approved_by'] == '999999999'
        assert 'expires_at' in grant
        assert grant['expires_at'] > int(time.time())

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded')
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_approve_safe_only(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試只批准安全命令"""
        mock_risk_result_safe = MagicMock()
        mock_risk_result_safe.score = 20
        mock_risk_result_high = MagicMock()
        mock_risk_result_high.score = 20

        mock_risk.side_effect = [mock_risk_result_safe, mock_risk_result_high]
        mock_excluded.side_effect = [False, True]  # second cmd is trust-excluded

        result = grant_module.create_grant_request(
            commands=['aws s3 ls', 'aws iam get-role --role-name test'],
            reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']

        # safe_only mode
        grant = grant_module.approve_grant(grant_id, '999999999', mode='safe_only')
        assert grant is not None
        assert len(grant['granted_commands']) == 1  # only the grantable one

    def test_approve_nonexistent(self, grant_module):
        """測試批准不存在的 grant"""
        result = grant_module.approve_grant('grant_nonexistent', '999999999')
        assert result is None

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_approve_already_approved(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試重複批准"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']

        # First approve
        grant = grant_module.approve_grant(grant_id, '999999999')
        assert grant is not None

        # Second approve should fail (status is now active, not pending_approval)
        grant2 = grant_module.approve_grant(grant_id, '999999999')
        assert grant2 is None


# ============================================================================
# deny_grant / revoke_grant Tests
# ============================================================================

class TestDenyRevoke:
    """deny_grant / revoke_grant 測試"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_deny(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']

        success = grant_module.deny_grant(grant_id)
        assert success is True

        # Verify status
        grant = grant_module.get_grant_session(grant_id)
        assert grant['status'] == 'denied'

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_revoke(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']

        # Approve first
        grant_module.approve_grant(grant_id, '999999999')

        # Then revoke
        success = grant_module.revoke_grant(grant_id)
        assert success is True

        grant = grant_module.get_grant_session(grant_id)
        assert grant['status'] == 'revoked'


# ============================================================================
# is_command_in_grant Tests
# ============================================================================

class TestIsCommandInGrant:
    """is_command_in_grant 測試"""

    def test_exact_match(self, grant_module):
        grant = {'granted_commands': ['aws s3 ls', 'aws ec2 describe-instances']}
        assert grant_module.is_command_in_grant('aws s3 ls', grant) is True
        assert grant_module.is_command_in_grant('aws ec2 describe-instances', grant) is True

    def test_no_match(self, grant_module):
        grant = {'granted_commands': ['aws s3 ls']}
        assert grant_module.is_command_in_grant('aws s3 cp', grant) is False

    def test_empty_grant(self, grant_module):
        grant = {'granted_commands': []}
        assert grant_module.is_command_in_grant('aws s3 ls', grant) is False

    def test_missing_key(self, grant_module):
        grant = {}
        assert grant_module.is_command_in_grant('aws s3 ls', grant) is False

    def test_case_sensitive(self, grant_module):
        """normalized commands should be lowercase"""
        grant = {'granted_commands': ['aws s3 ls']}
        assert grant_module.is_command_in_grant('AWS S3 LS', grant) is False
        assert grant_module.is_command_in_grant('aws s3 ls', grant) is True


# ============================================================================
# try_use_grant_command Tests
# ============================================================================

class TestTryUseGrantCommand:
    """try_use_grant_command 測試"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_one_time_use(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """一次性使用：第一次成功，第二次失敗"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        grant_module.approve_grant(grant_id, '999999999')

        # First use: success
        success = grant_module.try_use_grant_command(grant_id, 'aws s3 ls', allow_repeat=False)
        assert success is True

        # Second use: fail (already used)
        success = grant_module.try_use_grant_command(grant_id, 'aws s3 ls', allow_repeat=False)
        assert success is False

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_allow_repeat(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """允許重複：多次使用都成功"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
            allow_repeat=True,
        )
        grant_id = result['grant_id']
        grant_module.approve_grant(grant_id, '999999999')

        # Multiple uses: all succeed
        for _ in range(3):
            success = grant_module.try_use_grant_command(grant_id, 'aws s3 ls', allow_repeat=True)
            assert success is True

        # Verify count
        grant = grant_module.get_grant_session(grant_id)
        assert int(grant['total_executions']) == 3

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_revoked_grant_fails(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """撤銷後的 grant 不能使用"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        grant_module.approve_grant(grant_id, '999999999')
        grant_module.revoke_grant(grant_id)

        # Should fail because status is not 'active'
        success = grant_module.try_use_grant_command(grant_id, 'aws s3 ls', allow_repeat=False)
        assert success is False


# ============================================================================
# get_grant_status Tests
# ============================================================================

class TestGetGrantStatus:
    """get_grant_status 測試"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_basic_status(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls', 'aws ec2 describe-instances'],
            reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        grant_module.approve_grant(grant_id, '999999999')

        status = grant_module.get_grant_status(grant_id, 'Bot')
        assert status is not None
        assert status['status'] == 'active'
        assert status['granted_count'] == 2
        assert status['used_count'] == 0
        assert status['remaining_seconds'] > 0

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_source_mismatch(self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module):
        """測試 source 不匹配"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        result = grant_module.create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        grant_module.approve_grant(grant_id, '999999999')

        # Wrong source
        status = grant_module.get_grant_status(grant_id, 'Wrong-Bot')
        assert status is None

    def test_nonexistent_grant(self, grant_module):
        """測試不存在的 grant"""
        status = grant_module.get_grant_status('grant_nonexistent', 'Bot')
        assert status is None


# ============================================================================
# _check_grant_session Pipeline Tests
# ============================================================================

class TestCheckGrantSession:
    """_check_grant_session pipeline 測試"""

    def test_no_grant_id_fallthrough(self, mcp_module):
        """沒帶 grant_id → fallthrough (return None)"""
        ctx = mcp_module.ExecuteContext(
            req_id='test-1',
            command='aws s3 ls',
            reason='test',
            source='Bot',
            trust_scope='test-session',
            context=None,
            account_id='111111111111',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id=None,
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is None

    def test_invalid_grant_id_fallthrough(self, mcp_module):
        """無效 grant_id → fallthrough"""
        ctx = mcp_module.ExecuteContext(
            req_id='test-2',
            command='aws s3 ls',
            reason='test',
            source='Bot',
            trust_scope='test-session',
            context=None,
            account_id='111111111111',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id='grant_nonexistent',
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is None

    @patch('mcp_tools.send_grant_execute_notification')
    @patch('mcp_tools.execute_command', return_value='bucket1\nbucket2')
    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_active_grant_executes(self, mock_compliance, mock_blocked, mock_excluded,
                                    mock_risk, mock_exec, mock_notify, mcp_module):
        """有效 grant + 匹配命令 → 自動執行"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        from grant import create_grant_request, approve_grant

        result = create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        approve_grant(grant_id, '999999999')

        ctx = mcp_module.ExecuteContext(
            req_id='test-3',
            command='aws s3 ls',
            reason='test',
            source='Bot',
            trust_scope='test-session',
            context=None,
            account_id='111111111111',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id=grant_id,
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is not None
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])
        assert data['status'] == 'grant_auto_approved'
        assert data['grant_id'] == grant_id

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_source_mismatch_fallthrough(self, mock_compliance, mock_blocked, mock_excluded,
                                          mock_risk, mcp_module):
        """Source 不匹配 → fallthrough"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        from grant import create_grant_request, approve_grant

        result = create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        approve_grant(grant_id, '999999999')

        ctx = mcp_module.ExecuteContext(
            req_id='test-4',
            command='aws s3 ls',
            reason='test',
            source='Wrong-Bot',  # mismatched source
            trust_scope='test-session',
            context=None,
            account_id='111111111111',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id=grant_id,
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is None  # fallthrough

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_command_not_in_list_fallthrough(self, mock_compliance, mock_blocked, mock_excluded,
                                              mock_risk, mcp_module):
        """命令不在授權清單 → fallthrough"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        from grant import create_grant_request, approve_grant

        result = create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        approve_grant(grant_id, '999999999')

        ctx = mcp_module.ExecuteContext(
            req_id='test-5',
            command='aws ec2 describe-instances',  # not in grant
            reason='test',
            source='Bot',
            trust_scope='test-session',
            context=None,
            account_id='111111111111',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id=grant_id,
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is None  # fallthrough

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_expired_grant_fallthrough(self, mock_compliance, mock_blocked, mock_excluded,
                                        mock_risk, mcp_module):
        """過期 grant → fallthrough"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        from grant import create_grant_request, approve_grant
        import db

        result = create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        approve_grant(grant_id, '999999999')

        # Manually set expires_at to past
        db.table.update_item(
            Key={'request_id': grant_id},
            UpdateExpression='SET expires_at = :exp',
            ExpressionAttributeValues={':exp': int(time.time()) - 100},
        )

        ctx = mcp_module.ExecuteContext(
            req_id='test-6',
            command='aws s3 ls',
            reason='test',
            source='Bot',
            trust_scope='test-session',
            context=None,
            account_id='111111111111',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id=grant_id,
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is None  # fallthrough

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_account_mismatch_fallthrough(self, mock_compliance, mock_blocked, mock_excluded,
                                           mock_risk, mcp_module):
        """帳號不匹配 → fallthrough"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        from grant import create_grant_request, approve_grant

        result = create_grant_request(
            commands=['aws s3 ls'], reason='test', source='Bot', account_id='111111111111',
        )
        grant_id = result['grant_id']
        approve_grant(grant_id, '999999999')

        ctx = mcp_module.ExecuteContext(
            req_id='test-7',
            command='aws s3 ls',
            reason='test',
            source='Bot',
            trust_scope='test-session',
            context=None,
            account_id='222222222222',  # mismatched account
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
            grant_id=grant_id,
        )
        result = mcp_module._check_grant_session(ctx)
        assert result is None  # fallthrough
