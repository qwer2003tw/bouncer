"""bouncer_config MCP tool — manage dynamic config.

Tools:
  bouncer_config_get: get a config key
  bouncer_config_set: set a config key (value is JSON)
  bouncer_config_list: list all configs
"""

import json
from aws_lambda_powertools import Logger
from utils import mcp_result, mcp_error
from config_store import get_config, set_config, list_configs

logger = Logger(service="bouncer")


def mcp_tool_config_get(req_id, params: dict) -> dict:
    """Handle bouncer_config_get tool call.

    Args:
        req_id: JSON-RPC request ID
        params: {'key': str, 'default': optional}

    Returns:
        MCP result with config value
    """
    key = params.get('key')
    if not key:
        return mcp_error(req_id, -32602, "Missing required parameter: key")

    default = params.get('default')
    value = get_config(key, default=default)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'key': key,
                'value': value,
                'found': value is not None or default is not None,
            })
        }]
    })


def mcp_tool_config_set(req_id, params: dict) -> dict:
    """Handle bouncer_config_set tool call.

    Args:
        req_id: JSON-RPC request ID
        params: {'key': str, 'value': any (JSON-serializable), 'updated_by': optional str}

    Returns:
        MCP result indicating success
    """
    key = params.get('key')
    if not key:
        return mcp_error(req_id, -32602, "Missing required parameter: key")

    if 'value' not in params:
        return mcp_error(req_id, -32602, "Missing required parameter: value")

    value = params.get('value')
    updated_by = params.get('updated_by', 'mcp')

    try:
        set_config(key, value, updated_by=updated_by)
    except Exception as e:
        logger.exception("Failed to set config: %s", e, extra={"src_module": "mcp_config", "operation": "set_config", "key": key, "error": str(e)})
        return mcp_error(req_id, -32603, f"Failed to set config: {e}")

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'status': 'success',
                'key': key,
                'message': f'Config key "{key}" updated successfully',
            })
        }]
    })


def mcp_tool_config_list(req_id, params: dict) -> dict:
    """Handle bouncer_config_list tool call.

    Args:
        req_id: JSON-RPC request ID
        params: {} (no parameters)

    Returns:
        MCP result with list of all configs
    """
    configs = list_configs()

    # Convert DynamoDB Decimal to native types for JSON serialization
    from utils import decimal_to_native
    configs = decimal_to_native(configs)

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'count': len(configs),
                'configs': configs,
            })
        }]
    })
