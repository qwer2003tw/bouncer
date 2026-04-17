"""
Tests for deployer/notifier/handler.py handle_analyze and handle_infra_approval_request

Sprint 73: Refactored to test DDB-based changeset flow (no more dry-run changeset creation).
Phase 1 (CodeBuild) creates the changeset via sam deploy --no-execute-changeset and stores
changeset_name in DDB. handle_analyze reads changeset_name from DDB and analyzes it.
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
    import handler as app


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
def sample_event():
    """Sample event for handle_analyze"""
    return {
        'deploy_id': 'test-deploy-001',
        'project_id': 'test-project',
    }


class TestHandleAnalyze:
    """Test handle_analyze function (Sprint 73 — DDB-based changeset flow)"""

    def test_handle_analyze_code_only_success(self, mock_env, sample_event):
        """Test handle_analyze with code-only changes from pre-created changeset"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': 'arn:aws:cloudformation:us-east-1:123456:changeSet/samcli-deploy-12345/guid',
            'no_changes': False,
            'telegram_message_id': 123,
        }

        mock_cfn = MagicMock()
        mock_analysis = MagicMock()
        mock_analysis.resource_changes = [
            {'ResourceChange': {'ResourceType': 'AWS::Lambda::Function', 'Action': 'Modify'}}
        ]
        mock_analysis.error = None

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value='test-stack'), \
             patch('boto3.client', return_value=mock_cfn), \
             patch('changeset_analyzer.analyze_changeset', return_value=mock_analysis) as mock_analyze, \
             patch('changeset_analyzer.is_code_only_change', return_value=True):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is True
            assert result['deploy_id'] == 'test-deploy-001'
            assert result['project_id'] == 'test-project'
            assert result['change_count'] == 1

            # Verify analyze_changeset was called with the changeset from DDB
            mock_analyze.assert_called_once()
            assert mock_analyze.call_args[0][1] == 'test-stack'
            assert 'samcli-deploy-12345' in mock_analyze.call_args[0][2]

    def test_handle_analyze_no_changes_flag(self, mock_env, sample_event):
        """Test handle_analyze when no_changes=true in DDB → auto-approve"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': '',
            'no_changes': True,
            'telegram_message_id': 123,
        }

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is True
            assert result['change_count'] == 0
            assert result['analysis_error'] is None

    def test_handle_analyze_infra_changes(self, mock_env, sample_event):
        """Test handle_analyze with infra changes → is_code_only=False"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': 'arn:aws:cloudformation:us-east-1:123456:changeSet/samcli-deploy-456/guid',
            'no_changes': False,
            'telegram_message_id': 123,
        }

        mock_cfn = MagicMock()
        mock_analysis = MagicMock()
        mock_analysis.resource_changes = [
            {'ResourceChange': {'ResourceType': 'AWS::IAM::Role', 'Action': 'Modify'}},
            {'ResourceChange': {'ResourceType': 'AWS::Lambda::Function', 'Action': 'Modify'}}
        ]
        mock_analysis.error = None

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value='test-stack'), \
             patch('boto3.client', return_value=mock_cfn), \
             patch('changeset_analyzer.analyze_changeset', return_value=mock_analysis), \
             patch('changeset_analyzer.is_code_only_change', return_value=False):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is False
            assert result['deploy_id'] == 'test-deploy-001'

    def test_handle_analyze_missing_changeset_name(self, mock_env, sample_event):
        """Test handle_analyze when changeset_name is missing → fail-safe"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            # No changeset_name key
            'no_changes': False,
            'telegram_message_id': 123,
        }

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value='test-stack'):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is False
            assert 'Missing changeset_name' in result['analysis_error']

    def test_handle_analyze_missing_stack_name(self, mock_env, sample_event):
        """Test handle_analyze when stack_name cannot be determined → fail-safe"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': 'some-changeset',
            'no_changes': False,
            'telegram_message_id': 123,
        }

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value=''):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is False
            assert 'Missing stack_name' in result['analysis_error']

    def test_handle_analyze_cfn_error(self, mock_env, sample_event):
        """Test handle_analyze with CFN describe-change-set error → fail-safe"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': 'some-changeset-arn',
            'no_changes': False,
            'telegram_message_id': 123,
        }

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value='test-stack'), \
             patch('boto3.client', return_value=MagicMock()), \
             patch('changeset_analyzer.analyze_changeset', side_effect=Exception('CloudFormation API error')):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is False
            assert 'CloudFormation API error' in result['analysis_error']

    def test_handle_analyze_self_deploying_project(self, mock_env):
        """Test handle_analyze for bouncer-deployer → always auto-approve"""
        event = {
            'deploy_id': 'test-deploy-bd',
            'project_id': 'bouncer-deployer',
        }

        history_data = {
            'deploy_id': 'test-deploy-bd',
            'telegram_message_id': 123,
        }

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'):

            result = app.handle_analyze(event)

            assert result['is_code_only'] is True
            assert result['project_id'] == 'bouncer-deployer'

    def test_handle_analyze_no_cleanup(self, mock_env, sample_event):
        """Verify handle_analyze does NOT call cleanup_changeset (Phase 2 handles it)"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': 'test-changeset-arn',
            'no_changes': False,
            'telegram_message_id': 123,
        }

        mock_cfn = MagicMock()
        mock_analysis = MagicMock()
        mock_analysis.resource_changes = []
        mock_analysis.error = None

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value='test-stack'), \
             patch('boto3.client', return_value=mock_cfn), \
             patch('changeset_analyzer.analyze_changeset', return_value=mock_analysis), \
             patch('changeset_analyzer.is_code_only_change', return_value=False):

            result = app.handle_analyze(sample_event)

            # Verify cleanup_changeset was NOT called on CFN client
            mock_cfn.delete_change_set.assert_not_called()

    def test_handle_analyze_no_updates_to_perform(self, mock_env, sample_event):
        """Test handle_analyze when changeset says 'No updates are to be performed' → code-only"""
        history_data = {
            'deploy_id': 'test-deploy-001',
            'changeset_name': 'some-changeset-arn',
            'no_changes': False,
            'telegram_message_id': 123,
        }

        mock_cfn = MagicMock()
        mock_analysis = MagicMock()
        mock_analysis.resource_changes = []
        mock_analysis.error = "No updates are to be performed."

        with patch('handler.get_history', return_value=history_data), \
             patch('handler.update_history'), \
             patch('handler.handle_progress'), \
             patch('handler._get_stack_name', return_value='test-stack'), \
             patch('boto3.client', return_value=mock_cfn), \
             patch('changeset_analyzer.analyze_changeset', return_value=mock_analysis):

            result = app.handle_analyze(sample_event)

            assert result['is_code_only'] is True
            assert result['change_count'] == 0


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

        with patch('handler.history_table', mock_history_table), \
             patch('handler.send_telegram_message', return_value=789) as mock_telegram, \
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

        with patch('handler.history_table', mock_history_table), \
             patch('handler.send_telegram_message', return_value=999) as mock_telegram, \
             patch('time.time', return_value=2000000):

            result = app.handle_infra_approval_request(event)

            # Verify result (should not fail)
            assert result['status'] == 'approval_requested'

            # Verify Telegram message uses default branch
            telegram_text = mock_telegram.call_args[0][0]
            assert 'master' in telegram_text


# ============================================================================
# Whitelist extensions: IAM::Role Policies + Custom::* ServiceToken (#278 part 2)
# ============================================================================

def test_is_code_only_iam_role_policies_only_is_safe():
    """SAM-generated IAM::Role Modify (Policies only) should be code-only (Lambda ARN update)."""
    from changeset_analyzer import is_code_only_change, AnalysisResult

    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[{
            "ResourceChange": {
                "ResourceType": "AWS::IAM::Role",
                "Action": "Modify",
                "LogicalResourceId": "DdbBackupScheduleRole",
                "Details": [{"Target": {"Name": "Policies", "Attribute": "Properties"}}],
            }
        }],
    )
    assert is_code_only_change(result) is True


def test_is_code_only_iam_role_non_policies_is_unsafe():
    """IAM::Role Modify with non-Policies target should NOT be code-only."""
    from changeset_analyzer import is_code_only_change, AnalysisResult

    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[{
            "ResourceChange": {
                "ResourceType": "AWS::IAM::Role",
                "Action": "Modify",
                "LogicalResourceId": "ApiFunctionRole",
                "Details": [{"Target": {"Name": "AssumeRolePolicyDocument", "Attribute": "Properties"}}],
            }
        }],
    )
    assert is_code_only_change(result) is False


def test_is_code_only_custom_service_token_only_is_safe():
    """Custom::* Modify (ServiceToken only) should be code-only (Lambda ARN update)."""
    from changeset_analyzer import is_code_only_change, AnalysisResult

    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[{
            "ResourceChange": {
                "ResourceType": "Custom::KeyPairValidator",
                "Action": "Modify",
                "LogicalResourceId": "KeyPairValidator",
                "Details": [{"Target": {"Name": "ServiceToken", "Attribute": "Properties"}}],
            }
        }],
    )
    assert is_code_only_change(result) is True


def test_is_code_only_custom_non_service_token_is_unsafe():
    """Custom::* Modify with non-ServiceToken target should NOT be code-only."""
    from changeset_analyzer import is_code_only_change, AnalysisResult

    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[{
            "ResourceChange": {
                "ResourceType": "Custom::KeyPairValidator",
                "Action": "Modify",
                "LogicalResourceId": "KeyPairValidator",
                "Details": [{"Target": {"Name": "KeyPair", "Attribute": "Properties"}}],
            }
        }],
    )
    assert is_code_only_change(result) is False
