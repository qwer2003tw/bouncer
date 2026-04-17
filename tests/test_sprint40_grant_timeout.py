"""
Regression test: Grant session configurable approval_timeout (#29)
Sprint 40 Task 2

Test that approval_timeout parameter works correctly:
- Default is 300s (5 min)
- Maximum is 900s (15 min)
- Minimum is 60s (1 min)
- Value is stored in DDB and returned in response
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

        # Create accounts table
        dynamodb.create_table(
            TableName='bouncer-accounts',
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )

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
        'risk_scorer', 'mcp_execute', 'mcp_upload', 'mcp_admin', 'notifications', 'telegram', 'app',
        'utils', 'accounts', 'rate_limit', 'paging', 'callbacks',
        'smart_approval', 'tool_schema', 'metrics',
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]


    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import grant
    yield grant

    sys.path.pop(0)


class TestGrantSessionConfigurableTimeout:
    """Test grant session configurable approval_timeout (#29)"""

    def test_approval_timeout_custom_600(self, grant_module, mock_dynamodb):
        """Test 1: approval_timeout=600 → ttl = now + 600 (not 300)"""
        table = mock_dynamodb.Table('clawdbot-approval-requests')

        # Mock the compliance, blocked, and trust checks
        with patch('compliance_checker.check_compliance', return_value=(True, None)), \
             patch('commands.is_blocked', return_value=False), \
             patch('trust.is_trust_excluded', return_value=False):

            start_time = int(time.time())
            result = grant_module.create_grant_request(
                commands=['aws ec2 describe-instances'],
                reason='test 600s timeout',
                source='test-source',
                account_id='111111111111',
                ttl_minutes=30,
                allow_repeat=False,
                approval_timeout=600
            )

            # Response should include approval_timeout
            assert result['approval_timeout'] == 600
            assert result['expires_in'] == 600

            # DDB item should have correct ttl
            grant_id = result['grant_id']
            item = table.get_item(Key={'request_id': grant_id})['Item']
            assert item['approval_timeout'] == 600
            assert item['ttl'] >= start_time + 600
            assert item['ttl'] <= start_time + 600 + 5  # Allow 5s tolerance

    def test_approval_timeout_clamped_to_max_900(self, grant_module, mock_dynamodb):
        """Test 2: approval_timeout=1000 → clamped to 900"""
        table = mock_dynamodb.Table('clawdbot-approval-requests')

        with patch('compliance_checker.check_compliance', return_value=(True, None)), \
             patch('commands.is_blocked', return_value=False), \
             patch('trust.is_trust_excluded', return_value=False):

            start_time = int(time.time())
            result = grant_module.create_grant_request(
                commands=['aws ec2 describe-instances'],
                reason='test max clamp',
                source='test-source',
                account_id='111111111111',
                approval_timeout=1000  # Over max
            )

            # Should be clamped to 900
            assert result['approval_timeout'] == 900
            assert result['expires_in'] == 900

            grant_id = result['grant_id']
            item = table.get_item(Key={'request_id': grant_id})['Item']
            assert item['approval_timeout'] == 900
            assert item['ttl'] >= start_time + 900
            assert item['ttl'] <= start_time + 900 + 5

    def test_approval_timeout_default_300(self, grant_module, mock_dynamodb):
        """Test 3: approval_timeout=None → default 300"""
        table = mock_dynamodb.Table('clawdbot-approval-requests')

        with patch('compliance_checker.check_compliance', return_value=(True, None)), \
             patch('commands.is_blocked', return_value=False), \
             patch('trust.is_trust_excluded', return_value=False):

            start_time = int(time.time())
            result = grant_module.create_grant_request(
                commands=['aws ec2 describe-instances'],
                reason='test default timeout',
                source='test-source',
                account_id='111111111111',
                approval_timeout=None  # Use default
            )

            # Should default to 300
            assert result['approval_timeout'] == 300
            assert result['expires_in'] == 300

            grant_id = result['grant_id']
            item = table.get_item(Key={'request_id': grant_id})['Item']
            assert item['approval_timeout'] == 300
            assert item['ttl'] >= start_time + 300
            assert item['ttl'] <= start_time + 300 + 5

    def test_approval_timeout_clamped_to_min_60(self, grant_module, mock_dynamodb):
        """Test 4: approval_timeout=30 → clamped to 60"""
        table = mock_dynamodb.Table('clawdbot-approval-requests')

        with patch('compliance_checker.check_compliance', return_value=(True, None)), \
             patch('commands.is_blocked', return_value=False), \
             patch('trust.is_trust_excluded', return_value=False):

            start_time = int(time.time())
            result = grant_module.create_grant_request(
                commands=['aws ec2 describe-instances'],
                reason='test min clamp',
                source='test-source',
                account_id='111111111111',
                approval_timeout=30  # Below min
            )

            # Should be clamped to 60
            assert result['approval_timeout'] == 60
            assert result['expires_in'] == 60

            grant_id = result['grant_id']
            item = table.get_item(Key={'request_id': grant_id})['Item']
            assert item['approval_timeout'] == 60
            assert item['ttl'] >= start_time + 60
            assert item['ttl'] <= start_time + 60 + 5

    def test_response_contains_approval_timeout_field(self, grant_module):
        """Test 5: response contains 'approval_timeout' field"""
        with patch('compliance_checker.check_compliance', return_value=(True, None)), \
             patch('commands.is_blocked', return_value=False), \
             patch('trust.is_trust_excluded', return_value=False):

            result = grant_module.create_grant_request(
                commands=['aws ec2 describe-instances'],
                reason='test response format',
                source='test-source',
                account_id='111111111111',
                approval_timeout=450
            )

            # Response must include approval_timeout
            assert 'approval_timeout' in result
            assert result['approval_timeout'] == 450

            # Backward compatibility: expires_in should also be present
            assert 'expires_in' in result
            assert result['expires_in'] == 450
