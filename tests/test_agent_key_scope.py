"""Tests for agent key scope enforcement.

Sprint 96: Agent key scope — allowed_commands, allowed_accounts, max_risk_score
"""

import time
import os
import boto3
from moto import mock_aws

from agent_keys import (
    create_agent_key, identify_agent, check_scope_authorization
)


def _setup_config_table():
    """Setup mock DynamoDB config table."""
    os.environ['CONFIG_TABLE'] = 'bouncer-config'
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.create_table(
        TableName='bouncer-config',
        KeySchema=[
            {'AttributeName': 'config_key', 'KeyType': 'HASH'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'config_key', 'AttributeType': 'S'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )
    return table


@mock_aws
def test_create_key_with_scope():
    """create key with scope + allowed_commands → identify returns scope fields"""
    _setup_config_table()

    result = create_agent_key(
        agent_id='test-agent',
        agent_name='Test Agent',
        created_by='test',
        scope='debug',
        allowed_commands=['ec2 describe-*', 's3 ls*'],
        allowed_accounts=['111111111111'],
        max_risk_score=30,
    )

    assert result['scope'] == 'debug'
    assert result['allowed_commands'] == ['ec2 describe-*', 's3 ls*']
    assert result['allowed_accounts'] == ['111111111111']
    assert result['max_risk_score'] == 30

    # identify should return scope fields
    agent = identify_agent(result['key'])
    assert agent is not None
    assert agent['agent_id'] == 'test-agent'
    assert agent['scope'] == 'debug'
    assert agent['allowed_commands'] == ['ec2 describe-*', 's3 ls*']
    assert agent['allowed_accounts'] == ['111111111111']
    assert agent['max_risk_score'] == 30


@mock_aws
def test_scope_check_allowed_command():
    """check_scope_authorization: allowed command → None"""
    agent = {
        'agent_id': 'test-agent',
        'allowed_commands': ['ec2 describe-*', 's3 ls'],
    }

    # Exact match
    result = check_scope_authorization(agent, 's3 ls', '111111111111')
    assert result is None

    # Prefix match (wildcard)
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111')
    assert result is None

    result = check_scope_authorization(agent, 'ec2 describe-vpcs', '111111111111')
    assert result is None


@mock_aws
def test_scope_check_disallowed_command():
    """check_scope_authorization: disallowed command → error string"""
    agent = {
        'agent_id': 'test-agent',
        'allowed_commands': ['ec2 describe-*', 's3 ls'],
    }

    result = check_scope_authorization(agent, 's3 rm', '111111111111')
    assert result is not None
    assert 's3 rm' in result
    assert 'not in allowed_commands' in result


@mock_aws
def test_scope_check_wildcard_matching():
    """check_scope_authorization: wildcard matching (ec2 describe-* matches ec2 describe-instances)"""
    agent = {
        'agent_id': 'test-agent',
        'allowed_commands': ['ec2 describe-*', 's3api list-*'],
    }

    # Should match ec2 describe-*
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111')
    assert result is None

    result = check_scope_authorization(agent, 'ec2 describe-vpcs', '111111111111')
    assert result is None

    # Should match s3api list-*
    result = check_scope_authorization(agent, 's3api list-objects-v2', '111111111111')
    assert result is None

    # Should NOT match (no wildcard for ec2 run-instances)
    result = check_scope_authorization(agent, 'ec2 run-instances', '111111111111')
    assert result is not None
    assert 'not in allowed_commands' in result


@mock_aws
def test_scope_check_allowed_accounts():
    """check_scope_authorization: allowed_accounts check"""
    agent = {
        'agent_id': 'test-agent',
        'allowed_accounts': ['111111111111', '222222222222'],
    }

    # Allowed account
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111')
    assert result is None

    result = check_scope_authorization(agent, 'ec2 describe-instances', '222222222222')
    assert result is None

    # Disallowed account
    result = check_scope_authorization(agent, 'ec2 describe-instances', '333333333333')
    assert result is not None
    assert '333333333333' in result
    assert 'not in allowed_accounts' in result


@mock_aws
def test_scope_check_max_risk_score():
    """check_scope_authorization: max_risk_score check"""
    agent = {
        'agent_id': 'test-agent',
        'max_risk_score': 30,
    }

    # Risk score within limit
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111', risk_score=25)
    assert result is None

    # Risk score at limit
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111', risk_score=30)
    assert result is None

    # Risk score exceeds limit
    result = check_scope_authorization(agent, 'ec2 terminate-instances', '111111111111', risk_score=80)
    assert result is not None
    assert '80' in result
    assert 'max_risk_score' in result
    assert '30' in result


@mock_aws
def test_scope_check_no_restrictions():
    """check_scope_authorization: no restrictions (all None) → always allowed"""
    agent = {
        'agent_id': 'test-agent',
    }

    # No restrictions — all commands allowed
    result = check_scope_authorization(agent, 'ec2 terminate-instances', '111111111111', risk_score=100)
    assert result is None

    result = check_scope_authorization(agent, 's3 rm', '999999999999', risk_score=0)
    assert result is None


@mock_aws
def test_temp_key_expiry():
    """temp key: create with expires_at → identify works before expiry, returns None after"""
    _setup_config_table()

    expires_at = int(time.time()) + 2  # 2 seconds

    result = create_agent_key(
        agent_id='temp-agent',
        agent_name='Temp Agent',
        created_by='test',
        expires_at=expires_at,
    )

    # Should work immediately
    agent = identify_agent(result['key'])
    assert agent is not None
    assert agent['agent_id'] == 'temp-agent'

    # Wait for expiry
    time.sleep(3)

    # Clear cache to force re-check from DDB
    import agent_keys
    agent_keys._key_cache.clear()

    # Should return None after expiry
    agent = identify_agent(result['key'])
    assert agent is None


@mock_aws
def test_identify_agent_updates_last_used_at():
    """identify_agent updates last_used_at"""
    _setup_config_table()

    result = create_agent_key(
        agent_id='test-agent',
        agent_name='Test Agent',
        created_by='test',
    )

    time.sleep(1)

    # First identify
    agent1 = identify_agent(result['key'], caller_ip='1.2.3.4')
    assert agent1 is not None

    # Wait a bit and identify again
    time.sleep(1)
    agent2 = identify_agent(result['key'], caller_ip='5.6.7.8')
    assert agent2 is not None

    # Both should return same agent_id
    assert agent1['agent_id'] == agent2['agent_id']


@mock_aws
def test_scope_check_combined():
    """check_scope_authorization: combined checks (commands + accounts + risk)"""
    agent = {
        'agent_id': 'test-agent',
        'allowed_commands': ['ec2 describe-*'],
        'allowed_accounts': ['111111111111'],
        'max_risk_score': 50,
    }

    # All conditions met → allowed
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111', risk_score=30)
    assert result is None

    # Command not allowed
    result = check_scope_authorization(agent, 's3 ls', '111111111111', risk_score=30)
    assert result is not None
    assert 'not in allowed_commands' in result

    # Account not allowed
    result = check_scope_authorization(agent, 'ec2 describe-instances', '222222222222', risk_score=30)
    assert result is not None
    assert 'not in allowed_accounts' in result

    # Risk score too high
    result = check_scope_authorization(agent, 'ec2 describe-instances', '111111111111', risk_score=80)
    assert result is not None
    assert 'max_risk_score' in result
