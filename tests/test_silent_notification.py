"""Tests for silent notification mode (#380)."""

import pytest
from moto import mock_aws
import boto3
from unittest.mock import patch, MagicMock


def _reset_caches():
    """Reset all module-level caches so they reinitialize inside mock context."""
    from src import config_store, db, execute_pipeline
    config_store._cache.clear()
    config_store._ddb_table = None
    if hasattr(db, '_table'):
        db._table = None
    # Reset cached table binding in execute_pipeline (from db import table)
    import boto3
    try:
        dynamodb = boto3.resource('dynamodb')
        execute_pipeline.table = dynamodb.Table('bouncer-prod-requests')
    except Exception:
        pass


@pytest.fixture
def ddb_tables():
    """Create mock DynamoDB tables with proper cache reset."""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        dynamodb.create_table(
            TableName='bouncer-prod-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        dynamodb.create_table(
            TableName='bouncer-config',
            KeySchema=[{'AttributeName': 'config_key', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'config_key', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        _reset_caches()
        yield
        _reset_caches()


@patch('src.execute_pipeline.log_decision')
@patch('src.execute_pipeline.store_paged_output')
@patch('src.execute_pipeline.emit_metric')
@patch('src.execute_pipeline.generate_request_id', return_value='test-req-001')
@patch('src.execute_pipeline.send_telegram_message_silent')
@patch('src.execute_pipeline.execute_command')
def test_auto_approved_silent_source_no_notification(mock_execute, mock_telegram, mock_gen_id, mock_metric, mock_paged, mock_log, ddb_tables, monkeypatch):
    """Test auto_approved with silent source → no notification, audit logged with notification_suppressed=true."""
    monkeypatch.setenv('TABLE_NAME', 'bouncer-prod-requests')
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    # Set silent_sources config
    from src.config_store import set_config
    set_config('silent_sources', ['Private Bot*'])

    # Mock command execution
    mock_execute.return_value = 'command output'
    mock_paged.return_value = MagicMock(telegram_pages=1)

    # Import after mocking
    from src.execute_pipeline import _check_auto_approve
    from src.execute_context import ExecuteContext

    # Create context with silent source
    ctx = ExecuteContext(
        req_id='test-req-001',
        command='aws s3 ls',
        reason='test reason',
        source='Private Bot (EKS)',
        account_id='123456789012',
        account_name='Test Account',
        assume_role='arn:aws:iam::123456789012:role/BouncerRole',
        trust_scope='test-scope',
        context='',
        timeout=300,
        bot_id='test-bot',
        caller_ip='1.2.3.4',
        grant_id=None,
        smart_decision=None,
        template_scan_result=None,
        sync_mode=False,
        is_native=False,
    )

    # Call _check_auto_approve
    with patch('src.execute_pipeline.is_auto_approve', return_value=True):
        with patch('src.execute_pipeline._should_throttle_notification', return_value=False):
            _check_auto_approve(ctx)

    # Assert no Telegram notification sent
    mock_telegram.assert_not_called()

    # Assert notification_suppressed was passed to log_decision
    mock_log.assert_called_once()
    assert mock_log.call_args[1].get('notification_suppressed') is True


@patch('src.execute_pipeline.log_decision')
@patch('src.execute_pipeline.store_paged_output')
@patch('src.execute_pipeline.emit_metric')
@patch('src.execute_pipeline.generate_request_id', return_value='test-req-002')
@patch('src.execute_pipeline.send_telegram_message_silent')
@patch('src.execute_pipeline.execute_command')
def test_auto_approved_non_silent_source_sends_notification(mock_execute, mock_telegram, mock_gen_id, mock_metric, mock_paged, mock_log, ddb_tables, monkeypatch):
    """Test auto_approved with non-silent source → notification sent."""
    monkeypatch.setenv('TABLE_NAME', 'bouncer-prod-requests')
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    # Set silent_sources config (does not match source)
    from src.config_store import set_config
    set_config('silent_sources', ['Private Bot*'])

    # Mock command execution
    mock_execute.return_value = 'command output'
    mock_paged.return_value = MagicMock(telegram_pages=1)

    # Import after mocking
    from src.execute_pipeline import _check_auto_approve
    from src.execute_context import ExecuteContext

    # Create context with non-silent source
    ctx = ExecuteContext(
        req_id='test-req-002',
        command='aws s3 ls',
        reason='test reason',
        source='Public Bot',
        account_id='123456789012',
        account_name='Test Account',
        assume_role='arn:aws:iam::123456789012:role/BouncerRole',
        trust_scope='test-scope',
        context='',
        timeout=300,
        bot_id='test-bot',
        caller_ip='1.2.3.4',
        grant_id=None,
        smart_decision=None,
        template_scan_result=None,
        sync_mode=False,
        is_native=False,
    )

    # Call _check_auto_approve
    with patch('src.execute_pipeline.is_auto_approve', return_value=True):
        with patch('src.execute_pipeline._should_throttle_notification', return_value=False):
            _check_auto_approve(ctx)

    # Assert Telegram notification sent
    mock_telegram.assert_called_once()

    # Assert audit log does not have notification_suppressed (or is False)
    items = table.scan()['Items']
    assert len(items) == 1
    assert items[0]['decision_type'] == 'auto_approved'
    assert items[0].get('notification_suppressed', False) is False


@patch('src.execute_pipeline.send_blocked_notification')
def test_blocked_silent_source_still_sends_notification(mock_blocked_notif, ddb_tables, monkeypatch):
    """Test blocked command with silent source → notification still sent (silent only applies to auto_approved)."""
    monkeypatch.setenv('TABLE_NAME', 'bouncer-prod-requests')
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    # Set silent_sources config
    from src.config_store import set_config
    set_config('silent_sources', ['Private Bot*'])

    # Import after mocking
    from src.execute_pipeline import _check_blocked
    from src.execute_context import ExecuteContext

    # Create context with silent source
    ctx = ExecuteContext(
        req_id='test-req-003',
        command='aws iam create-user --user-name malicious',
        reason='test reason',
        source='Private Bot (EKS)',
        account_id='123456789012',
        account_name='Test Account',
        assume_role='arn:aws:iam::123456789012:role/BouncerRole',
        trust_scope='test-scope',
        context='',
        timeout=300,
        bot_id='test-bot',
        caller_ip='1.2.3.4',
        grant_id=None,
        smart_decision=None,
        template_scan_result=None,
        sync_mode=False,
        is_native=False,
    )

    # Call _check_blocked
    with patch('src.commands.get_block_reason', return_value='User management is blocked'):
        _check_blocked(ctx)

    # Assert Telegram notification sent (blocked notifications are not suppressed)
    assert mock_blocked_notif.call_count >= 1


def test_wildcard_matching(ddb_tables, monkeypatch):
    """Test silent source wildcard matching (prefix*)."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    from src.config_store import set_config, _is_silent_source

    set_config('silent_sources', ['Private Bot*', 'Test*'])

    # Prefix matches
    assert _is_silent_source('Private Bot (EKS)') is True
    assert _is_silent_source('Private Bot') is True
    assert _is_silent_source('Test Agent') is True
    assert _is_silent_source('TestBot') is True

    # Non-matches
    assert _is_silent_source('Public Bot') is False
    assert _is_silent_source('Production Bot') is False


@mock_aws
def test_empty_config_all_notifications_sent(ddb_tables, monkeypatch):
    """Test empty silent_sources config → all notifications sent."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    from src.config_store import _is_silent_source

    # No config set
    assert _is_silent_source('Private Bot') is False
    assert _is_silent_source('Public Bot') is False
    assert _is_silent_source('Any Source') is False


@mock_aws
@patch('src.execute_pipeline.send_approval_request')
@patch('src.execute_pipeline.post_notification_setup')
def test_manual_approval_silent_source_sends_notification(mock_post_setup, mock_send_approval, ddb_tables, monkeypatch):
    """Test manual approval with silent source → notification still sent (silent only for auto_approved)."""
    monkeypatch.setenv('TABLE_NAME', 'bouncer-prod-requests')
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    # Set silent_sources config
    from src.config_store import set_config
    set_config('silent_sources', ['Private Bot*'])

    # Mock notification
    mock_send_approval.return_value = MagicMock(ok=True, message_id=12345)

    # Import after mocking
    from src.execute_pipeline import _submit_for_approval
    from src.execute_context import ExecuteContext

    # Create context with silent source
    ctx = ExecuteContext(
        req_id='test-req-004',
        command='aws ec2 terminate-instances --instance-ids i-1234567890abcdef0',
        reason='test reason',
        source='Private Bot (EKS)',
        account_id='123456789012',
        account_name='Test Account',
        assume_role='arn:aws:iam::123456789012:role/BouncerRole',
        trust_scope='test-scope',
        context='',
        timeout=300,
        bot_id='test-bot',
        caller_ip='1.2.3.4',
        grant_id=None,
        smart_decision=None,
        template_scan_result=None,
        sync_mode=False,
        is_native=False,
    )

    # Call _submit_for_approval
    with patch('src.execute_pipeline.get_scheduler_service', return_value=MagicMock()):
        _submit_for_approval(ctx)

    # Assert approval request notification sent (manual approval ignores silent config)
    mock_send_approval.assert_called_once()


@mock_aws
def test_is_silent_source_edge_cases(ddb_tables, monkeypatch):
    """Test _is_silent_source edge cases."""
    monkeypatch.setenv('CONFIG_TABLE', 'bouncer-config')



    from src.config_store import set_config, _is_silent_source

    set_config('silent_sources', ['Private Bot*', 'Exact Match', 'Test*'])

    # Edge cases
    assert _is_silent_source(None) is False
    assert _is_silent_source('') is False
    assert _is_silent_source('Private') is False  # Partial match without wildcard
    assert _is_silent_source('Exact Match') is True
    assert _is_silent_source('Exact Match Extra') is False  # Exact match doesn't match suffix
    assert _is_silent_source('Test') is True  # Wildcard matches prefix exactly
    assert _is_silent_source('Testing') is True
