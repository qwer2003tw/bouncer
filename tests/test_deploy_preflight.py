"""
Tests for src/deploy_preflight.py - Pre-deploy validation logic.

Tests template URL validation, changed files detection, and secrets preflight checks.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, mock_open
from botocore.exceptions import ClientError

# Ensure src is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import deploy_preflight

pytestmark = pytest.mark.xdist_group("deploy_preflight")


class TestValidateTemplateS3Url:
    """Test validate_template_s3_url() format validation."""

    def test_valid_virtual_hosted_style_url(self):
        """validate_template_s3_url() accepts virtual-hosted-style S3 URLs."""
        url = "https://my-bucket.s3.us-east-1.amazonaws.com/templates/stack.yaml"
        is_valid, reason = deploy_preflight.validate_template_s3_url(url)

        assert is_valid is True
        assert reason == ""

    def test_valid_path_style_url(self):
        """validate_template_s3_url() accepts path-style S3 URLs."""
        url = "https://s3.amazonaws.com/my-bucket/template.yaml"
        is_valid, reason = deploy_preflight.validate_template_s3_url(url)

        assert is_valid is True
        assert reason == ""

    def test_valid_dash_region_url(self):
        """validate_template_s3_url() accepts dash-region S3 URLs."""
        url = "https://s3-us-west-2.amazonaws.com/bucket/key"
        is_valid, reason = deploy_preflight.validate_template_s3_url(url)

        assert is_valid is True
        assert reason == ""

    def test_empty_url(self):
        """validate_template_s3_url() rejects empty URL."""
        is_valid, reason = deploy_preflight.validate_template_s3_url("")

        assert is_valid is False
        assert "empty" in reason.lower()

    def test_url_too_long(self):
        """validate_template_s3_url() rejects URLs over 1024 chars."""
        url = "https://bucket.s3.amazonaws.com/" + "x" * 1024
        is_valid, reason = deploy_preflight.validate_template_s3_url(url)

        assert is_valid is False
        assert "too long" in reason.lower()

    def test_non_https_url(self):
        """validate_template_s3_url() rejects non-HTTPS URLs."""
        url = "http://bucket.s3.amazonaws.com/template.yaml"
        is_valid, reason = deploy_preflight.validate_template_s3_url(url)

        assert is_valid is False
        assert "https://" in reason.lower()

    def test_non_s3_url(self):
        """validate_template_s3_url() rejects non-S3 URLs."""
        url = "https://example.com/template.yaml"
        is_valid, reason = deploy_preflight.validate_template_s3_url(url)

        assert is_valid is False
        assert "s3" in reason.lower()


class TestGetChangedFiles:
    """Test _get_changed_files() git diff wrapper."""

    def test_get_changed_files_success(self):
        """_get_changed_files() returns list of changed file paths."""
        with patch('deploy_preflight.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='src/app.py\ntests/test_app.py\nREADME.md\n'
            )

            result = deploy_preflight._get_changed_files()

            assert len(result) == 3
            assert 'src/app.py' in result
            assert 'tests/test_app.py' in result
            assert 'README.md' in result

    def test_get_changed_files_no_changes(self):
        """_get_changed_files() returns empty list when no changes."""
        with patch('deploy_preflight.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='\n')

            result = deploy_preflight._get_changed_files()

            assert result == []

    def test_get_changed_files_git_error(self):
        """_get_changed_files() returns empty list on git error."""
        with patch('deploy_preflight.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout='')

            result = deploy_preflight._get_changed_files()

            assert result == []

    def test_get_changed_files_timeout(self):
        """_get_changed_files() returns empty list on subprocess timeout."""
        with patch('deploy_preflight.subprocess.run') as mock_run:
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired('git', 10)

            result = deploy_preflight._get_changed_files()

            assert result == []

    def test_get_changed_files_with_repo_path(self):
        """_get_changed_files() passes repo_path as cwd to subprocess."""
        with patch('deploy_preflight.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='file.txt\n')

            deploy_preflight._get_changed_files(repo_path='/tmp/repo')

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs['cwd'] == '/tmp/repo'


class TestPreflightCheckSecrets:
    """Test preflight_check_secrets() secret validation."""

    @patch('deploy_preflight._get_secretsmanager_client')
    def test_preflight_check_secrets_no_git_repo(self, mock_sm_client):
        """preflight_check_secrets() returns empty list when no git_repo configured."""
        project = {'project_id': 'test'}
        result = deploy_preflight.preflight_check_secrets(project, 'main')

        assert result == []
        mock_sm_client.assert_not_called()

    @patch('deploy_preflight._get_secretsmanager_client')
    def test_preflight_check_secrets_github_pat_fetch_fails(self, mock_sm_client):
        """preflight_check_secrets() returns empty list when GitHub PAT fetch fails."""
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = ClientError(
            {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Not found'}},
            'GetSecretValue'
        )
        mock_sm_client.return_value = mock_client

        project = {'git_repo': 'https://github.com/user/repo', 'sam_template_path': '.'}
        result = deploy_preflight.preflight_check_secrets(project, 'main')

        assert result == []

    @patch('deploy_preflight.shutil.rmtree')
    @patch('deploy_preflight.subprocess.run')
    @patch('deploy_preflight.tempfile.mkdtemp')
    @patch('deploy_preflight._get_secretsmanager_client')
    def test_preflight_check_secrets_git_clone_fails(
        self, mock_sm_client, mock_mkdtemp, mock_run, mock_rmtree
    ):
        """preflight_check_secrets() returns empty list when git clone fails."""
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {'SecretString': 'ghp_token'}
        mock_sm_client.return_value = mock_client

        mock_mkdtemp.return_value = '/tmp/test-clone'
        mock_run.return_value = MagicMock(returncode=128, stderr='fatal: repository not found')

        project = {'git_repo': 'https://github.com/user/repo', 'sam_template_path': '.'}
        result = deploy_preflight.preflight_check_secrets(project, 'main')

        assert result == []
        mock_rmtree.assert_called_once()

    @patch('deploy_preflight.shutil.rmtree')
    @patch('deploy_preflight.os.path.exists')
    @patch('deploy_preflight.subprocess.run')
    @patch('deploy_preflight.tempfile.mkdtemp')
    @patch('deploy_preflight._get_secretsmanager_client')
    def test_preflight_check_secrets_template_not_found(
        self, mock_sm_client, mock_mkdtemp, mock_run, mock_exists, mock_rmtree
    ):
        """preflight_check_secrets() returns empty list when template.yaml not found."""
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {'SecretString': 'ghp_token'}
        mock_sm_client.return_value = mock_client

        mock_mkdtemp.return_value = '/tmp/test-clone'
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = False  # template.yaml not found

        project = {'git_repo': 'https://github.com/user/repo', 'sam_template_path': '.'}
        result = deploy_preflight.preflight_check_secrets(project, 'main')

        assert result == []
        mock_rmtree.assert_called_once()

    @patch('deploy_preflight.shutil.rmtree')
    @patch('deploy_preflight.os.path.exists')
    @patch('deploy_preflight.subprocess.run')
    @patch('deploy_preflight.tempfile.mkdtemp')
    @patch('deploy_preflight._get_secretsmanager_client')
    def test_preflight_check_secrets_no_secrets_referenced(
        self, mock_sm_client, mock_mkdtemp, mock_run, mock_exists, mock_rmtree
    ):
        """preflight_check_secrets() returns empty list when no secrets in template."""
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {'SecretString': 'ghp_token'}
        mock_sm_client.return_value = mock_client

        mock_mkdtemp.return_value = '/tmp/test-clone'
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True

        template_content = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  MyFunction:
    Type: AWS::Lambda::Function
    Properties:
      Runtime: python3.11
"""
        with patch('builtins.open', mock_open(read_data=template_content)):
            project = {'git_repo': 'https://github.com/user/repo', 'sam_template_path': '.'}
            result = deploy_preflight.preflight_check_secrets(project, 'main')

        assert result == []
        mock_rmtree.assert_called_once()

    @patch('deploy_preflight.shutil.rmtree')
    @patch('deploy_preflight.os.path.exists')
    @patch('deploy_preflight.subprocess.run')
    @patch('deploy_preflight.tempfile.mkdtemp')
    @patch('deploy_preflight._get_secretsmanager_client')
    def test_preflight_check_secrets_missing_awscurrent(
        self, mock_sm_client, mock_mkdtemp, mock_run, mock_exists, mock_rmtree
    ):
        """preflight_check_secrets() returns missing secrets without AWSCURRENT."""
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {'SecretString': 'ghp_token'}
        # describe_secret returns no AWSCURRENT
        mock_client.describe_secret.return_value = {
            'VersionIdsToStages': {
                'v1': ['AWSPENDING']
            }
        }
        mock_sm_client.return_value = mock_client

        mock_mkdtemp.return_value = '/tmp/test-clone'
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True

        template_content = """
Environment:
  Variables:
    DB_PASSWORD: !Sub '{{resolve:secretsmanager:myapp/db-password:SecretString:password}}'
"""
        with patch('builtins.open', mock_open(read_data=template_content)):
            project = {'git_repo': 'https://github.com/user/repo', 'sam_template_path': '.'}
            result = deploy_preflight.preflight_check_secrets(project, 'main')

        assert 'myapp/db-password' in result
        mock_rmtree.assert_called_once()
