"""Sprint 39 UX improvements regression tests."""
import os
import sys
import json
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

from notifications import send_auto_approve_deploy_notification
from deployer import _format_changeset_summary


def test_auto_approve_notification_with_changes_summary():
    """Test that changes_summary appears in auto-approve notification."""
    import sys, importlib, telegram as _telegram_mod
    with patch.object(_telegram_mod, 'send_message_with_entities') as mock_send:
        mock_send.return_value = {'ok': True}
        # Force reload so notifications._telegram picks up our mock
        import importlib
        if 'notifications' in sys.modules:
            importlib.reload(sys.modules['notifications'])
        from notifications import send_auto_approve_deploy_notification as _fn

        _fn(
            project_id='test-project',
            deploy_id='d123',
            source='auto',
            reason='code-only',
            changes_summary='Lambda::Function: MyFunc (Modify), S3::Bucket: MyBucket (Add)',
        )

        assert mock_send.called
        text, entities = mock_send.call_args[0]
        assert '📋' in text
        assert '變更：' in text
        assert 'Lambda::Function: MyFunc (Modify)' in text


def test_auto_approve_notification_without_summary():
    """Test that _(無變更明細)_ appears when no changes_summary."""
    import sys, importlib, telegram as _telegram_mod
    with patch.object(_telegram_mod, 'send_message_with_entities') as mock_send:
        mock_send.return_value = {'ok': True}
        import importlib
        if 'notifications' in sys.modules:
            importlib.reload(sys.modules['notifications'])
        from notifications import send_auto_approve_deploy_notification as _fn

        _fn(
            project_id='test-project',
            deploy_id='d123',
            source='auto',
            reason='code-only',
            changes_summary='',
        )

        assert mock_send.called
        text, entities = mock_send.call_args[0]
        assert '(無變更明細)' in text


def test_format_changeset_summary_3_items():
    """Test that _format_changeset_summary formats 3 resources correctly."""
    resource_changes = [
        {
            'ResourceChange': {
                'ResourceType': 'AWS::Lambda::Function',
                'LogicalResourceId': 'ApprovalFunction',
                'Action': 'Modify',
            }
        },
        {
            'ResourceChange': {
                'ResourceType': 'AWS::S3::Bucket',
                'LogicalResourceId': 'Bucket1',
                'Action': 'Add',
            }
        },
        {
            'ResourceChange': {
                'ResourceType': 'AWS::DynamoDB::Table',
                'LogicalResourceId': 'Table1',
                'Action': 'Remove',
            }
        },
    ]

    summary = _format_changeset_summary(resource_changes)

    assert 'Lambda::Function: ApprovalFunction (Modify)' in summary
    assert 'S3::Bucket: Bucket1 (Add)' in summary
    assert 'DynamoDB::Table: Table1 (Remove)' in summary
    assert '+' not in summary  # No truncation for 3 items


def test_format_changeset_summary_truncates():
    """Test that _format_changeset_summary shows '+N more' for >3 resources."""
    resource_changes = [
        {
            'ResourceChange': {
                'ResourceType': 'AWS::Lambda::Function',
                'LogicalResourceId': f'Func{i}',
                'Action': 'Modify',
            }
        }
        for i in range(5)
    ]

    summary = _format_changeset_summary(resource_changes)

    assert 'Lambda::Function: Func0 (Modify)' in summary
    assert 'Lambda::Function: Func1 (Modify)' in summary
    assert 'Lambda::Function: Func2 (Modify)' in summary
    assert '+2 more' in summary
    assert 'Func3' not in summary  # Truncated


def test_format_changeset_summary_empty():
    """Test that _format_changeset_summary returns empty string for empty list."""
    assert _format_changeset_summary([]) == ''
    assert _format_changeset_summary(None) == ''


def test_datetime_entity_in_approval_notification():
    """Test that approval notification contains expiry info (date_time entity removed — Telegram requires unix_time format which is incompatible with MessageBuilder offset/length pattern)."""
    import telegram as _telegram_mod
    with patch.object(_telegram_mod, 'send_message_with_entities') as mock_send:
        mock_send.return_value = {'ok': True, 'result': {'message_id': 123}}
        import importlib
        if 'notifications' in sys.modules:
            importlib.reload(sys.modules['notifications'])
        from notifications import send_approval_request as _fn

        _fn(
            request_id='req123',
            command='aws s3 ls',
            reason='test',
            timeout=300,
        )

        assert mock_send.called
        text, entities = mock_send.call_args[0]
        # Expiry shown as relative time (e.g. "5 分鐘後過期")
        assert '後過期' in text, "Expected relative expiry in text"
