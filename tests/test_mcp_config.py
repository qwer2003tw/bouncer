"""Tests for mcp_config module."""
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from unittest.mock import patch
from decimal import Decimal


def parse_mcp_response(response):
    """Helper to parse MCP response from HTTP response format."""
    body = json.loads(response['body'])
    return body


class TestMcpToolConfigGet:
    """Tests for mcp_tool_config_get function."""

    @patch('mcp_config.get_config')
    def test_get_existing_config(self, mock_get_config):
        """Test getting an existing config value."""
        from mcp_config import mcp_tool_config_get

        mock_get_config.return_value = 'test_value'

        result = mcp_tool_config_get('req-123', {'key': 'test_key'})
        body = parse_mcp_response(result)

        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['key'] == 'test_key'
        assert content['value'] == 'test_value'
        assert content['found'] is True

        mock_get_config.assert_called_once_with('test_key', default=None)

    @patch('mcp_config.get_config')
    def test_get_non_existing_config_with_default(self, mock_get_config):
        """Test getting non-existing config with default value."""
        from mcp_config import mcp_tool_config_get

        mock_get_config.return_value = 'default_value'

        result = mcp_tool_config_get('req-456', {
            'key': 'missing_key',
            'default': 'default_value'
        })

        body = parse_mcp_response(result)
        content = json.loads(body['result']['content'][0]['text'])
        assert content['value'] == 'default_value'
        assert content['found'] is True

    @patch('mcp_config.get_config')
    def test_get_non_existing_config_no_default(self, mock_get_config):
        """Test getting non-existing config without default."""
        from mcp_config import mcp_tool_config_get

        mock_get_config.return_value = None

        result = mcp_tool_config_get('req-789', {'key': 'missing_key'})

        body = parse_mcp_response(result)
        content = json.loads(body['result']['content'][0]['text'])
        assert content['value'] is None
        assert content['found'] is False

    def test_get_config_missing_key_parameter(self):
        """Test error when key parameter is missing."""
        from mcp_config import mcp_tool_config_get

        result = mcp_tool_config_get('req-error', {})
        body = parse_mcp_response(result)

        assert 'error' in body
        assert body['error']['code'] == -32602
        assert 'Missing required parameter: key' in body['error']['message']

    def test_get_config_empty_key(self):
        """Test error when key is empty string."""
        from mcp_config import mcp_tool_config_get

        result = mcp_tool_config_get('req-empty', {'key': ''})
        body = parse_mcp_response(result)

        assert 'error' in body
        assert body['error']['code'] == -32602


class TestMcpToolConfigSet:
    """Tests for mcp_tool_config_set function."""

    @patch('mcp_config.set_config')
    def test_set_config_success(self, mock_set_config):
        """Test successfully setting a config value."""
        from mcp_config import mcp_tool_config_set

        result = mcp_tool_config_set('req-set-1', {
            'key': 'my_config',
            'value': 'my_value'
        })

        body = parse_mcp_response(result)
        assert 'result' in body
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'success'
        assert content['key'] == 'my_config'

        mock_set_config.assert_called_once_with('my_config', 'my_value', updated_by='mcp')

    @patch('mcp_config.set_config')
    def test_set_config_with_custom_updated_by(self, mock_set_config):
        """Test setting config with custom updated_by."""
        from mcp_config import mcp_tool_config_set

        result = mcp_tool_config_set('req-set-2', {
            'key': 'config_key',
            'value': 123,
            'updated_by': 'admin:user123'
        })

        body = parse_mcp_response(result)
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'success'

        mock_set_config.assert_called_once_with('config_key', 123, updated_by='admin:user123')

    def test_set_config_missing_key(self):
        """Test error when key parameter is missing."""
        from mcp_config import mcp_tool_config_set

        result = mcp_tool_config_set('req-err-1', {'value': 'some_value'})
        body = parse_mcp_response(result)

        assert 'error' in body
        assert body['error']['code'] == -32602

    def test_set_config_missing_value(self):
        """Test error when value parameter is missing."""
        from mcp_config import mcp_tool_config_set

        result = mcp_tool_config_set('req-err-2', {'key': 'some_key'})
        body = parse_mcp_response(result)

        assert 'error' in body
        assert body['error']['code'] == -32602

    @patch('mcp_config.set_config')
    def test_set_config_exception_handling(self, mock_set_config):
        """Test exception handling during set_config."""
        from mcp_config import mcp_tool_config_set

        mock_set_config.side_effect = Exception("DynamoDB error")

        result = mcp_tool_config_set('req-err-4', {
            'key': 'fail_key',
            'value': 'fail_value'
        })

        body = parse_mcp_response(result)
        assert 'error' in body
        assert body['error']['code'] == -32603


class TestMcpToolConfigList:
    """Tests for mcp_tool_config_list function."""

    @patch('mcp_config.list_configs')
    def test_list_configs_success(self, mock_list_configs):
        """Test successfully listing all configs."""
        from mcp_config import mcp_tool_config_list

        mock_configs = [
            {'key': 'config1', 'value': 'value1'},
            {'key': 'config2', 'value': 'value2'}
        ]
        mock_list_configs.return_value = mock_configs

        result = mcp_tool_config_list('req-list-1', {})

        body = parse_mcp_response(result)
        content = json.loads(body['result']['content'][0]['text'])
        assert content['count'] == 2
        assert len(content['configs']) == 2

        mock_list_configs.assert_called_once()

    @patch('mcp_config.list_configs')
    def test_list_configs_empty(self, mock_list_configs):
        """Test listing configs when none exist."""
        from mcp_config import mcp_tool_config_list

        mock_list_configs.return_value = []

        result = mcp_tool_config_list('req-list-2', {})

        body = parse_mcp_response(result)
        content = json.loads(body['result']['content'][0]['text'])
        assert content['count'] == 0
        assert content['configs'] == []
