"""MCP tools for per-agent API key management.

Tools:
  bouncer_agent_key_create: Create a new agent API key
  bouncer_agent_key_revoke: Revoke an existing key
  bouncer_agent_key_list: List all keys (metadata only, no full key)
  bouncer_agent_key_rotate: Create new key for existing agent
"""

import json

from aws_lambda_powertools import Logger
from agent_keys import create_agent_key, revoke_agent_key, list_agent_keys, rotate_agent_key
from utils import mcp_result, mcp_error

logger = Logger(service="bouncer")


def mcp_tool_agent_key_create(req_id, arguments: dict) -> dict:
    """Create a new agent API key.

    Args:
        req_id: MCP request ID
        arguments: {
            agent_id: str (e.g. "private-bot")
            agent_name: str (e.g. "Steven's Private Bot")
            expires_in_days: int (optional, days until expiry)
            scope: str (optional, e.g. "daily-inspection", "interactive", "debug")
            allowed_commands: list (optional, e.g. ["ec2 describe-*", "s3 ls*"])
            allowed_accounts: list (optional, e.g. ["190825685292", "992382394211"])
            max_risk_score: int (optional, e.g. 30)
        }

    Returns:
        MCP result with {key, agent_id, agent_name, key_prefix, created_at, expires_at}
        IMPORTANT: key is only shown ONCE
    """
    agent_id = arguments.get('agent_id', '').strip()
    agent_name = arguments.get('agent_name', '').strip()
    expires_in_days = arguments.get('expires_in_days')
    scope = arguments.get('scope')
    allowed_commands = arguments.get('allowed_commands')
    allowed_accounts = arguments.get('allowed_accounts')
    max_risk_score = arguments.get('max_risk_score')

    if not agent_id:
        return mcp_error(req_id, -32602, "Missing required parameter: agent_id")

    if not agent_name:
        return mcp_error(req_id, -32602, "Missing required parameter: agent_name")

    # Calculate expires_at if expires_in_days is provided
    expires_at = None
    if expires_in_days:
        try:
            import time
            expires_at = int(time.time()) + (int(expires_in_days) * 86400)
        except (ValueError, TypeError):
            return mcp_error(req_id, -32602, "expires_in_days must be a valid integer")

    try:
        result = create_agent_key(
            agent_id=agent_id,
            agent_name=agent_name,
            created_by='mcp',
            expires_at=expires_at,
            scope=scope,
            allowed_commands=allowed_commands,
            allowed_accounts=allowed_accounts,
            max_risk_score=max_risk_score,
        )

        response_data = {
            'status': 'success',
            'key': result['key'],
            'agent_id': result['agent_id'],
            'agent_name': result['agent_name'],
            'key_prefix': result['key_prefix'],
            'created_at': result['created_at'],
            'expires_at': result.get('expires_at'),
            'warning': 'This key will ONLY be shown once. Store it securely.',
        }
        # Include scope fields in response
        if result.get('scope'):
            response_data['scope'] = result['scope']
        if result.get('allowed_commands'):
            response_data['allowed_commands'] = result['allowed_commands']
        if result.get('allowed_accounts'):
            response_data['allowed_accounts'] = result['allowed_accounts']
        if result.get('max_risk_score') is not None:
            response_data['max_risk_score'] = result['max_risk_score']

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps(response_data)
            }]
        })

    except Exception as e:
        logger.exception("Failed to create agent key", extra={"src_module": "mcp_agent_key", "operation": "create", "error": str(e)})
        return mcp_error(req_id, -32603, f"Failed to create agent key: {str(e)}")


def mcp_tool_agent_key_revoke(req_id, arguments: dict) -> dict:
    """Revoke an agent API key.

    Args:
        req_id: MCP request ID
        arguments: {
            key_prefix: str (e.g. "bncr_priv_a1b2c3d4")
            agent_id: str (must match)
        }

    Returns:
        MCP result with {status: "revoked"}
    """
    key_prefix = arguments.get('key_prefix', '').strip()
    agent_id = arguments.get('agent_id', '').strip()

    if not key_prefix:
        return mcp_error(req_id, -32602, "Missing required parameter: key_prefix")

    if not agent_id:
        return mcp_error(req_id, -32602, "Missing required parameter: agent_id")

    success = revoke_agent_key(key_prefix, agent_id)

    if not success:
        return mcp_error(req_id, -32603, "Failed to revoke key: not found or error")

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'status': 'revoked',
                'key_prefix': key_prefix,
                'agent_id': agent_id,
            })
        }]
    })


def mcp_tool_agent_key_list(req_id, arguments: dict) -> dict:
    """List all agent API keys (metadata only, never returns full key).

    Args:
        req_id: MCP request ID
        arguments: {
            agent_id: str (optional, filter by agent_id)
        }

    Returns:
        MCP result with list of {agent_id, agent_name, key_prefix, created_at, expires_at, revoked}
    """
    agent_id = arguments.get('agent_id', '').strip() or None

    keys = list_agent_keys(agent_id)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'status': 'success',
                'keys': keys,
                'count': len(keys),
            })
        }]
    })


def mcp_tool_agent_key_rotate(req_id, arguments: dict) -> dict:
    """Rotate an agent API key (create new, old stays active).

    Args:
        req_id: MCP request ID
        arguments: {
            agent_id: str
        }

    Returns:
        MCP result with new key info
        Old keys remain active until manually revoked
    """
    agent_id = arguments.get('agent_id', '').strip()

    if not agent_id:
        return mcp_error(req_id, -32602, "Missing required parameter: agent_id")

    try:
        result = rotate_agent_key(agent_id, created_by='mcp')

        return mcp_result(req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'success',
                    'key': result['key'],
                    'agent_id': result['agent_id'],
                    'agent_name': result['agent_name'],
                    'key_prefix': result['key_prefix'],
                    'created_at': result['created_at'],
                    'expires_at': result.get('expires_at'),
                    'warning': 'This key will ONLY be shown once. Old keys remain active until revoked.',
                })
            }]
        })

    except ValueError as e:
        return mcp_error(req_id, -32602, str(e))
    except Exception as e:
        logger.exception("Failed to rotate agent key", extra={"src_module": "mcp_agent_key", "operation": "rotate", "error": str(e)})
        return mcp_error(req_id, -32603, f"Failed to rotate agent key: {str(e)}")
