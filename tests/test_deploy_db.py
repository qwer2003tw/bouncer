"""
Tests for src/deploy_db.py - DynamoDB operations and git helpers.

Tests git commit info extraction, project list, and DDB table accessors.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

# Ensure src is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import deploy_db

pytestmark = pytest.mark.xdist_group("deploy_db")


class TestGetGitCommitInfo:
    """Test get_git_commit_info() graceful fallback behavior."""

    def test_get_git_commit_info_success(self):
        """get_git_commit_info() returns sha, short, and message when git succeeds."""
        with patch('deploy_db.subprocess.run') as mock_run:
            # Mock git rev-parse HEAD
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout='abcdef1234567890abcdef1234567890abcdef12\n'),
                MagicMock(returncode=0, stdout='abcdef1 Initial commit\n')
            ]

            result = deploy_db.get_git_commit_info()

            assert result['commit_sha'] == 'abcdef1234567890abcdef1234567890abcdef12'
            assert result['commit_short'] == 'abcdef1'
            assert result['commit_message'] == 'Initial commit'

    def test_get_git_commit_info_not_a_git_repo(self):
        """get_git_commit_info() returns null dict when not in git repo."""
        with patch('deploy_db.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout='')

            result = deploy_db.get_git_commit_info()

            assert result['commit_sha'] is None
            assert result['commit_short'] is None
            assert result['commit_message'] is None

    def test_get_git_commit_info_timeout(self):
        """get_git_commit_info() returns null dict on subprocess timeout."""
        with patch('deploy_db.subprocess.run') as mock_run:
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired('git', 5)

            result = deploy_db.get_git_commit_info()

            assert result['commit_sha'] is None
            assert result['commit_short'] is None
            assert result['commit_message'] is None

    def test_get_git_commit_info_with_cwd(self):
        """get_git_commit_info() passes cwd to subprocess when specified."""
        with patch('deploy_db.subprocess.run') as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout='abc123\n'),
                MagicMock(returncode=0, stdout='abc123 Test\n')
            ]

            deploy_db.get_git_commit_info(cwd='/tmp/test-repo')

            # Check that cwd was passed to subprocess.run
            call_kwargs = mock_run.call_args_list[0][1]
            assert call_kwargs['cwd'] == '/tmp/test-repo'

    def test_get_git_commit_info_log_fails_gracefully(self):
        """get_git_commit_info() handles git log failure gracefully."""
        with patch('deploy_db.subprocess.run') as mock_run:
            # rev-parse succeeds, git log fails
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout='abc123\n'),
                MagicMock(returncode=128, stdout='')
            ]

            result = deploy_db.get_git_commit_info()

            assert result['commit_sha'] == 'abc123'
            assert result['commit_short'] == 'abc123'[:7]
            assert result['commit_message'] is None


class TestListProjects:
    """Test list_projects() DynamoDB scan wrapper."""

    @patch('deploy_db._get_projects_table')
    def test_list_projects_success(self, mock_get_table):
        """list_projects() returns Items from DDB scan."""
        mock_table = MagicMock()
        mock_table.scan.return_value = {
            'Items': [
                {'project_id': 'proj1', 'git_repo': 'https://github.com/user/repo1'},
                {'project_id': 'proj2', 'git_repo': 'https://github.com/user/repo2'}
            ]
        }
        mock_get_table.return_value = mock_table

        result = deploy_db.list_projects()

        assert len(result) == 2
        assert result[0]['project_id'] == 'proj1'
        assert result[1]['project_id'] == 'proj2'

    @patch('deploy_db._get_projects_table')
    def test_list_projects_empty(self, mock_get_table):
        """list_projects() returns empty list when no projects exist."""
        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': []}
        mock_get_table.return_value = mock_table

        result = deploy_db.list_projects()

        assert result == []

    @patch('deploy_db._get_projects_table')
    def test_list_projects_client_error(self, mock_get_table):
        """list_projects() returns empty list on ClientError (graceful degradation)."""
        mock_table = MagicMock()
        mock_table.scan.side_effect = ClientError(
            {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Table not found'}},
            'Scan'
        )
        mock_get_table.return_value = mock_table

        result = deploy_db.list_projects()

        assert result == []

    @patch('deploy_db._get_projects_table')
    def test_list_projects_generic_exception(self, mock_get_table):
        """list_projects() returns empty list on unexpected exception."""
        mock_table = MagicMock()
        mock_table.scan.side_effect = Exception("Network error")
        mock_get_table.return_value = mock_table

        result = deploy_db.list_projects()

        assert result == []


class TestGetDynamoDBHelpers:
    """Test _get_*_table() helper functions."""

    @patch('deploy_db.boto3.resource')
    @patch('deploy_db._db.deployer_projects_table._get')
    def test_get_projects_table_initializes_once(self, mock_db_get, mock_boto3):
        """_get_projects_table() uses deployer module global for caching."""
        import deployer
        # Reset module state
        deployer.projects_table = None
        deployer._dynamodb = None

        mock_table = MagicMock()
        mock_db_get.return_value = mock_table

        result1 = deploy_db._get_projects_table()
        result2 = deploy_db._get_projects_table()

        # _get() should be called only once (cached)
        assert mock_db_get.call_count == 1
        assert result1 == mock_table
        assert result2 == mock_table

    @patch('deploy_db.boto3.resource')
    @patch('deploy_db._db.deployer_history_table._get')
    def test_get_history_table(self, mock_db_get, mock_boto3):
        """_get_history_table() returns history table from deployer module."""
        import deployer
        deployer.history_table = None

        mock_table = MagicMock()
        mock_db_get.return_value = mock_table

        result = deploy_db._get_history_table()

        assert result == mock_table
        mock_db_get.assert_called_once()

    @patch('deploy_db.boto3.resource')
    @patch('deploy_db._db.deployer_locks_table._get')
    def test_get_locks_table(self, mock_db_get, mock_boto3):
        """_get_locks_table() returns locks table from deployer module."""
        import deployer
        deployer.locks_table = None

        mock_table = MagicMock()
        mock_db_get.return_value = mock_table

        result = deploy_db._get_locks_table()

        assert result == mock_table
        mock_db_get.assert_called_once()
