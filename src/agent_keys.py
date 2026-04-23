"""Per-agent API key management.

Provides server-side agent identity verification to prevent client-side
source spoofing. Keys are hashed (SHA-256) and stored in ConfigTable
(bouncer-config) with agent metadata.

Key format: bncr_{agent_short}_{32_random_chars}
Example: bncr_priv_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6

Functions:
  identify_agent(key: str) -> Optional[dict]
    Hash key → lookup DDB → return {agent_id, agent_name} or None
    Check not revoked, not expired
    Cache result in memory (TTL 60s) to avoid DDB lookup every request

  create_agent_key(agent_id: str, agent_name: str, created_by: str, expires_at: int = None) -> dict
    Generate key: bncr_{agent_id_short}_{32_random}
    Store hash + metadata in ConfigTable
    Return {key: "bncr_...", agent_id, agent_name, key_prefix, created_at}
    IMPORTANT: key is returned ONCE on creation, never stored in plaintext

  revoke_agent_key(key_prefix: str, agent_id: str) -> bool
    Find key by prefix + agent_id → set revoked=True, revoked_at=now
    Invalidate cache

  list_agent_keys(agent_id: str = None) -> list
    Scan ConfigTable for agent_key# prefix
    Return list of {agent_id, agent_name, key_prefix, created_at, expires_at, revoked}
    Never return key hash or full key

  rotate_agent_key(agent_id: str, created_by: str) -> dict
    Create new key for same agent_id
    Old keys stay active (caller revokes manually when ready)
    Return new key info
"""

import hashlib
import secrets
import time
import threading
from typing import Optional

from aws_lambda_powertools import Logger
from config_store import _get_table

logger = Logger(service="bouncer")

# In-memory cache for key lookups (60s TTL)
_key_cache = {}
_cache_lock = threading.Lock()
_KEY_CACHE_TTL = 60  # seconds


def _hash_key(key: str) -> str:
    """Hash API key using SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def _generate_key(agent_id: str) -> tuple[str, str]:
    """Generate a new API key.

    Args:
        agent_id: Agent identifier (e.g. "private-bot")

    Returns:
        (full_key, key_prefix) tuple
        full_key format: bncr_{agent_short}_{32_random_chars}
        key_prefix format: bncr_{agent_short}_{first_8_chars}
    """
    # Extract short agent identifier (max 8 chars, alphanumeric only)
    agent_short = ''.join(c for c in agent_id if c.isalnum())[:8]
    random_chars = secrets.token_hex(16)  # 32 hex chars
    full_key = f"bncr_{agent_short}_{random_chars}"
    key_prefix = f"bncr_{agent_short}_{random_chars[:8]}"
    return full_key, key_prefix


def check_scope_authorization(agent: dict, command: str, account_id: str, risk_score: int = 0) -> Optional[str]:
    """Check if agent's scope allows this command.

    Args:
        agent: Agent dict from identify_agent() (includes scope fields)
        command: Command to check (e.g. "ec2 describe-instances", "s3:list_objects_v2")
        account_id: AWS account ID
        risk_score: Command risk score (0-100)

    Returns:
        None if allowed, error message string if denied.

    Logic:
        - allowed_commands: if set, command must match at least one pattern (trailing * = prefix match)
        - allowed_accounts: if set, account_id must be in list
        - max_risk_score: if set, risk_score must be <= max_risk_score
    """
    # Check allowed_commands
    allowed_commands = agent.get('allowed_commands')
    if allowed_commands:
        matched = False
        for pattern in allowed_commands:
            if pattern.endswith('*'):
                # Prefix match (wildcard)
                prefix = pattern[:-1]
                if command.startswith(prefix):
                    matched = True
                    break
            else:
                # Exact match
                if command == pattern:
                    matched = True
                    break
        if not matched:
            return f"Command '{command[:100]}' not in allowed_commands list"

    # Check allowed_accounts
    allowed_accounts = agent.get('allowed_accounts')
    if allowed_accounts and account_id not in allowed_accounts:
        return f"Account '{account_id}' not in allowed_accounts list"

    # Check max_risk_score
    max_risk_score = agent.get('max_risk_score')
    if max_risk_score is not None and risk_score > max_risk_score:
        return f"Risk score {risk_score} exceeds max_risk_score {max_risk_score}"

    return None


def identify_agent(key: str, caller_ip: str = None) -> Optional[dict]:
    """Identify agent by API key.

    Args:
        key: Full API key (bncr_...)
        caller_ip: Optional caller IP address (for last_ip tracking)

    Returns:
        {agent_id, agent_name, scope, allowed_commands, allowed_accounts, max_risk_score} if valid,
        None if invalid/revoked/expired
    """
    if not key or not key.startswith('bncr_'):
        return None

    key_hash = _hash_key(key)

    # Check cache
    with _cache_lock:
        cached_entry = _key_cache.get(key_hash)
        if cached_entry:
            value, timestamp = cached_entry
            if time.time() - timestamp < _KEY_CACHE_TTL:
                return value
            # Cache expired
            del _key_cache[key_hash]

    # Lookup in DynamoDB
    try:
        table = _get_table()
        pk = f"agent_key#{key_hash}"
        response = table.get_item(Key={'config_key': pk})
        item = response.get('Item')

        if not item:
            logger.debug("Agent key not found", extra={"src_module": "agent_keys", "operation": "identify_agent"})
            return None

        # Check revoked
        if item.get('revoked', False):
            logger.debug("Agent key revoked", extra={"src_module": "agent_keys", "operation": "identify_agent", "agent_id": item.get('agent_id')})
            return None

        # Check expired
        expires_at = item.get('expires_at')
        if expires_at and int(expires_at) < int(time.time()):
            logger.debug("Agent key expired", extra={"src_module": "agent_keys", "operation": "identify_agent", "agent_id": item.get('agent_id')})
            return None

        # Update last_used_at and last_ip
        now = int(time.time())
        update_expr = 'SET last_used_at = :now'
        expr_values = {':now': now}
        if caller_ip:
            update_expr += ', last_ip = :ip'
            expr_values[':ip'] = caller_ip
        try:
            table.update_item(
                Key={'config_key': pk},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values
            )
        except Exception as update_err:
            logger.warning("Failed to update last_used_at: %s", update_err, extra={"src_module": "agent_keys", "operation": "update_last_used"})

        result = {
            'agent_id': item['agent_id'],
            'agent_name': item['agent_name'],
        }

        # Include scope fields if present
        if 'scope' in item:
            result['scope'] = item['scope']
        if 'allowed_commands' in item:
            result['allowed_commands'] = item['allowed_commands']
        if 'allowed_accounts' in item:
            result['allowed_accounts'] = item['allowed_accounts']
        if 'max_risk_score' in item:
            result['max_risk_score'] = int(item['max_risk_score'])

        # Store in cache
        with _cache_lock:
            _key_cache[key_hash] = (result, time.time())

        return result

    except Exception as e:
        logger.exception("Failed to identify agent: %s", e, extra={"src_module": "agent_keys", "operation": "identify_agent", "error": str(e)})
        return None


def create_agent_key(
    agent_id: str,
    agent_name: str,
    created_by: str,
    expires_at: Optional[int] = None,
    scope: Optional[str] = None,
    allowed_commands: Optional[list] = None,
    allowed_accounts: Optional[list] = None,
    max_risk_score: Optional[int] = None,
) -> dict:
    """Create a new agent API key.

    Args:
        agent_id: Agent identifier (e.g. "private-bot")
        agent_name: Human-readable agent name (e.g. "Steven's Private Bot")
        created_by: Who created this key (e.g. "admin", "mcp")
        expires_at: Optional Unix timestamp for expiry (None = never expires)
        scope: Optional scope identifier (e.g. "daily-inspection", "interactive", "debug")
        allowed_commands: Optional command whitelist (e.g. ["ec2 describe-*", "s3 ls*"]) — wildcard supported
        allowed_accounts: Optional account whitelist (e.g. ["190825685292", "992382394211"])
        max_risk_score: Optional max risk score (e.g. 30) — reject if risk > this

    Returns:
        {key: "bncr_...", agent_id, agent_name, key_prefix, created_at}
        IMPORTANT: key is returned ONCE, never stored in plaintext
    """
    full_key, key_prefix = _generate_key(agent_id)
    key_hash = _hash_key(full_key)
    now = int(time.time())

    try:
        table = _get_table()
        pk = f"agent_key#{key_hash}"

        item = {
            'config_key': pk,
            'agent_id': agent_id,
            'agent_name': agent_name,
            'key_prefix': key_prefix,
            'created_at': now,
            'revoked': False,
            'created_by': created_by,
        }

        if expires_at:
            item['expires_at'] = expires_at

        # Store scope fields
        if scope:
            item['scope'] = scope
        if allowed_commands:
            item['allowed_commands'] = allowed_commands
        if allowed_accounts:
            item['allowed_accounts'] = allowed_accounts
        if max_risk_score is not None:
            item['max_risk_score'] = max_risk_score

        table.put_item(Item=item)

        logger.info("Agent key created", extra={
            "src_module": "agent_keys",
            "operation": "create_agent_key",
            "agent_id": agent_id,
            "key_prefix": key_prefix,
            "created_by": created_by,
            "scope": scope,
        })

        return {
            'key': full_key,
            'agent_id': agent_id,
            'agent_name': agent_name,
            'key_prefix': key_prefix,
            'created_at': now,
            'expires_at': expires_at,
            'scope': scope,
            'allowed_commands': allowed_commands,
            'allowed_accounts': allowed_accounts,
            'max_risk_score': max_risk_score,
        }

    except Exception as e:
        logger.exception("Failed to create agent key: %s", e, extra={"src_module": "agent_keys", "operation": "create_agent_key", "agent_id": agent_id, "error": str(e)})
        raise


def revoke_agent_key(key_prefix: str, agent_id: str) -> bool:
    """Revoke an agent API key by prefix.

    Args:
        key_prefix: Key prefix (bncr_{agent}_{first_8})
        agent_id: Agent identifier (must match)

    Returns:
        True if revoked, False if not found or error
    """
    try:
        table = _get_table()

        # Scan for key with matching prefix and agent_id
        response = table.scan(
            FilterExpression='begins_with(config_key, :prefix) AND agent_id = :aid AND attribute_exists(key_prefix)',
            ExpressionAttributeValues={
                ':prefix': 'agent_key#',
                ':aid': agent_id,
            }
        )

        items = [item for item in response.get('Items', []) if item.get('key_prefix') == key_prefix]

        if not items:
            logger.warning("Agent key not found for revocation", extra={
                "src_module": "agent_keys",
                "operation": "revoke_agent_key",
                "key_prefix": key_prefix,
                "agent_id": agent_id,
            })
            return False

        item = items[0]
        pk = item['config_key']
        now = int(time.time())

        table.update_item(
            Key={'config_key': pk},
            UpdateExpression='SET revoked = :r, revoked_at = :ra',
            ExpressionAttributeValues={
                ':r': True,
                ':ra': now,
            }
        )

        # Invalidate cache (we don't have the full key, so clear all cache)
        with _cache_lock:
            _key_cache.clear()

        logger.info("Agent key revoked", extra={
            "src_module": "agent_keys",
            "operation": "revoke_agent_key",
            "agent_id": agent_id,
            "key_prefix": key_prefix,
        })

        return True

    except Exception as e:
        logger.exception("Failed to revoke agent key: %s", e, extra={"src_module": "agent_keys", "operation": "revoke_agent_key", "key_prefix": key_prefix, "error": str(e)})
        return False


def list_agent_keys(agent_id: Optional[str] = None) -> list:
    """List all agent API keys (never returns full key or hash).

    Args:
        agent_id: Optional agent_id filter (None = all agents)

    Returns:
        List of {agent_id, agent_name, key_prefix, created_at, expires_at, revoked}
    """
    try:
        table = _get_table()

        if agent_id:
            response = table.scan(
                FilterExpression='begins_with(config_key, :prefix) AND agent_id = :aid AND attribute_exists(key_prefix)',
                ExpressionAttributeValues={
                    ':prefix': 'agent_key#',
                    ':aid': agent_id,
                }
            )
        else:
            response = table.scan(
                FilterExpression='begins_with(config_key, :prefix) AND attribute_exists(key_prefix)',
                ExpressionAttributeValues={
                    ':prefix': 'agent_key#',
                }
            )

        items = response.get('Items', [])

        # Format output (never expose key hash)
        result = []
        for item in items:
            entry = {
                'agent_id': item.get('agent_id', ''),
                'agent_name': item.get('agent_name', ''),
                'key_prefix': item.get('key_prefix', ''),
                'created_at': int(item.get('created_at', 0)),
                'revoked': item.get('revoked', False),
            }
            if 'expires_at' in item and item['expires_at']:
                entry['expires_at'] = int(item['expires_at'])
            if 'revoked_at' in item and item['revoked_at']:
                entry['revoked_at'] = int(item['revoked_at'])
            if 'created_by' in item:
                entry['created_by'] = item['created_by']
            # Include scope fields
            if 'scope' in item:
                entry['scope'] = item['scope']
            if 'allowed_commands' in item:
                entry['allowed_commands'] = item['allowed_commands']
            if 'allowed_accounts' in item:
                entry['allowed_accounts'] = item['allowed_accounts']
            if 'max_risk_score' in item:
                entry['max_risk_score'] = int(item['max_risk_score'])
            if 'last_used_at' in item:
                entry['last_used_at'] = int(item['last_used_at'])
            if 'last_ip' in item:
                entry['last_ip'] = item['last_ip']
            result.append(entry)

        # Sort by created_at descending
        result.sort(key=lambda x: x['created_at'], reverse=True)

        return result

    except Exception as e:
        logger.exception("Failed to list agent keys: %s", e, extra={"src_module": "agent_keys", "operation": "list_agent_keys", "error": str(e)})
        return []


def rotate_agent_key(agent_id: str, created_by: str) -> dict:
    """Create a new key for an existing agent.

    Old keys stay active (caller revokes manually when ready).

    Args:
        agent_id: Agent identifier
        created_by: Who is rotating this key

    Returns:
        New key info (same format as create_agent_key)
    """
    # Get agent_name from existing keys
    existing_keys = list_agent_keys(agent_id)
    if not existing_keys:
        raise ValueError(f"No existing keys found for agent_id: {agent_id}")

    agent_name = existing_keys[0]['agent_name']

    # Create new key (no expiry by default)
    return create_agent_key(agent_id, agent_name, created_by)
