"""Per-bot caller identification.

Identifies callers by their secret and maps them to bot configurations.
Both private-bot and public-bot secrets are stored in Secrets Manager.
REQUEST_SECRET env var is kept as fallback for backward compatibility.
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

_SM_REGION = 'us-east-1'
_BOT_SECRETS = {
    'private-bot': {
        'sm_name': 'bouncer/private-bot-secret',
        'default_source': 'Private Bot',
        'default_role_arn': None,
    },
    'public-bot': {
        'sm_name': 'bouncer/public-bot-secret',
        'default_source': 'Public Bot',
        'default_role_arn': os.environ.get(
            'PUBLIC_BOT_ROLE_ARN',
            'arn:aws:iam::190825685292:role/bouncer-public-bot-role',
        ),
    },
}


def _load_registry():
    """Build bot registry from Secrets Manager + env var fallback."""
    global _registry_cache, _registry_loaded_at

    now = time.time()
    if _registry_cache and (now - _registry_loaded_at) < _CACHE_TTL:
        return _registry_cache

    bots = {}
    sm = boto3.client('secretsmanager', region_name=_SM_REGION)

    for bot_id, config in _BOT_SECRETS.items():
        try:
            resp = sm.get_secret_value(SecretId=config['sm_name'])
            secret_data = json.loads(resp['SecretString'])
            bots[bot_id] = {
                'secret': secret_data['secret'],
                'source': secret_data.get('source', config['default_source']),
                'role_arn': secret_data.get('role_arn', config['default_role_arn']),
            }
        except Exception:  # noqa: BLE001
            logger.debug("Secret %s not loaded", config['sm_name'])

    # Fallback: if private-bot not loaded from SM, use REQUEST_SECRET env var
    if 'private-bot' not in bots:
        request_secret = os.environ.get('REQUEST_SECRET', '')
        if request_secret:
            bots['private-bot'] = {
                'secret': request_secret,
                'source': 'Private Bot',
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
