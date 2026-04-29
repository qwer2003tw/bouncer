"""
Test suite for frontend deploy auto-approve feature (Sprint 99).

Tests the auto-approve logic in bouncer_confirm_frontend_deploy:
  - Auto-approve when source is provided and all files verified
  - Manual approval fallback when FRONTEND_AUTO_APPROVE is disabled
  - Manual approval fallback when source is None/unknown
  - Silent notification is sent for auto-approved deployments
"""

import os
import sys
import json

# Setup environment
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('PROJECTS_TABLE', 'bouncer-projects')
os.environ.setdefault('HISTORY_TABLE', 'bouncer-deploy-history')

from unittest.mock import patch, MagicMock, call
import mcp_deploy_frontend as app


def _call_confirm(args):
    """Helper to call mcp_tool_confirm_frontend_deploy and parse result"""
    result = app.mcp_tool_confirm_frontend_deploy('req-test', args)
    body = json.loads(result['body'])
    return json.loads(body['result']['content'][0]['text'])


# ---------------------------------------------------------------------------
# Test 1: Auto-approve success
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_telegram_message_silent')
@patch('mcp_deploy_frontend.get_cloudfront_client')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend.deployer_history_table')
@patch('mcp_deploy_frontend.emit_metric')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_frontend_auto_approve_success(
    mock_get_project_config, mock_get_frontend_config, mock_emit_metric,
    mock_history_table, mock_table, mock_get_s3_client, mock_get_cloudfront_client,
    mock_send_silent
):
    """Auto-approve and execute deploy when source is provided and files verified"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
        'deploy_role_arn': None,
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }

    # Mock S3 client for head_object (file verification) and get_object/put_object (deploy)
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_s3.get_object.return_value = {
        'Body': MagicMock(read=lambda: b'<html>test</html>')
    }
    mock_get_s3_client.return_value = mock_s3

    # Mock CloudFront client
    mock_cf = MagicMock()
    mock_get_cloudfront_client.return_value = mock_cf

    # Mock DynamoDB tables
    mock_table.put_item = MagicMock()
    mock_table.update_item = MagicMock()
    mock_history_table.put_item = MagicMock()

    args = {
        'request_id': 'req-123',
        'project': 'ztp-files',
        'reason': 'Deploy new frontend',
        'source': 'agent-123',  # Valid source (not None/unknown)
        'files': [
            {'filename': 'index.html', 'content_type': 'text/html'},
            {'filename': 'assets/main.js', 'content_type': 'application/javascript'},
        ],
    }

    with patch.dict(os.environ, {'FRONTEND_AUTO_APPROVE': 'true'}):
        result = _call_confirm(args)

    # Verify auto-approve executed
    assert result['status'] == 'auto_approved'
    assert result['deploy_status'] == 'deployed'
    assert result['deployed_count'] == 2
    assert result['failed_count'] == 0
    assert 'auto-approved and executed' in result['message'].lower()

    # Verify DDB was updated with auto_approved status
    assert mock_table.put_item.called
    put_item_call = mock_table.put_item.call_args[1]['Item']
    assert put_item_call['status'] == 'auto_approved'
    assert put_item_call['project'] == 'ztp-files'
    assert put_item_call['source'] == 'agent-123'

    # Verify deploy was executed (S3 put_object called)
    assert mock_s3.put_object.call_count == 2

    # Verify CloudFront invalidation was called
    assert mock_cf.create_invalidation.called

    # Verify silent notification was sent
    assert mock_send_silent.called
    notification_text = mock_send_silent.call_args[0][0]
    assert '✅' in notification_text
    assert 'auto' in notification_text.lower() or 'Auto' in notification_text or '自動' in notification_text
    assert 'ztp-files' in notification_text

    # Verify history was written
    assert mock_history_table.put_item.called


# ---------------------------------------------------------------------------
# Test 2: Auto-approve disabled
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_deploy_frontend_notification')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_frontend_auto_approve_disabled(
    mock_get_project_config, mock_get_frontend_config,
    mock_table, mock_get_s3_client, mock_send_notification
):
    """Falls through to manual approval when FRONTEND_AUTO_APPROVE is disabled"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_get_s3_client.return_value = mock_s3

    mock_table.put_item = MagicMock()

    args = {
        'request_id': 'req-123',
        'project': 'ztp-files',
        'reason': 'Deploy new frontend',
        'source': 'agent-123',  # Valid source
        'files': [
            {'filename': 'index.html', 'content_type': 'text/html'},
        ],
    }

    with patch.dict(os.environ, {'FRONTEND_AUTO_APPROVE': 'false'}):
        result = _call_confirm(args)

    # Verify manual approval flow
    assert result['status'] == 'pending_approval'
    assert 'approval request sent' in result['message'].lower()

    # Verify DDB record has pending_approval status
    put_item_call = mock_table.put_item.call_args[1]['Item']
    assert put_item_call['status'] == 'pending_approval'

    # Verify Telegram notification was sent (manual approval request)
    assert mock_send_notification.called


# ---------------------------------------------------------------------------
# Test 3: Auto-approve with no source
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_deploy_frontend_notification')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_frontend_auto_approve_no_source(
    mock_get_project_config, mock_get_frontend_config,
    mock_table, mock_get_s3_client, mock_send_notification
):
    """Falls through to manual approval when source is None or unknown"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_get_s3_client.return_value = mock_s3

    mock_table.put_item = MagicMock()

    # Test with source=None
    args = {
        'request_id': 'req-123',
        'project': 'ztp-files',
        'reason': 'Deploy new frontend',
        'source': None,  # No source
        'files': [
            {'filename': 'index.html', 'content_type': 'text/html'},
        ],
    }

    with patch.dict(os.environ, {'FRONTEND_AUTO_APPROVE': 'true'}):
        result = _call_confirm(args)

    # Verify manual approval flow
    assert result['status'] == 'pending_approval'

    # Test with source='unknown'
    args['source'] = 'unknown'
    result = _call_confirm(args)
    assert result['status'] == 'pending_approval'


# ---------------------------------------------------------------------------
# Test 4: Auto-approve notification format
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_telegram_message_silent')
@patch('mcp_deploy_frontend.get_cloudfront_client')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend.deployer_history_table')
@patch('mcp_deploy_frontend.emit_metric')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_frontend_auto_approve_notification(
    mock_get_project_config, mock_get_frontend_config, mock_emit_metric,
    mock_history_table, mock_table, mock_get_s3_client, mock_get_cloudfront_client,
    mock_send_silent
):
    """Verify silent notification format for auto-approved deploy"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 512}
    mock_s3.get_object.return_value = {
        'Body': MagicMock(read=lambda: b'content')
    }
    mock_get_s3_client.return_value = mock_s3
    mock_get_cloudfront_client.return_value = MagicMock()

    mock_table.put_item = MagicMock()
    mock_table.update_item = MagicMock()
    mock_history_table.put_item = MagicMock()

    args = {
        'request_id': 'req-test-456',
        'project': 'ztp-files',
        'reason': 'Update homepage',
        'source': 'agent-bot-123',
        'files': [
            {'filename': 'index.html', 'content_type': 'text/html'},
        ],
    }

    with patch.dict(os.environ, {'FRONTEND_AUTO_APPROVE': 'true'}):
        result = _call_confirm(args)

    # Verify notification was sent
    assert mock_send_silent.called
    notification_text = mock_send_silent.call_args[0][0]

    # Verify notification contains required fields
    assert 'ztp-files' in notification_text  # Project
    assert 'Update homepage' in notification_text  # Reason
    assert 'agent-bot-123' in notification_text  # Source
    # Request ID should be in the notification (generated, not req-test-456)
    assert '請求' in notification_text or 'ID' in notification_text


# ---------------------------------------------------------------------------
# Test 5: Auto-approve with file count limit
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_deploy_frontend_notification')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_frontend_auto_approve_file_count_limit(
    mock_get_project_config, mock_get_frontend_config,
    mock_table, mock_get_s3_client, mock_send_notification
):
    """Falls through to manual approval when file count >= 500"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_get_s3_client.return_value = mock_s3

    mock_table.put_item = MagicMock()

    # Generate 500 files
    files = [{'filename': f'file{i}.txt', 'content_type': 'text/plain'} for i in range(500)]

    args = {
        'request_id': 'req-123',
        'project': 'ztp-files',
        'reason': 'Deploy many files',
        'source': 'agent-123',  # Valid source
        'files': files,
    }

    with patch.dict(os.environ, {'FRONTEND_AUTO_APPROVE': 'true'}):
        result = _call_confirm(args)

    # Verify manual approval flow (file count >= 500)
    assert result['status'] == 'pending_approval'
