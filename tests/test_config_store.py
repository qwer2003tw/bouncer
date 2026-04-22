"""Tests for config_store module (#380)."""

import time
import threading
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
def test_get_config_not_found(config_table, monkeypatch):
    """Test get_config returns default when key not found."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import get_config

    result = get_config('nonexistent', default='fallback')
    assert result == 'fallback'


@mock_aws
def test_set_and_get_config(config_table, monkeypatch):
    """Test set_config and get_config round-trip."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, get_config

    set_config('test_key', 'test_value', updated_by='pytest')
    result = get_config('test_key')
    assert result == 'test_value'


@mock_aws
def test_set_config_complex_value(config_table, monkeypatch):
    """Test set_config with list/dict values."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, get_config

    test_list = ['pattern1*', 'pattern2', 'pattern3*']
    set_config('silent_sources', test_list, updated_by='pytest')
    result = get_config('silent_sources')
    assert result == test_list


@mock_aws
def test_list_configs(config_table, monkeypatch):
    """Test list_configs returns all config entries."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, list_configs

    set_config('key1', 'value1')
    set_config('key2', 'value2')
    set_config('key3', ['a', 'b', 'c'])

    configs = list_configs()
    assert len(configs) == 3

    keys = {item['config_key'] for item in configs}
    assert keys == {'key1', 'key2', 'key3'}


@mock_aws
def test_cache_ttl(config_table, monkeypatch):
    """Test cache expires after TTL."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None
    config_store._CACHE_TTL = 1  # 1 second for test

    from src.config_store import set_config, get_config

    set_config('ttl_test', 'initial_value')

    # First get — cache miss, fetches from DDB
    result1 = get_config('ttl_test')
    assert result1 == 'initial_value'

    # Update DDB directly (bypass set_config to avoid cache invalidation)
    config_table.put_item(Item={
        'config_key': 'ttl_test',
        'value': 'updated_value',
        'updatedAt': int(time.time()),
        'updatedBy': 'direct',
    })

    # Second get — cache hit, returns stale value
    result2 = get_config('ttl_test')
    assert result2 == 'initial_value'

    # Wait for cache to expire
    time.sleep(1.5)

    # Third get — cache expired, fetches new value from DDB
    result3 = get_config('ttl_test')
    assert result3 == 'updated_value'

    # Restore TTL
    config_store._CACHE_TTL = 300


@mock_aws
def test_cache_invalidation_on_set(config_table, monkeypatch):
    """Test set_config invalidates cache."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, get_config

    set_config('invalidate_test', 'value1')

    # Get to populate cache
    result1 = get_config('invalidate_test')
    assert result1 == 'value1'

    # Update via set_config — should invalidate cache
    set_config('invalidate_test', 'value2')

    # Get should return new value immediately (not cached stale value)
    result2 = get_config('invalidate_test')
    assert result2 == 'value2'


@mock_aws
def test_thread_safety(config_table, monkeypatch):
    """Test concurrent access with threading."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, get_config

    results = []

    def worker(thread_id):
        set_config(f'thread_key_{thread_id}', f'value_{thread_id}')
        value = get_config(f'thread_key_{thread_id}')
        results.append((thread_id, value))

    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert len(results) == 5
    for thread_id, value in results:
        assert value == f'value_{thread_id}'


@mock_aws
def test_is_silent_source_exact_match(config_table, monkeypatch):
    """Test _is_silent_source with exact match."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, _is_silent_source

    set_config('silent_sources', ['Private Bot', 'Test Bot'])

    assert _is_silent_source('Private Bot') is True
    assert _is_silent_source('Test Bot') is True
    assert _is_silent_source('Public Bot') is False


@mock_aws
def test_is_silent_source_wildcard(config_table, monkeypatch):
    """Test _is_silent_source with wildcard prefix match."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, _is_silent_source

    set_config('silent_sources', ['Private Bot*', 'Test*'])

    assert _is_silent_source('Private Bot (EKS)') is True
    assert _is_silent_source('Private Bot') is True
    assert _is_silent_source('Test Agent') is True
    assert _is_silent_source('Public Bot') is False


@mock_aws
def test_is_silent_source_empty_config(config_table, monkeypatch):
    """Test _is_silent_source returns False when config not set."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import _is_silent_source

    # No silent_sources config set
    assert _is_silent_source('Private Bot') is False
    assert _is_silent_source('Any Source') is False


@mock_aws
def test_is_silent_source_empty_list(config_table, monkeypatch):
    """Test _is_silent_source returns False when config is empty list."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, _is_silent_source

    set_config('silent_sources', [])

    assert _is_silent_source('Private Bot') is False


@mock_aws
def test_is_silent_source_none_source(config_table, monkeypatch):
    """Test _is_silent_source handles None source gracefully."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')

    # Clear module-level cache
    from src import config_store
    config_store._cache.clear()
    config_store._ddb_table = None

    from src.config_store import set_config, _is_silent_source

    set_config('silent_sources', ['Private Bot*'])

    assert _is_silent_source(None) is False
    assert _is_silent_source('') is False
