"""
Bouncer - MCP whoami tool
Returns Bouncer version, configuration, and feature flags for agent self-check.
"""

import json
import os
from utils import mcp_result
from constants import VERSION, DEFAULT_ACCOUNT_ID, DEFAULT_REGION


def mcp_tool_whoami(req_id: str, arguments: dict) -> dict:
    """MCP tool handler: return Bouncer configuration and version info.

    Returns version, default account, region, and feature flags.
    Useful for agent debugging and environment verification.
    """
    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'version': VERSION,
                'default_account_id': DEFAULT_ACCOUNT_ID,
                'region': DEFAULT_REGION,
                'features': {
                    'grant_enabled': os.environ.get('GRANT_SESSION_ENABLED', 'true').lower() == 'true',
                    'trust_enabled': os.environ.get('TRUST_SESSION_ENABLED', 'true').lower() == 'true',
                    'trust_rate_limit_enabled': os.environ.get('TRUST_RATE_LIMIT_ENABLED', 'true').lower() == 'true',
                    'rate_limit_enabled': os.environ.get('RATE_LIMIT_ENABLED', 'true').lower() == 'true',
                    'ip_binding_mode': os.environ.get('BOUNCER_IP_BINDING_MODE', 'strict'),
                },
                'uptime_hint': 'Lambda is stateless; cold start = new uptime',
            }, ensure_ascii=False)
        }],
    })
