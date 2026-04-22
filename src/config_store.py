"""Dynamic config store backed by DynamoDB.

Table: bouncer-config (created via template.yaml)
PK: config_key (String)
Attributes: value (any type), updatedAt (Number), updatedBy (String)

Cache: in-memory dict, TTL 300 seconds (5 min).
Thread-safe (Lambda warm start shares memory).
"""

import os
import time
import threading
import boto3
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 300  # seconds

TABLE_NAME = os.environ.get('CONFIG_TABLE', 'bouncer-config')

# Lazy-init DynamoDB client
_ddb_table = None


def _get_table():
    """Lazy-initialize DynamoDB table resource."""
    global _ddb_table
    if _ddb_table is None:
        dynamodb = boto3.resource('dynamodb')
        _ddb_table = dynamodb.Table(TABLE_NAME)
    return _ddb_table


def get_config(key: str, default=None):
    """Get config value. Returns default if not found. Uses memory cache with TTL.

    Args:
        key: Config key to retrieve
        default: Default value if key not found

    Returns:
        Config value (can be any JSON-serializable type), or default if not found
    """
    with _cache_lock:
        cached_entry = _cache.get(key)
        if cached_entry:
            value, timestamp = cached_entry
            if time.time() - timestamp < _CACHE_TTL:
                return value
            # Cache expired
            del _cache[key]

    # Cache miss or expired — fetch from DynamoDB
    try:
        table = _get_table()
        response = table.get_item(Key={'config_key': key})
        item = response.get('Item')
        if not item:
            logger.debug("Config key not found: %s", key, extra={"src_module": "config_store", "operation": "get_config", "key": key})
            return default

        value = item['value']

        # Store in cache
        with _cache_lock:
            _cache[key] = (value, time.time())

        return value
    except Exception as e:
        logger.exception("Failed to get config key %s: %s", key, e, extra={"src_module": "config_store", "operation": "get_config", "key": key, "error": str(e)})
        return default


def set_config(key: str, value, updated_by: str = "system"):
    """Set config value. Invalidates cache for this key.

    Args:
        key: Config key to set
        value: Value to store (must be JSON-serializable)
        updated_by: Identity of who updated this config
    """
    try:
        table = _get_table()
        table.put_item(Item={
            'config_key': key,
            'value': value,
            'updatedAt': int(time.time()),
            'updatedBy': updated_by,
        })

        # Invalidate cache
        with _cache_lock:
            if key in _cache:
                del _cache[key]

        logger.info("Config key updated: %s", key, extra={"src_module": "config_store", "operation": "set_config", "key": key, "updated_by": updated_by})
    except Exception as e:
        logger.exception("Failed to set config key %s: %s", key, e, extra={"src_module": "config_store", "operation": "set_config", "key": key, "error": str(e)})
        raise


def list_configs():
    """List all config keys + values.

    Returns:
        List of dict with keys: config_key, value, updatedAt, updatedBy
    """
    try:
        table = _get_table()
        response = table.scan()
        items = response.get('Items', [])

        # Continue scanning if result is paginated
        while 'LastEvaluatedKey' in response:
            response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
            items.extend(response.get('Items', []))

        return items
    except Exception as e:
        logger.exception("Failed to list configs: %s", e, extra={"src_module": "config_store", "operation": "list_configs", "error": str(e)})
        return []


def _is_silent_source(source: str) -> bool:
    """Check if source matches any pattern in silent_sources config.

    Patterns support trailing * wildcard (prefix match).
    Returns False if silent_sources not configured or empty.

    Args:
        source: Request source identifier

    Returns:
        True if source matches a silent pattern, False otherwise
    """
    patterns = get_config('silent_sources', [])
    if not patterns or not source:
        return False

    for pattern in patterns:
        if pattern.endswith('*'):
            # Prefix match
            if source.startswith(pattern[:-1]):
                return True
        elif source == pattern:
            # Exact match
            return True

    return False
