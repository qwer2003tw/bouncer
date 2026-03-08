"""
Tests for sprint12-001: PROJECT_CONFIGS stored in DynamoDB

Covers:
  - _get_frontend_config(): DDB has record with frontend fields -> returns config
  - _get_frontend_config(): DDB has record without frontend fields -> returns None
  - _get_frontend_config(): DDB has no record -> returns None
  - _get_frontend_config(): DDB unavailable -> returns None (graceful)
  - _get_project_config(): DDB config present -> returns DDB config
  - _get_project_config(): DDB returns None -> returns None (no hardcoded fallback)
  - deploy_frontend full flow: project config from DDB -> success
  - deploy_frontend: project has no frontend config in DDB -> isError

Sprint 18 change: hardcoded fallback dict removed from _get_project_config().
All project configs must be seeded into DynamoDB bouncer-projects table.
"""
import base64
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('PROJECTS_TABLE', 'bouncer-projects')


def _b64(content: str) -> str:
    return base64.b64encode(content.encode()).decode()


def _make_files():
    return [
        {"filename": "index.html", "content": _b64("<html/>"), "content_type": "text/html"},
    ]


# ---------------------------------------------------------------------------
# Unit tests for _get_frontend_config
# ---------------------------------------------------------------------------

class TestGetFrontendConfig:
    """Unit tests for _get_frontend_config() DDB lookup."""

    def test_returns_config_when_ddb_has_frontend_fields(self):
        """DDB item with all frontend fields -> returns normalised config."""
        from mcp_deploy_frontend import _get_frontend_config

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'project_id': 'my-app',
                'frontend_bucket': 'my-app-frontend-bucket',
                'frontend_distribution_id': 'EABCDEF123456',
                'frontend_region': 'us-west-2',
                'frontend_deploy_role_arn': 'arn:aws:iam::123456789012:role/my-app-deploy-role',
            }
        }

        mock_boto3 = MagicMock()
        mock_boto3.return_value.Table.return_value = mock_table

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            result = _get_frontend_config('my-app')

        assert result is not None
        assert result['frontend_bucket'] == 'my-app-frontend-bucket'
        assert result['distribution_id'] == 'EABCDEF123456'
        assert result['region'] == 'us-west-2'
        assert result['deploy_role_arn'] == 'arn:aws:iam::123456789012:role/my-app-deploy-role'

    def test_returns_config_with_default_region(self):
        """DDB item without frontend_region -> defaults to us-east-1."""
        from mcp_deploy_frontend import _get_frontend_config

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'project_id': 'my-app',
                'frontend_bucket': 'my-app-bucket',
                'frontend_distribution_id': 'EXYZ',
                # no frontend_region
            }
        }

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            result = _get_frontend_config('my-app')

        assert result is not None
        assert result['region'] == 'us-east-1'

    def test_returns_none_when_no_ddb_item(self):
        """DDB has no item for project -> returns None."""
        from mcp_deploy_frontend import _get_frontend_config

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no 'Item' key

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            result = _get_frontend_config('no-such-project')

        assert result is None

    def test_returns_none_when_item_missing_frontend_bucket(self):
        """DDB item exists but no frontend_bucket -> returns None."""
        from mcp_deploy_frontend import _get_frontend_config

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'project_id': 'backend-only',
                'git_repo': 'some-repo',
                # no frontend fields
            }
        }

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            result = _get_frontend_config('backend-only')

        assert result is None

    def test_returns_none_when_item_missing_distribution_id(self):
        """DDB item has frontend_bucket but no frontend_distribution_id -> returns None."""
        from mcp_deploy_frontend import _get_frontend_config

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'project_id': 'half-configured',
                'frontend_bucket': 'some-bucket',
                # no frontend_distribution_id
            }
        }

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            result = _get_frontend_config('half-configured')

        assert result is None

    def test_returns_none_when_ddb_raises(self):
        """DDB throws exception -> graceful fallback (returns None, no exception)."""
        from mcp_deploy_frontend import _get_frontend_config

        mock_table = MagicMock()
        mock_table.get_item.side_effect = Exception("DDB unavailable")

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            result = _get_frontend_config('ztp-files')

        assert result is None  # graceful fallback, no exception raised


# ---------------------------------------------------------------------------
# Unit tests for _get_project_config (DDB + fallback logic)
# ---------------------------------------------------------------------------

class TestGetProjectConfig:
    """Unit tests for _get_project_config() resolution order."""

    def test_prefers_ddb_over_hardcoded(self):
        """DDB returns config -> hardcoded fallback is NOT used."""
        from mcp_deploy_frontend import _get_project_config

        ddb_config = {
            'frontend_bucket': 'ddb-bucket',
            'distribution_id': 'EDDB123',
            'region': 'eu-west-1',
            'deploy_role_arn': 'arn:aws:iam::111:role/ddb-role',
        }

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=ddb_config):
            result = _get_project_config('ztp-files')

        assert result == ddb_config
        assert result['frontend_bucket'] == 'ddb-bucket'  # NOT the hardcoded value

    def test_returns_none_when_ddb_returns_none(self):
        """DDB returns None -> _get_project_config returns None (no hardcoded fallback).
        Sprint 18: hardcoded fallback removed; all configs must be seeded in DDB."""
        from mcp_deploy_frontend import _get_project_config

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=None):
            result = _get_project_config('ztp-files')

        assert result is None  # No fallback; DDB is the only source

    def test_returns_none_for_unknown_project(self):
        """DDB returns None for unknown project -> _get_project_config returns None."""
        from mcp_deploy_frontend import _get_project_config

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=None):
            result = _get_project_config('completely-unknown-project')

        assert result is None

    def test_ddb_config_has_required_keys(self):
        """DDB config from _get_project_config has the expected canonical keys."""
        from mcp_deploy_frontend import _get_project_config

        ddb_config = {
            'frontend_bucket': 'test-bucket',
            'distribution_id': 'ETEST123',
            'region': 'us-east-1',
            'deploy_role_arn': 'arn:aws:iam::999:role/test-role',
        }

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=ddb_config):
            result = _get_project_config('test-app')

        required_keys = {'frontend_bucket', 'distribution_id', 'region', 'deploy_role_arn'}
        assert required_keys.issubset(result.keys())


# ---------------------------------------------------------------------------
# Integration tests: deploy_frontend with DDB config source
# ---------------------------------------------------------------------------

class TestDeployFrontendWithDDBConfig:
    """Integration tests ensuring deploy_frontend works with DDB-sourced config."""

    def _parse(self, result: dict) -> dict:
        body = json.loads(result["body"])
        text = body["result"]["content"][0]["text"]
        return json.loads(text)

    def _is_error(self, result: dict) -> bool:
        try:
            body = json.loads(result["body"])
            return bool(body.get("result", {}).get("isError"))
        except Exception:
            return False

    def _call(self, arguments: dict):
        from mcp_deploy_frontend import mcp_tool_deploy_frontend
        return mcp_tool_deploy_frontend("req-test", arguments)

    def test_deploy_succeeds_with_ddb_config(self):
        """Full deploy flow using DDB-sourced project config -> pending_approval."""
        ddb_config = {
            'frontend_bucket': 'ddb-frontend-bucket',
            'distribution_id': 'EDDB123',
            'region': 'us-east-1',
            'deploy_role_arn': 'arn:aws:iam::111:role/deploy-role',
        }

        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}

        mock_notif = MagicMock()
        mock_notif.ok = True
        mock_notif.message_id = 12345

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=ddb_config), \
             patch('mcp_deploy_frontend.boto3') as mock_b3, \
             patch('mcp_deploy_frontend.table') as mock_table, \
             patch('mcp_deploy_frontend.send_deploy_frontend_notification', return_value=mock_notif), \
             patch('mcp_deploy_frontend.post_notification_setup', create=True):

            mock_b3.client.return_value = mock_s3

            r = self._call({
                "project": "ztp-files",
                "files": _make_files(),
                "reason": "Test deploy via DDB config",
                "source": "test-bot",
                "trust_scope": "ts",
            })

        data = self._parse(r)
        assert not self._is_error(r), f"Unexpected error: {data}"
        assert data["status"] == "pending_approval"
        assert data["file_count"] == 1

    def test_deploy_fails_for_project_without_any_config(self):
        """Project with no DDB config and no hardcoded config -> isError."""
        with patch('mcp_deploy_frontend._get_frontend_config', return_value=None):
            r = self._call({
                "project": "nonexistent-project-xyz",
                "files": _make_files(),
                "source": "test-bot",
                "trust_scope": "ts",
            })

        data = self._parse(r)
        assert self._is_error(r)
        assert "Unknown project" in data["error"]
        assert "available_projects" in data

    def test_deploy_fails_for_project_with_partial_ddb_config(self):
        """Project in DDB but missing frontend_distribution_id -> falls back to hardcoded.
        For a project NOT in hardcoded -> isError."""
        mock_table = MagicMock()
        # Item exists but no distribution_id -> _get_frontend_config returns None
        mock_table.get_item.return_value = {
            'Item': {
                'project_id': 'partial-app',
                'frontend_bucket': 'some-bucket',
                # missing frontend_distribution_id
            }
        }

        with patch('mcp_deploy_frontend.boto3') as mock_b3:
            mock_b3.resource.return_value.Table.return_value = mock_table
            # Also mock _list_known_projects to avoid second DDB call
            with patch('mcp_deploy_frontend._list_known_projects', return_value=['ztp-files']):
                r = self._call({
                    "project": "partial-app",
                    "files": _make_files(),
                    "source": "test-bot",
                    "trust_scope": "ts",
                })

        data = self._parse(r)
        assert self._is_error(r)
        assert "Unknown project" in data["error"]

    def test_unknown_project_returns_available_projects_list(self):
        """Error response for unknown project includes available_projects."""
        with patch('mcp_deploy_frontend._get_frontend_config', return_value=None), \
             patch('mcp_deploy_frontend._list_known_projects', return_value=['ztp-files', 'other-app']):
            r = self._call({
                "project": "unknown-xyz",
                "files": _make_files(),
                "source": "test-bot",
                "trust_scope": "ts",
            })

        data = self._parse(r)
        assert self._is_error(r)
        assert isinstance(data["available_projects"], list)
        assert "ztp-files" in data["available_projects"]

    def test_ddb_config_propagated_to_ddb_record(self):
        """DDB-sourced config values appear in the approval request DDB record."""
        ddb_config = {
            'frontend_bucket': 'my-custom-bucket',
            'distribution_id': 'ECUSTOM123',
            'region': 'ap-southeast-1',
            'deploy_role_arn': 'arn:aws:iam::999:role/custom-role',
        }

        written_items = []

        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}

        mock_notif = MagicMock()
        mock_notif.ok = True
        mock_notif.message_id = None

        mock_approval_table = MagicMock()
        mock_approval_table.put_item.side_effect = lambda Item: written_items.append(Item)

        with patch('mcp_deploy_frontend._get_frontend_config', return_value=ddb_config), \
             patch('mcp_deploy_frontend.boto3') as mock_b3, \
             patch('mcp_deploy_frontend.table', mock_approval_table), \
             patch('mcp_deploy_frontend.send_deploy_frontend_notification', return_value=mock_notif):

            mock_b3.client.return_value = mock_s3

            self._call({
                "project": "custom-app",
                "files": _make_files(),
                "reason": "check DDB record",
                "source": "test-bot",
                "trust_scope": "ts",
            })

        assert len(written_items) == 1
        record = written_items[0]
        assert record['frontend_bucket'] == 'my-custom-bucket'
        assert record['distribution_id'] == 'ECUSTOM123'
        assert record['region'] == 'ap-southeast-1'
        assert record['deploy_role_arn'] == 'arn:aws:iam::999:role/custom-role'
