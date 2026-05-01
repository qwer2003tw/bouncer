"""Tests for silent_rules module (#387)"""

import time
import pytest
from unittest.mock import MagicMock, patch
from moto import mock_aws
import boto3
import os


@pytest.fixture
def ddb_table():
    """Create a test DynamoDB table for silent rules."""
    with mock_aws():
        # Create table
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='bouncer-silent-rules-test',
            BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[
                {'AttributeName': 'rule_id', 'AttributeType': 'S'},
                {'AttributeName': 'source_action', 'AttributeType': 'S'},
            ],
            KeySchema=[
                {'AttributeName': 'rule_id', 'KeyType': 'HASH'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'source-action-index',
                    'KeySchema': [
                        {'AttributeName': 'source_action', 'KeyType': 'HASH'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                }
            ],
        )

        # Set environment variable
        os.environ['SILENT_RULES_TABLE'] = 'bouncer-silent-rules-test'

        yield table


def test_create_rule(ddb_table):
    """Test creating a silent rule."""
    from silent_rules import create_rule

    rule = create_rule(
        source='clawdbot',
        service='ec2',
        action='describe-instances',
        created_by='123456789'
    )

    assert rule['rule_id'].startswith('sr-')
    assert rule['source'] == 'clawdbot'
    assert rule['service'] == 'ec2'
    assert rule['action'] == 'describe-instances'
    assert rule['source_action'] == 'clawdbot|ec2:describe-instances'
    assert rule['created_by'] == '123456789'
    assert rule['hit_count'] == 0
    assert rule['last_triggered_at'] is None
    assert 'created_at' in rule


def test_is_silenced_active_rule(ddb_table):
    """Test checking if a command is silenced by an active rule."""
    from silent_rules import create_rule, is_silenced

    # Create a rule
    created_rule = create_rule(
        source='clawdbot',
        service='ec2',
        action='describe-instances',
        created_by='123456789'
    )

    # Check if silenced
    rule = is_silenced('clawdbot', 'ec2', 'describe-instances')

    assert rule is not None
    assert rule['rule_id'] == created_rule['rule_id']

    # Hit count should have been incremented
    # Note: In the actual implementation, hit count is updated async,
    # so we can't reliably test it here


def test_is_silenced_expired_rule(ddb_table):
    """Test that expired rules don't match."""
    from silent_rules import create_rule, is_silenced

    # Create a rule that expired 1 hour ago
    expired_time = int(time.time()) - 3600
    create_rule(
        source='clawdbot',
        service='ec2',
        action='describe-instances',
        created_by='123456789',
        expires_at=expired_time
    )

    # Check if silenced - should return None because rule is expired
    rule = is_silenced('clawdbot', 'ec2', 'describe-instances')

    assert rule is None


def test_is_silenced_no_rule(ddb_table):
    """Test checking for non-existent rule."""
    from silent_rules import is_silenced

    # Check for a rule that doesn't exist
    rule = is_silenced('clawdbot', 'ec2', 'describe-instances')

    assert rule is None


def test_revoke_rule(ddb_table):
    """Test revoking a rule."""
    from silent_rules import create_rule, revoke_rule, is_silenced

    # Create a rule
    created_rule = create_rule(
        source='clawdbot',
        service='ec2',
        action='describe-instances',
        created_by='123456789'
    )

    # Verify it exists
    rule = is_silenced('clawdbot', 'ec2', 'describe-instances')
    assert rule is not None

    # Revoke it
    success = revoke_rule(created_rule['rule_id'])
    assert success is True

    # Verify it's gone
    rule = is_silenced('clawdbot', 'ec2', 'describe-instances')
    assert rule is None


def test_list_rules(ddb_table):
    """Test listing all active rules."""
    from silent_rules import create_rule, list_rules

    # Create several rules
    create_rule('clawdbot', 'ec2', 'describe-instances', '123456789')
    create_rule('admin', 's3', 'list-buckets', '987654321')

    # Create an expired rule
    expired_time = int(time.time()) - 3600
    create_rule('old', 'lambda', 'list-functions', '111111111', expires_at=expired_time)

    # List active rules
    rules = list_rules()

    # Should return 2 active rules (not the expired one)
    assert len(rules) == 2

    sources = [r['source'] for r in rules]
    assert 'clawdbot' in sources
    assert 'admin' in sources
    assert 'old' not in sources


def test_revoke_all(ddb_table):
    """Test revoking all rules."""
    from silent_rules import create_rule, revoke_all, list_rules

    # Create several rules
    create_rule('clawdbot', 'ec2', 'describe-instances', '123456789')
    create_rule('admin', 's3', 'list-buckets', '987654321')
    create_rule('bot', 'lambda', 'list-functions', '111111111')

    # Verify they exist
    rules = list_rules()
    assert len(rules) == 3

    # Revoke all
    count = revoke_all()
    assert count == 3

    # Verify they're gone
    rules = list_rules()
    assert len(rules) == 0


def test_make_source_action_key():
    """Test source_action key generation."""
    from silent_rules import make_source_action_key

    key = make_source_action_key('clawdbot', 'ec2', 'describe-instances')
    assert key == 'clawdbot|ec2:describe-instances'

    key2 = make_source_action_key('admin', 's3', 'list-buckets')
    assert key2 == 'admin|s3:list-buckets'


def test_create_rule_with_expiry(ddb_table):
    """Test creating a rule with expiry."""
    from silent_rules import create_rule, is_silenced

    # Create a rule that expires in 1 hour
    future_time = int(time.time()) + 3600
    rule = create_rule(
        source='clawdbot',
        service='ec2',
        action='describe-instances',
        created_by='123456789',
        expires_at=future_time
    )

    assert rule['expires_at'] == future_time

    # Should still be active
    active_rule = is_silenced('clawdbot', 'ec2', 'describe-instances')
    assert active_rule is not None
