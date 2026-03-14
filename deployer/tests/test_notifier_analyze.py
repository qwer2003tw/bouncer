"""
Tests for deployer/notifier/app.py handle_analyze and handle_infra_approval_request

Sprint 35-001c: Post-package changeset analysis
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add notifier directory to path
NOTIFIER_DIR = Path(__file__).parent.parent / "notifier"
sys.path.insert(0, str(NOTIFIER_DIR))

# Mock boto3 before importing app (app.py initializes dynamodb at module level)
with patch('boto3.resource'):
    import app


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables"""
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test_token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '12345')
    monkeypatch.setenv('HISTORY_TABLE', 'test-history-table')
    monkeypatch.setenv('LOCKS_TABLE', 'test-locks-table')
    monkeypatch.setenv('ARTIFACTS_BUCKET', 'test-artifacts-bucket')
    monkeypatch.setenv('DEPLOYS_TABLE', 'test-deploys-table')


@pytest.fixture
def mock_changeset_analyzer():
    """Mock changeset_analyzer module"""
    with patch('app.create_dry_run_changeset') as mock_create, \
         patch('app.analyze_changeset') as mock_analyze, \
         patch('app.cleanup_changeset') as mock_cleanup, \
         patch('app.is_code_only_change') as mock_is_code_only:
        yield {
            'create': mock_create,
            'analyze': mock_analyze,
            'cleanup': mock_cleanup,
            'is_code_only': mock_is_code_only,
        }


@pytest.fixture
def sample_event():
    """Sample event for handle_analyze"""
    return {
        'deploy_id': 'test-deploy-001',
        'project_id': 'test-project',
        'template_s3_key': 'deploys/test-deploy-001/packaged.yaml',
        'task_token': 'test-task-token-xyz',
    }


class TestHandleAnalyze:
    """Test handle_analyze function (Sprint 35-001c)"""

    def test_handle_analyze_code_only_success(self, mock_env, sample_event):
        """Test handle_analyze with code-only changes → send_task_success(is_code_only=True)"""
        # Mock DDB to return stack_name
        mock_ddb_resource = MagicMock()
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {'deploy_id': 'test-deploy-001', 'stack_name': 'test-stack', 'template_s3_url': 'https://test-artifacts-bucket.s3.amazonaws.com/test-project/packaged-template.yaml'}
        }
        mock_ddb_resource.Table.return_value = mock_table

        # Mock SFN and CFN clients
        mock_sfn = MagicMock()
        mock_cfn = MagicMock()

        # Mock changeset analyzer
        mock_analysis = MagicMock()
        mock_analysis.resource_changes = [
            {'ResourceChange': {'ResourceType': 'AWS::Lambda::Function', 'Action': 'Modify'}}
        ]
        mock_analysis.error = None

        with patch('boto3.resource', return_value=mock_ddb_resource), \
             patch('boto3.client') as mock_boto_client, \
             patch('changeset_analyzer.create_dry_run_changeset', return_value='test-changeset-123') as mock_create, \
             patch('changeset_analyzer.analyze_changeset', return_value=mock_analysis) as mock_analyze, \
             patch('changeset_analyzer.cleanup_changeset') as mock_cleanup, \
             patch('changeset_analyzer.is_code_only_change', return_value=True) as mock_is_code_only, \
             patch.object(app, 'ARTIFACTS_BUCKET', 'test-artifacts-bucket'), \
             patch.object(app, 'DEPLOYS_TABLE', 'test-deploys-table'):

            # Configure boto3.client to return appropriate mocks
            def client_factory(service, region_name=None):
                if service == 'stepfunctions':
                    return mock_sfn
                elif service == 'cloudformation':
                    return mock_cfn
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            result = app.handle_analyze(sample_event)

            # Verify result (no 'status' key in handle_analyze return)
            assert result['is_code_only'] is True

            # Verify changeset creation
            mock_create.assert_called_once()
            assert mock_create.call_args[0][0] == mock_cfn
            assert mock_create.call_args[0][1] == 'test-stack'
            assert 'test-artifacts-bucket.s3.amazonaws.com' in mock_create.call_args[0][2]

            # Verify changeset analysis
            mock_analyze.assert_called_once_with(mock_cfn, 'test-stack', 'test-changeset-123')

            # Verify cleanup
            mock_cleanup.assert_called_once_with(mock_cfn, 'test-stack', 'test-changeset-123')

            # handle_analyze returns dict directly (no SFN call)
            assert result['deploy_id'] == 'test-deploy-001'
            assert result['project_id'] == 'test-project'
            assert result['change_count'] == 1

    def test_handle_analyze_infra_changes(self, mock_env, sample_event):
        """Test handle_analyze with infra changes → send_task_success(is_code_only=False)"""
        # Mock DDB to return stack_name
        mock_ddb_resource = MagicMock()
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {'deploy_id': 'test-deploy-001', 'stack_name': 'test-stack', 'template_s3_url': 'https://test-artifacts-bucket.s3.amazonaws.com/test-project/packaged-template.yaml'}
        }
        mock_ddb_resource.Table.return_value = mock_table

        # Mock SFN and CFN clients
        mock_sfn = MagicMock()
        mock_cfn = MagicMock()

        # Mock changeset analyzer with infra changes
        mock_analysis = MagicMock()
        mock_analysis.resource_changes = [
            {'ResourceChange': {'ResourceType': 'AWS::IAM::Role', 'Action': 'Modify'}},
            {'ResourceChange': {'ResourceType': 'AWS::Lambda::Function', 'Action': 'Modify'}}
        ]
        mock_analysis.error = None

        with patch('boto3.resource', return_value=mock_ddb_resource), \
             patch('boto3.client') as mock_boto_client, \
             patch('changeset_analyzer.create_dry_run_changeset', return_value='test-changeset-456'), \
             patch('changeset_analyzer.analyze_changeset', return_value=mock_analysis), \
             patch('changeset_analyzer.cleanup_changeset'), \
             patch('changeset_analyzer.is_code_only_change', return_value=False), \
             patch.object(app, 'ARTIFACTS_BUCKET', 'test-artifacts-bucket'), \
             patch.object(app, 'DEPLOYS_TABLE', 'test-deploys-table'):

            # Configure boto3.client to return appropriate mocks
            def client_factory(service, region_name=None):
                if service == 'stepfunctions':
                    return mock_sfn
                elif service == 'cloudformation':
                    return mock_cfn
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            result = app.handle_analyze(sample_event)

            # Verify result (no 'status' key in handle_analyze return)
            assert result['is_code_only'] is False

            # handle_analyze returns dict directly — no SFN call
            assert result['deploy_id'] == 'test-deploy-001' 

    def test_handle_analyze_cfn_error(self, mock_env, sample_event):
        """Test handle_analyze with CFN error → send_task_failure"""
        # Mock DDB to return stack_name
        mock_ddb_resource = MagicMock()
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {'deploy_id': 'test-deploy-001', 'stack_name': 'test-stack', 'template_s3_url': 'https://test-artifacts-bucket.s3.amazonaws.com/test-project/packaged-template.yaml'}
        }
        mock_ddb_resource.Table.return_value = mock_table

        # Mock SFN and CFN clients
        mock_sfn = MagicMock()
        mock_cfn = MagicMock()

        with patch('boto3.resource', return_value=mock_ddb_resource), \
             patch('boto3.client') as mock_boto_client, \
             patch('changeset_analyzer.create_dry_run_changeset', side_effect=Exception('CloudFormation API error')), \
             patch('changeset_analyzer.cleanup_changeset'), \
             patch.object(app, 'ARTIFACTS_BUCKET', 'test-artifacts-bucket'), \
             patch.object(app, 'DEPLOYS_TABLE', 'test-deploys-table'):

            # Configure boto3.client to return appropriate mocks
            def client_factory(service, region_name=None):
                if service == 'stepfunctions':
                    return mock_sfn
                elif service == 'cloudformation':
                    return mock_cfn
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            result = app.handle_analyze(sample_event)

            # Verify result — handle_analyze returns is_code_only=False (fail-safe) on error
            assert result['is_code_only'] is False
            assert result['analysis_error'] is not None

            # handle_analyze returns dict directly — no SFN call
            assert result['analysis_error'] is not None and 'CloudFormation API error' in result['analysis_error'] 

    def test_handle_analyze_missing_stack_name(self, mock_env, sample_event):
        """Test handle_analyze when stack_name cannot be determined → send_task_failure"""
        # Mock DDB to return empty item (no stack_name)
        mock_ddb_resource = MagicMock()
        mock_table = MagicMock()
        mock_table.get_item.return_value = {'Item': {}}
        mock_ddb_resource.Table.return_value = mock_table

        # Mock SFN client
        mock_sfn = MagicMock()

        with patch('boto3.resource', return_value=mock_ddb_resource), \
             patch('boto3.client', return_value=mock_sfn), \
             patch.object(app, 'ARTIFACTS_BUCKET', 'test-artifacts-bucket'), \
             patch.object(app, 'DEPLOYS_TABLE', 'test-deploys-table'):

            result = app.handle_analyze(sample_event)

            # Verify result — fail-safe on missing stack_name
            assert result['is_code_only'] is False
            assert result['analysis_error'] is not None and 'Missing stack_name' in result['analysis_error']

            # handle_analyze returns dict directly — no SFN call


class TestGetStackName:
    """Test _get_stack_name helper function"""

    def test_get_stack_name_success(self):
        """Test successful stack_name retrieval from DDB"""
        mock_ddb_resource = MagicMock()
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {'deploy_id': 'test-001', 'stack_name': 'my-stack'}
        }
        mock_ddb_resource.Table.return_value = mock_table

        with patch('boto3.resource', return_value=mock_ddb_resource), \
             patch.object(app, 'DEPLOYS_TABLE', 'test-deploys-table'):

            stack_name = app._get_stack_name('test-001')
            assert stack_name == 'my-stack'

    def test_get_stack_name_empty_deploy_id(self):
        """Test _get_stack_name with empty deploy_id → returns empty string"""
        stack_name = app._get_stack_name('')
        assert stack_name == ''

    def test_get_stack_name_missing_item(self):
        """Test _get_stack_name when item not found in DDB → returns empty string"""
        mock_ddb_resource = MagicMock()
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_ddb_resource.Table.return_value = mock_table

        with patch('boto3.resource', return_value=mock_ddb_resource), \
             patch.object(app, 'DEPLOYS_TABLE', 'test-deploys-table'):

            stack_name = app._get_stack_name('test-001')
            assert stack_name == ''


class TestHandleInfraApprovalRequest:
    """Test handle_infra_approval_request function (Sprint 35-001c)"""

    def test_handle_infra_approval_request_success(self, mock_env):
        """Test handle_infra_approval_request → stores token in DDB + sends Telegram"""
        event = {
            'deploy_id': 'test-deploy-002',
            'project_id': 'test-project',
            'task_token': 'test-approval-token-abc',
            'change_count': 5,
        }

        mock_history_table = MagicMock()
        mock_history_table.get_item.return_value = {
            'Item': {'deploy_id': 'test-deploy-002', 'branch': 'feature-x'}
        }

        with patch('app.history_table', mock_history_table), \
             patch('app.send_telegram_message', return_value=789) as mock_telegram, \
             patch('time.time', return_value=1000000):

            result = app.handle_infra_approval_request(event)

            # Verify result
            assert result['status'] == 'approval_requested'
            assert result['message_id'] == 789

            # Verify DDB updates
            assert mock_history_table.update_item.call_count == 2

            # First update: store token + TTL
            first_call = mock_history_table.update_item.call_args_list[0]
            assert 'infra_approval_token' in first_call[1]['UpdateExpression']

            # Second update: store message_id
            second_call = mock_history_table.update_item.call_args_list[1]
            assert 'infra_approval_message_id' in second_call[1]['UpdateExpression']

            # Verify Telegram message sent
            mock_telegram.assert_called_once()
            telegram_text = mock_telegram.call_args[0][0]
            assert 'Infrastructure Changes Detected' in telegram_text
            assert 'test-project' in telegram_text
            assert 'feature-x' in telegram_text
            assert 'test-deploy-002' in telegram_text
            assert '5' in telegram_text

    def test_handle_infra_approval_request_no_history(self, mock_env):
        """Test handle_infra_approval_request when deploy has no history → uses defaults"""
        event = {
            'deploy_id': 'test-deploy-003',
            'project_id': 'test-project',
            'task_token': 'test-token',
            'change_count': 3,
        }

        mock_history_table = MagicMock()
        mock_history_table.get_item.return_value = {}

        with patch('app.history_table', mock_history_table), \
             patch('app.send_telegram_message', return_value=999) as mock_telegram, \
             patch('time.time', return_value=2000000):

            result = app.handle_infra_approval_request(event)

            # Verify result (should not fail)
            assert result['status'] == 'approval_requested'

            # Verify Telegram message uses default branch
            telegram_text = mock_telegram.call_args[0][0]
            assert 'master' in telegram_text
