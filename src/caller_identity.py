"""Per-bot caller identification.

Identifies callers by their secret and maps them to bot configurations.
Private bot uses REQUEST_SECRET env var (backward compat).
Additional bots use Secrets Manager entries.
"""
import json
import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)

_registry_cache = None
_registry_loaded_at = 0
_CACHE_TTL = 300  # 5 minutes

# Public bot config from Secrets Manager
_PUBLIC_BOT_SECRET_NAME = 'bouncer/public-bot-secret'
_PUBLIC_BOT_ROLE_ARN = os.environ.get(
    'PUBLIC_BOT_ROLE_ARN',
    'arn:aws:iam::190825685292:role/bouncer-public-bot-role'
)


def _load_registry():
    """Build bot registry from env vars + Secrets Manager."""
    global _registry_cache, _registry_loaded_at

    now = time.time()
    if _registry_cache and (now - _registry_loaded_at) < _CACHE_TTL:
        return _registry_cache

    bots = {}

    # Private bot: uses existing REQUEST_SECRET (always available)
    request_secret = os.environ.get('REQUEST_SECRET', '')
    if request_secret:
        bots['private-bot'] = {
            'secret': request_secret,
            'source': 'Private Bot',
            'role_arn': None,
        }

    # Public bot: load from Secrets Manager
    try:
        sm = boto3.client('secretsmanager', region_name='us-east-1')
        resp = sm.get_secret_value(SecretId=_PUBLIC_BOT_SECRET_NAME)
        secret_data = json.loads(resp['SecretString'])
        bots['public-bot'] = {
            'secret': secret_data.get('secret', ''),
            'source': secret_data.get('source', 'Public Bot'),
            'role_arn': secret_data.get('role_arn', _PUBLIC_BOT_ROLE_ARN),
        }
    except Exception:  # noqa: BLE001
        # Secret not found or access denied — public bot not configured
        logger.debug("Public bot secret not loaded (not configured or access denied)")

    # Legacy fallback: if REQUEST_SECRET matches but no bot entry,
    # treat as legacy caller
    if not bots:
        bots['legacy'] = {
            'secret': request_secret,
            'source': 'Legacy',
            'role_arn': None,
        }

    _registry_cache = {'bots': bots}
    _registry_loaded_at = now
    return _registry_cache


def identify_caller(secret: str) -> dict | None:
    """Identify caller by secret. Returns bot config dict or None."""
    if not secret:
        return None
    registry = _load_registry()
    for bot_id, config in registry['bots'].items():
        if config['secret'] == secret:
            return {"bot_id": bot_id, **config}
    return None
