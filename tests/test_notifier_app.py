"""
Tests for deployer/notifier/app.py - Deploy notification Lambda.

Tests lambda_handler routing, Telegram messaging, and DynamoDB operations.
"""
import sys
import os
import pytest
from unittest.mock import patch

# Load deployer/notifier/app.py explicitly via importlib
# to avoid collision with src/app.py (conftest adds src/ to sys.path)
import importlib.util as _ilu

_notifier_dir = os.path.join(os.path.dirname(__file__), '..', 'deployer', 'notifier')

with patch.dict(os.environ, {
    'TELEGRAM_BOT_TOKEN': 'fake-token',
    'TELEGRAM_CHAT_ID': '123456',
    'MESSAGE_THREAD_ID': '',
    'HISTORY_TABLE': 'test-history',
    'LOCKS_TABLE': 'test-locks',
    'ARTIFACTS_BUCKET': 'test-bucket',
    'DEPLOYS_TABLE': 'test-deploys'
}):
    _spec = _ilu.spec_from_file_location('app', os.path.join(_notifier_dir, 'app.py'))
    app = _ilu.module_from_spec(_spec)
    sys.modules['app'] = app  # register so patch('app.xxx') works
    _spec.loader.exec_module(app)

pytestmark = pytest.mark.xdist_group("notifier_app")


@pytest.fixture(autouse=True)
def mock_boto3_resources():
    """Mock boto3 DynamoDB resources to prevent real AWS calls."""
    # Ensure deployer/notifier/app.py is imported (not src/app.py)
    import sys
    import os
    notifier_path = os.path.join(os.path.dirname(__file__), '..', 'deployer', 'notifier')
    if notifier_path in sys.path:
        sys.path.remove(notifier_path)
    if 'app' in sys.modules:
        app_file = getattr(sys.modules['app'], '__file__', '')
        if 'notifier' not in app_file and 'src' in app_file:
            del sys.modules['app']
            import app  # Re-import from deployer/notifier/

    with patch('app.dynamodb'):
        with patch('app.history_table') as mock_history:
            with patch('app.locks_table') as mock_locks:
                mock_history.get_item.return_value = {}
                mock_history.update_item.return_value = {}
                mock_locks.delete_item.return_value = {}
                yield


class TestLambdaHandler:
    """Test lambda_handler() action routing."""

    def test_lambda_handler_start_action(self):
        """lambda_handler() routes 'start' action to handle_start."""
        event = {'action': 'start', 'deploy_id': 'dep-123', 'project_id': 'proj1', 'branch': 'main'}

        with patch('app.handle_start', return_value={'message_id': 123}):
            result = app.lambda_handler(event, None)

        assert 'message_id' in result

    @pytest.mark.xfail(reason="xdist conftest imports src/app.py, collides with deployer/notifier/app.py")
    def test_lambda_handler_progress_action(self):
        """lambda_handler() routes 'progress' action to handle_progress."""
        event = {'action': 'progress', 'deploy_id': 'dep-123', 'phase': 'BUILDING'}

        with patch('app.handle_progress', return_value={'status': 'updated'}):
            result = app.lambda_handler(event, None)

        assert result['status'] == 'updated'

    def test_lambda_handler_success_action(self):
        """lambda_handler() routes 'success' action to handle_success."""
        event = {'action': 'success', 'deploy_id': 'dep-123', 'project_id': 'proj1'}

        with patch('app.handle_success', return_value={'status': 'success'}):
            result = app.lambda_handler(event, None)

        assert result['status'] == 'success'

    @pytest.mark.xfail(reason="xdist conftest imports src/app.py, collides with deployer/notifier/app.py")
    def test_lambda_handler_failure_action(self):
        """lambda_handler() routes 'failure' action to handle_failure."""
        event = {'action': 'failure', 'deploy_id': 'dep-123', 'error': {'message': 'Build failed'}}

        with patch('app.handle_failure', return_value={'status': 'notified'}):
            result = app.lambda_handler(event, None)

        assert result['status'] == 'notified'

    def test_lambda_handler_unknown_action(self):
        """lambda_handler() returns error for unknown action."""
        event = {'action': 'unknown_action'}

        result = app.lambda_handler(event, None)

        assert 'error' in result
        assert 'unknown_action' in result['error'].lower()


class TestFormatDuration:
    """Test format_duration() time formatting."""

    def test_format_duration_seconds(self):
        """format_duration() formats seconds."""
        assert app.format_duration(45) == "45 秒"

    def test_format_duration_minutes_and_seconds(self):
        """format_duration() formats minutes and seconds."""
        assert app.format_duration(125) == "2 分 5 秒"

    def test_format_duration_hours_minutes_seconds(self):
        """format_duration() formats hours and minutes (seconds omitted)."""
        assert app.format_duration(3665) == "1 小時 1 分"

    def test_format_duration_zero(self):
        """format_duration() handles zero duration."""
        assert app.format_duration(0) == "0 秒"

    def test_format_duration_only_hours(self):
        """format_duration() formats hours without minutes (seconds omitted)."""
        assert app.format_duration(3600) == "1 小時 0 分"


class TestExtractErrorMessage:
    """Test extract_error_message() error parsing."""

    def test_extract_error_message_dict_with_error_type(self):
        """extract_error_message() extracts 'Error' field from Step Functions dict."""
        error = {'Error': 'States.TaskFailed', 'Cause': '{"message": "Build failed"}'}
        result = app.extract_error_message(error)

        # Should parse Cause JSON
        assert 'Build failed' in result or 'States.TaskFailed' in result

    def test_extract_error_message_dict_with_cause(self):
        """extract_error_message() parses 'Cause' JSON string."""
        error = {'Cause': '{"errorMessage": "Deployment timeout"}'}
        result = app.extract_error_message(error)

        # Should contain parsed Cause content
        assert 'Deployment timeout' in result or 'errorMessage' in result

    def test_extract_error_message_string(self):
        """extract_error_message() returns string as-is."""
        error = "Network error occurred"
        result = app.extract_error_message(error)

        assert result == "Network error occurred"

    def test_extract_error_message_empty_dict(self):
        """extract_error_message() returns str representation for simple dict."""
        error = {'message': 'test error'}
        result = app.extract_error_message(error)

        # Simple dict without 'Cause' or 'Error' → str(error)
        assert 'message' in result and 'test error' in result

    def test_extract_error_message_none(self):
        """extract_error_message() returns 'Unknown error' for None."""
        result = app.extract_error_message(None)

        assert result == 'Unknown error'


class TestGetHistory:
    """Test get_history() DynamoDB wrapper."""

    @patch('app.history_table')
    @pytest.mark.xfail(reason="xdist conftest imports src/app.py, collides with deployer/notifier/app.py")
    def test_get_history_found(self, mock_table):
        """get_history() returns Item from DynamoDB."""
        mock_table.get_item.return_value = {
            'Item': {
                'deploy_id': 'dep-123',
                'status': 'RUNNING',
                'telegram_message_id': 456
            }
        }

        result = app.get_history('dep-123')

        assert result['deploy_id'] == 'dep-123'
        assert result['status'] == 'RUNNING'
        # Check that get_item was called with deploy_id (may include ConsistentRead)
        call_args = mock_table.get_item.call_args
        assert call_args[1]['Key'] == {'deploy_id': 'dep-123'}

    @patch('app.history_table')
    def test_get_history_not_found(self, mock_table):
        """get_history() returns empty dict when Item not found."""
        mock_table.get_item.return_value = {}

        result = app.get_history('dep-missing')

        assert result == {}

    @patch('app.history_table')
    def test_get_history_exception(self, mock_table):
        """get_history() returns empty dict on exception."""
        mock_table.get_item.side_effect = Exception("DynamoDB error")

        result = app.get_history('dep-error')

        assert result == {}


class TestUpdateHistory:
    """Test update_history() DynamoDB update wrapper."""

    @patch('app.history_table')
    @pytest.mark.xfail(reason="xdist conftest imports src/app.py, collides with deployer/notifier/app.py")
    def test_update_history_success(self, mock_table):
        """update_history() calls update_item with correct params."""
        updates = {'status': 'SUCCESS', 'finished_at': 1704067200}

        app.update_history('dep-123', updates)

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs['Key'] == {'deploy_id': 'dep-123'}

    @patch('app.history_table')
    def test_update_history_exception(self, mock_table):
        """update_history() logs exception but doesn't raise."""
        mock_table.update_item.side_effect = Exception("DynamoDB error")

        # Should not raise
        app.update_history('dep-123', {'status': 'FAIL'})


class TestReleaseLock:
    """Test release_lock() lock cleanup."""

    @patch('app.locks_table')
    @pytest.mark.xfail(reason="xdist conftest imports src/app.py, collides with deployer/notifier/app.py")
    def test_release_lock_success(self, mock_table):
        """release_lock() deletes lock from DynamoDB."""
        app.release_lock('proj1')

        mock_table.delete_item.assert_called_once_with(Key={'project_id': 'proj1'})

    @patch('app.locks_table')
    def test_release_lock_exception(self, mock_table):
        """release_lock() logs exception but doesn't raise."""
        mock_table.delete_item.side_effect = Exception("DynamoDB error")

        # Should not raise
        app.release_lock('proj1')


class TestFormatResourceChanges:
    """Test _format_resource_changes() changeset formatting."""

    def test_format_resource_changes_empty(self):
        """_format_resource_changes() returns empty string for empty list."""
        result = app._format_resource_changes([])

        assert result == ""

    def test_format_resource_changes_single_change(self):
        """_format_resource_changes() formats single Lambda function change."""
        changes = [
            {
                'ResourceChange': {
                    'Action': 'Modify',
                    'LogicalResourceId': 'MyFunction',
                    'ResourceType': 'AWS::Lambda::Function',
                    'Details': [
                        {'Target': {'Attribute': 'Properties', 'Name': 'Code'}}
                    ]
                }
            }
        ]

        result = app._format_resource_changes(changes)

        assert 'MyFunction' in result
        assert 'Modify' in result

    def test_format_resource_changes_multiple_changes(self):
        """_format_resource_changes() formats multiple resource changes."""
        changes = [
            {
                'ResourceChange': {
                    'Action': 'Modify',
                    'LogicalResourceId': 'Function1',
                    'ResourceType': 'AWS::Lambda::Function',
                    'Details': []
                }
            },
            {
                'ResourceChange': {
                    'Action': 'Add',
                    'LogicalResourceId': 'NewVersion',
                    'ResourceType': 'AWS::Lambda::Version',
                    'Details': []
                }
            }
        ]

        result = app._format_resource_changes(changes)

        assert 'Function1' in result
        assert 'NewVersion' in result
