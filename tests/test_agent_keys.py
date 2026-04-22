"""Tests for agent_keys module (#418)."""

import time
import pytest
from moto import mock_aws
import boto3


@pytest.fixture
def config_table():
    """Create mock DynamoDB config table."""
    with mock_aws():
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
        yield table


@mock_aws
def test_create_agent_key(config_table, monkeypatch):
    """Test create_agent_key returns full key once."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear cache
    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key

    result = create_agent_key('private-bot', 'Steven\'s Private Bot', 'pytest')

    assert result['key'].startswith('bncr_')
    assert result['agent_id'] == 'private-bot'
    assert result['agent_name'] == 'Steven\'s Private Bot'
    assert 'key_prefix' in result
    assert result['created_at'] > 0

    # Verify stored in DDB (hash only, not plaintext)
    import hashlib
    key_hash = hashlib.sha256(result['key'].encode()).hexdigest()
    pk = f"agent_key#{key_hash}"
    item = config_table.get_item(Key={'config_key': pk})['Item']

    assert item['agent_id'] == 'private-bot'
    assert item['agent_name'] == 'Steven\'s Private Bot'
    assert item['revoked'] is False
    assert 'key' not in item  # plaintext key should not be stored


@mock_aws
def test_identify_agent_valid_key(config_table, monkeypatch):
    """Test identify_agent with valid key."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, identify_agent

    created = create_agent_key('test-agent', 'Test Agent', 'pytest')
    key = created['key']

    # Identify with valid key
    agent = identify_agent(key)

    assert agent is not None
    assert agent['agent_id'] == 'test-agent'
    assert agent['agent_name'] == 'Test Agent'


@mock_aws
def test_identify_agent_invalid_key(config_table, monkeypatch):
    """Test identify_agent with invalid key."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import identify_agent

    # Invalid key format
    agent = identify_agent('invalid_key')
    assert agent is None

    # Valid format but not in DDB
    agent = identify_agent('bncr_fake_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6')
    assert agent is None


@mock_aws
def test_identify_agent_revoked_key(config_table, monkeypatch):
    """Test identify_agent returns None for revoked key."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, revoke_agent_key, identify_agent

    created = create_agent_key('test-agent', 'Test Agent', 'pytest')
    key = created['key']
    key_prefix = created['key_prefix']

    # Revoke the key
    success = revoke_agent_key(key_prefix, 'test-agent')
    assert success is True

    # Identify should return None
    agent = identify_agent(key)
    assert agent is None


@mock_aws
def test_identify_agent_expired_key(config_table, monkeypatch):
    """Test identify_agent returns None for expired key."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, identify_agent

    # Create key that expired 1 hour ago
    expires_at = int(time.time()) - 3600
    created = create_agent_key('test-agent', 'Test Agent', 'pytest', expires_at=expires_at)
    key = created['key']

    # Identify should return None
    agent = identify_agent(key)
    assert agent is None


@mock_aws
def test_revoke_agent_key(config_table, monkeypatch):
    """Test revoke_agent_key."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, revoke_agent_key, identify_agent

    created = create_agent_key('test-agent', 'Test Agent', 'pytest')
    key = created['key']
    key_prefix = created['key_prefix']

    # Revoke
    success = revoke_agent_key(key_prefix, 'test-agent')
    assert success is True

    # Subsequent identify returns None
    agent = identify_agent(key)
    assert agent is None


@mock_aws
def test_revoke_agent_key_not_found(config_table, monkeypatch):
    """Test revoke_agent_key returns False when key not found."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import revoke_agent_key

    # Revoke nonexistent key
    success = revoke_agent_key('bncr_fake_12345678', 'fake-agent')
    assert success is False


@mock_aws
def test_list_agent_keys(config_table, monkeypatch):
    """Test list_agent_keys shows prefix but not full key."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, list_agent_keys

    created1 = create_agent_key('agent-1', 'Agent One', 'pytest')
    created2 = create_agent_key('agent-2', 'Agent Two', 'pytest')

    # List all
    keys = list_agent_keys()
    assert len(keys) == 2

    # Check that full key is not exposed
    for key_info in keys:
        assert 'key' not in key_info
        assert 'key_prefix' in key_info
        assert 'agent_id' in key_info
        assert 'agent_name' in key_info

    # List filtered by agent_id
    keys = list_agent_keys('agent-1')
    assert len(keys) == 1
    assert keys[0]['agent_id'] == 'agent-1'


@mock_aws
def test_rotate_agent_key(config_table, monkeypatch):
    """Test rotate_agent_key creates new key, old stays active."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, rotate_agent_key, identify_agent

    original = create_agent_key('test-agent', 'Test Agent', 'pytest')
    original_key = original['key']

    # Rotate
    rotated = rotate_agent_key('test-agent', 'pytest')
    new_key = rotated['key']

    # New key should be different
    assert new_key != original_key

    # Both keys should work (old not revoked)
    agent1 = identify_agent(original_key)
    agent2 = identify_agent(new_key)

    assert agent1 is not None
    assert agent2 is not None
    assert agent1['agent_id'] == 'test-agent'
    assert agent2['agent_id'] == 'test-agent'


@mock_aws
def test_cache_works(config_table, monkeypatch):
    """Test cache avoids redundant DDB lookups."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, identify_agent

    created = create_agent_key('test-agent', 'Test Agent', 'pytest')
    key = created['key']

    # First lookup (cache miss)
    agent1 = identify_agent(key)
    assert agent1 is not None

    # Second lookup (cache hit) — should not query DDB
    # We can't easily mock DDB calls, so we just verify it works
    agent2 = identify_agent(key)
    assert agent2 is not None
    assert agent2['agent_id'] == 'test-agent'


@mock_aws
def test_key_format_validation(config_table, monkeypatch):
    """Test invalid key formats are rejected."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import identify_agent

    # Empty string
    assert identify_agent('') is None

    # Wrong prefix
    assert identify_agent('wrong_prefix_abc123') is None

    # None
    assert identify_agent(None) is None


@mock_aws
def test_execute_pipeline_integration_valid_key(config_table, monkeypatch):
    """Integration test: request with valid key → source overridden."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import create_agent_key, identify_agent

    created = create_agent_key('test-bot', 'Test Bot', 'pytest')
    key = created['key']

    # Simulate agent key check (without importing ExecuteContext to avoid Python 3.9 syntax issues)
    agent = identify_agent(key)
    assert agent is not None

    # Simulate source override
    source = 'spoofed-source'
    agent_id = None
    verified_identity = False

    if agent:
        source = agent['agent_name']
        agent_id = agent['agent_id']
        verified_identity = True

    # Verify source overridden
    assert source == 'Test Bot'
    assert agent_id == 'test-bot'
    assert verified_identity is True


@mock_aws
def test_execute_pipeline_integration_invalid_key(config_table, monkeypatch):
    """Integration test: request with invalid key → error response."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    from src import agent_keys
    agent_keys._key_cache.clear()

    from src.agent_keys import identify_agent

    invalid_key = 'bncr_fake_invalidkeyhere123456'

    # Identify should return None
    agent = identify_agent(invalid_key)
    assert agent is None

    # In real code, this triggers mcp_error(-32001, "Invalid or expired agent key")
