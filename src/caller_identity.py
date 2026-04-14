"""
Bouncer - Caller Identity
Sprint 81: Per-bot authentication and authorization (ABAC)

Identifies callers by secret and returns bot configuration including execution role.
"""

import json
import os
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_registry():
    """Load bot registry from environment variable.

    Format:
    {
      "bots": {
        "private-bot": {
          "secret": "...",
          "source": "Private Bot",
          "role_arn": null
        },
        "public-bot": {
          "secret": "...",
          "source": "Public Bot",
          "role_arn": "arn:aws:iam::190825685292:role/bouncer-public-bot-role"
        }
      }
    }

    Falls back to legacy REQUEST_SECRET for backward compatibility.
    """
    registry_json = os.environ.get('BOT_REGISTRY_JSON', '')
    if registry_json:
        try:
            return json.loads(registry_json)
        except json.JSONDecodeError:
            logger.error("Invalid BOT_REGISTRY_JSON")

    # Fallback: legacy single secret
    return {
        "bots": {
            "legacy": {
                "secret": os.environ.get('REQUEST_SECRET', ''),
                "source": "Legacy",
                "role_arn": None
            }
        }
    }


def identify_caller(secret: str) -> dict | None:
    """Identify caller by secret.

    Args:
        secret: The x-approval-secret header value from the request.

    Returns:
        Bot config dict with bot_id, secret, source, role_arn if matched.
        None if secret does not match any registered bot.
    """
    if not secret:
        return None

    registry = _load_registry()
    for bot_id, config in registry['bots'].items():
        if config['secret'] == secret:
            return {"bot_id": bot_id, **config}

    return None
