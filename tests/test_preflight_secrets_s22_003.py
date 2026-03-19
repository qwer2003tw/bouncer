"""
Tests for Sprint 22 Task 003: Pre-flight check for external secrets (#95)

Covers:
  - preflight_check_secrets: successful validation (all secrets have AWSCURRENT)
  - preflight_check_secrets: detect missing AWSCURRENT
  - preflight_check_secrets: detect non-existent secrets
  - preflight_check_secrets: graceful degradation on git clone failure
  - preflight_check_secrets: graceful degradation on missing template.yaml
  - mcp_tool_deploy: reject deploy when secrets are missing
  - mcp_tool_deploy: allow deploy when all secrets are valid
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('PROJECTS_TABLE', 'bouncer-projects')
os.environ.setdefault('HISTORY_TABLE', 'bouncer-deploy-history')
os.environ.setdefault('LOCKS_TABLE', 'bouncer-deploy-locks')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

import deployer
import deploy_preflight


@pytest.fixture
def mock_project():
    """Mock project configuration"""
    return {
        'project_id': 'ztp-files',
        'name': 'ZTP Files',
        'git_repo': 'https://github.com/example/ztp-files.git',
        'sam_template_path': '.',
        'default_branch': 'master',
        'stack_name': 'ztp-files-dev',
    }


@pytest.fixture
def mock_template_with_secrets():
    """Mock SAM template.yaml with secret references"""
    return '''
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

Resources:
  MyFunction:
    Type: AWS::Serverless::Function
    Properties:
      Environment:
        Variables:
          DB_PASSWORD: !Sub '{{resolve:secretsmanager:myapp/db-password:SecretString:password}}'
          API_KEY: !Sub '{{resolve:secretsmanager:myapp/api-key}}'
          WEBHOOK_SECRET: "{{resolve:secretsmanager:ztp-files-dev/github-webhook-secret}}"
'''


@pytest.fixture
def mock_template_no_secrets():
    """Mock SAM template.yaml without secret references"""
    return '''
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

Resources:
  MyFunction:
    Type: AWS::Serverless::Function
    Properties:
      Environment:
        Variables:
          STAGE: dev
'''


def test_preflight_check_all_secrets_valid(mock_project, mock_template_with_secrets):
    """Test preflight check passes when all secrets have AWSCURRENT"""
    with patch.object(deploy_preflight, '_get_secretsmanager_client') as mock_sm_client_func:
        mock_sm_client = MagicMock()
        mock_sm_client_func.return_value = mock_sm_client

        # Mock GitHub PAT
        mock_sm_client.get_secret_value.side_effect = [
            {'SecretString': 'ghp_test123'},  # GitHub PAT
        ]

        # Mock describe_secret for each referenced secret
        def describe_secret_side_effect(SecretId):
            return {
                'ARN': f'arn:aws:secretsmanager:us-east-1:123456789012:secret:{SecretId}',
                'Name': SecretId,
                'VersionIdsToStages': {
                    'v1': ['AWSCURRENT'],
                }
            }

        mock_sm_client.describe_secret.side_effect = describe_secret_side_effect

        # Mock git clone
        with patch('subprocess.run') as mock_run, \
             patch('tempfile.mkdtemp', return_value='/tmp/test-clone'), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: mock_template_with_secrets)))), \
             patch('shutil.rmtree'):

            mock_run.return_value = MagicMock(returncode=0, stderr='')

            # Call preflight check
            missing = deployer.preflight_check_secrets(mock_project, 'master')

            # Should return empty list (all secrets valid)
            assert missing == []
            assert mock_sm_client.describe_secret.call_count == 3  # 3 unique secrets


def test_preflight_check_missing_awscurrent(mock_project, mock_template_with_secrets):
    """Test preflight check detects secrets without AWSCURRENT"""
    with patch.object(deploy_preflight, '_get_secretsmanager_client') as mock_sm_client_func:
        mock_sm_client = MagicMock()
        mock_sm_client_func.return_value = mock_sm_client

        # Mock GitHub PAT
        mock_sm_client.get_secret_value.side_effect = [
            {'SecretString': 'ghp_test123'},
        ]

        # Mock describe_secret: one secret has no AWSCURRENT
        def describe_secret_side_effect(SecretId):
            if SecretId == 'ztp-files-dev/github-webhook-secret':
                # This secret exists but has no AWSCURRENT (empty secret)
                return {
                    'ARN': f'arn:aws:secretsmanager:us-east-1:123456789012:secret:{SecretId}',
                    'Name': SecretId,
                    'VersionIdsToStages': {},  # No versions!
                }
            else:
                return {
                    'ARN': f'arn:aws:secretsmanager:us-east-1:123456789012:secret:{SecretId}',
                    'Name': SecretId,
                    'VersionIdsToStages': {'v1': ['AWSCURRENT']},
                }

        mock_sm_client.describe_secret.side_effect = describe_secret_side_effect

        # Mock git clone
        with patch('subprocess.run') as mock_run, \
             patch('tempfile.mkdtemp', return_value='/tmp/test-clone'), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: mock_template_with_secrets)))), \
             patch('shutil.rmtree'):

            mock_run.return_value = MagicMock(returncode=0, stderr='')

            # Call preflight check
            missing = deployer.preflight_check_secrets(mock_project, 'master')

            # Should detect the missing secret
            assert 'ztp-files-dev/github-webhook-secret' in missing
            assert len(missing) == 1


def test_preflight_check_secret_not_found(mock_project, mock_template_with_secrets):
    """Test preflight check detects non-existent secrets"""
    with patch.object(deploy_preflight, '_get_secretsmanager_client') as mock_sm_client_func:
        mock_sm_client = MagicMock()
        mock_sm_client_func.return_value = mock_sm_client

        # Mock GitHub PAT
        mock_sm_client.get_secret_value.side_effect = [
            {'SecretString': 'ghp_test123'},
        ]

        # Mock describe_secret: one secret doesn't exist
        def describe_secret_side_effect(SecretId):
            if SecretId == 'myapp/api-key':
                raise mock_sm_client.exceptions.ResourceNotFoundException({
                    'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Secret not found'}
                }, 'DescribeSecret')
            else:
                return {
                    'ARN': f'arn:aws:secretsmanager:us-east-1:123456789012:secret:{SecretId}',
                    'Name': SecretId,
                    'VersionIdsToStages': {'v1': ['AWSCURRENT']},
                }

        mock_sm_client.describe_secret.side_effect = describe_secret_side_effect
        mock_sm_client.exceptions.ResourceNotFoundException = type('ResourceNotFoundException', (Exception,), {})

        # Mock git clone
        with patch('subprocess.run') as mock_run, \
             patch('tempfile.mkdtemp', return_value='/tmp/test-clone'), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: mock_template_with_secrets)))), \
             patch('shutil.rmtree'):

            mock_run.return_value = MagicMock(returncode=0, stderr='')

            # Call preflight check
            missing = deployer.preflight_check_secrets(mock_project, 'master')

            # Should detect the non-existent secret
            assert 'myapp/api-key' in missing
            assert len(missing) == 1


def test_preflight_check_git_clone_failure(mock_project):
    """Test preflight check gracefully handles git clone failure"""
    with patch.object(deploy_preflight, '_get_secretsmanager_client') as mock_sm_client_func:
        mock_sm_client = MagicMock()
        mock_sm_client_func.return_value = mock_sm_client

        # Mock GitHub PAT
        mock_sm_client.get_secret_value.return_value = {'SecretString': 'ghp_test123'}

        # Mock git clone failure
        with patch('subprocess.run') as mock_run, \
             patch('tempfile.mkdtemp', return_value='/tmp/test-clone'), \
             patch('shutil.rmtree'):

            mock_run.return_value = MagicMock(returncode=1, stderr='fatal: repository not found')

            # Call preflight check
            missing = deployer.preflight_check_secrets(mock_project, 'master')

            # Should return empty list (graceful degradation)
            assert missing == []


def test_preflight_check_no_template_file(mock_project):
    """Test preflight check gracefully handles missing template.yaml"""
    with patch.object(deploy_preflight, '_get_secretsmanager_client') as mock_sm_client_func:
        mock_sm_client = MagicMock()
        mock_sm_client_func.return_value = mock_sm_client

        # Mock GitHub PAT
        mock_sm_client.get_secret_value.return_value = {'SecretString': 'ghp_test123'}

        # Mock git clone success but template.yaml not found
        with patch('subprocess.run') as mock_run, \
             patch('tempfile.mkdtemp', return_value='/tmp/test-clone'), \
             patch('os.path.exists', return_value=False), \
             patch('shutil.rmtree'):

            mock_run.return_value = MagicMock(returncode=0, stderr='')

            # Call preflight check
            missing = deployer.preflight_check_secrets(mock_project, 'master')

            # Should return empty list (graceful degradation)
            assert missing == []


def test_preflight_check_no_secrets_in_template(mock_project, mock_template_no_secrets):
    """Test preflight check passes when template has no secrets"""
    with patch.object(deploy_preflight, '_get_secretsmanager_client') as mock_sm_client_func:
        mock_sm_client = MagicMock()
        mock_sm_client_func.return_value = mock_sm_client

        # Mock GitHub PAT
        mock_sm_client.get_secret_value.return_value = {'SecretString': 'ghp_test123'}

        # Mock git clone
        with patch('subprocess.run') as mock_run, \
             patch('tempfile.mkdtemp', return_value='/tmp/test-clone'), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: mock_template_no_secrets)))), \
             patch('shutil.rmtree'):

            mock_run.return_value = MagicMock(returncode=0, stderr='')

            # Call preflight check
            missing = deployer.preflight_check_secrets(mock_project, 'master')

            # Should return empty list (no secrets to check)
            assert missing == []
            assert mock_sm_client.describe_secret.call_count == 0


def test_mcp_tool_deploy_rejects_missing_secrets():
    """Test mcp_tool_deploy rejects deploy when secrets are missing"""
    with patch.object(deployer, 'get_project') as mock_get_project, \
         patch.object(deployer, 'preflight_check_secrets') as mock_preflight, \
         patch.object(deployer, 'get_lock') as mock_get_lock:

        mock_get_project.return_value = {
            'project_id': 'ztp-files',
            'name': 'ZTP Files',
            'default_branch': 'master',
        }

        # Simulate missing secrets
        mock_preflight.return_value = ['ztp-files-dev/github-webhook-secret', 'myapp/api-key']
        mock_get_lock.return_value = None  # No existing lock

        # Mock database table
        mock_table = MagicMock()

        # Call mcp_tool_deploy
        result = deployer.mcp_tool_deploy(
            req_id='test-001',
            arguments={'project': 'ztp-files', 'branch': 'master', 'reason': 'test deploy'},
            table=mock_table,
            send_approval_func=MagicMock()
        )

        # Parse the response structure
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['jsonrpc'] == '2.0'
        assert body['id'] == 'test-001'

        # Check the result contains error
        result_data = body['result']
        assert result_data['isError'] is True
        content_text = result_data['content'][0]['text']
        content_data = json.loads(content_text)
        assert content_data['status'] == 'error'
        assert 'Deploy 前檢查失敗' in content_data['error']
        assert 'ztp-files-dev/github-webhook-secret' in content_data['missing_secrets']
        assert 'myapp/api-key' in content_data['missing_secrets']


def test_mcp_tool_deploy_proceeds_when_secrets_valid():
    """Test mcp_tool_deploy proceeds when all secrets are valid"""
    with patch.object(deployer, 'get_project') as mock_get_project, \
         patch.object(deployer, 'preflight_check_secrets') as mock_preflight, \
         patch.object(deployer, 'get_lock') as mock_get_lock, \
         patch.object(deployer, '_db') as mock_db:

        mock_get_project.return_value = {
            'project_id': 'ztp-files',
            'name': 'ZTP Files',
            'default_branch': 'master',
        }

        # Simulate all secrets valid
        mock_preflight.return_value = []
        mock_get_lock.return_value = None  # No existing lock

        # Mock database
        mock_table = MagicMock()
        mock_db.table = mock_table

        # Call mcp_tool_deploy
        result = deployer.mcp_tool_deploy(
            req_id='test-001',
            arguments={'project': 'ztp-files', 'branch': 'master', 'reason': 'test deploy'},
            table=mock_table,
            send_approval_func=MagicMock()
        )

        # Parse the response structure
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['jsonrpc'] == '2.0'
        assert body['id'] == 'test-001'

        # Should proceed to approval request (not error)
        result_data = body['result']
        assert result_data.get('isError') is not True
        content_text = result_data['content'][0]['text']
        content_data = json.loads(content_text)
        assert content_data['status'] == 'pending_approval'
        assert mock_table.put_item.called
