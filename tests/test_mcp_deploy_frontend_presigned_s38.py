"""
Test suite for presigned URL two-step frontend deployment flow (bouncer-s38-001).

Tests the new tools:
  - bouncer_request_frontend_presigned (Step 1: generate presigned URLs)
  - bouncer_confirm_frontend_deploy (Step 2: verify uploads + create approval)
"""

import os
import json

# Setup environment
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('PROJECTS_TABLE', 'bouncer-projects')

from unittest.mock import patch, MagicMock
import mcp_deploy_frontend as app


def _call_presigned(args):
    """Helper to call mcp_tool_request_frontend_presigned and parse result"""
    result = app.mcp_tool_request_frontend_presigned('req-test', args)
    body = json.loads(result['body'])
    return json.loads(body['result']['content'][0]['text'])


def _call_confirm(args):
    """Helper to call mcp_tool_confirm_frontend_deploy and parse result"""
    result = app.mcp_tool_confirm_frontend_deploy('req-test', args)
    body = json.loads(result['body'])
    return json.loads(body['result']['content'][0]['text'])


# ---------------------------------------------------------------------------
# Test 1: Request presigned - success
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend._get_project_config')
def test_request_presigned_success(mock_get_project_config, mock_get_s3_client):
    """Step 1: Generate presigned URLs for valid files"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
        'deploy_role_arn': 'arn:aws:iam::123456789012:role/deploy',
    }

    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = 'https://s3.amazonaws.com/test-presigned-url'
    mock_get_s3_client.return_value = mock_s3

    args = {
        'project': 'ztp-files',
        'files': [
            {'filename': 'index.html', 'content_type': 'text/html'},
            {'filename': 'assets/main.js', 'content_type': 'application/javascript'},
        ],
    }

    with patch('mcp_deploy_frontend.DEFAULT_ACCOUNT_ID', '190825685292'):
        result = _call_presigned(args)

    assert result['status'] == 'ready'
    assert 'request_id' in result
    assert len(result['presigned_urls']) == 2
    assert result['presigned_urls'][0]['filename'] == 'index.html'
    assert result['presigned_urls'][0]['presigned_url'] == 'https://s3.amazonaws.com/test-presigned-url'
    assert 'frontend/ztp-files/' in result['presigned_urls'][0]['s3_key']
    assert 'expires_at' in result
    assert result['staging_bucket'] == 'bouncer-uploads-190825685292'


# ---------------------------------------------------------------------------
# Test 2: Request presigned - invalid project
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend._get_project_config')
def test_request_presigned_invalid_project(mock_get_project_config):
    """Step 1: Unknown project should return error"""
    mock_get_project_config.return_value = None

    args = {
        'project': 'unknown-project',
        'files': [{'filename': 'index.html'}],
    }

    result = _call_presigned(args)

    assert result['status'] == 'error'
    assert 'Unknown project' in result['error']


# ---------------------------------------------------------------------------
# Test 3: Request presigned - blocked extension
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend._get_project_config')
def test_request_presigned_blocked_extension(mock_get_project_config):
    """Step 1: Blocked file extensions should be rejected"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }

    args = {
        'project': 'ztp-files',
        'files': [{'filename': 'malware.exe'}],
    }

    result = _call_presigned(args)

    assert result['status'] == 'error'
    assert 'Blocked file extension' in result['error']


# ---------------------------------------------------------------------------
# Test 4: Request presigned - duplicate filename
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend._get_project_config')
def test_request_presigned_duplicate_filename(mock_get_project_config):
    """Step 1: Duplicate filenames should be rejected"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }

    args = {
        'project': 'ztp-files',
        'files': [
            {'filename': 'index.html'},
            {'filename': 'index.html'},
        ],
    }

    result = _call_presigned(args)

    assert result['status'] == 'error'
    assert 'Duplicate filename' in result['error']


# ---------------------------------------------------------------------------
# Test 5: Request presigned - empty files
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend._get_project_config')
def test_request_presigned_empty_files(mock_get_project_config):
    """Step 1: Empty files array should be rejected"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }

    args = {
        'project': 'ztp-files',
        'files': [],
    }

    result = _call_presigned(args)

    assert result['status'] == 'error'
    assert 'files is required' in result['error']


# ---------------------------------------------------------------------------
# Test 6: Confirm deploy - success
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_deploy_frontend_notification')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_confirm_deploy_success(
    mock_get_project_config,
    mock_get_frontend_config,
    mock_get_s3_client,
    mock_table,
    mock_send_notification,
):
    """Step 2: Verify all files uploaded successfully"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
        'deploy_role_arn': 'arn:aws:iam::123456789012:role/deploy',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
        'region': 'us-east-1',
        'deploy_role_arn': 'arn:aws:iam::123456789012:role/deploy',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_get_s3_client.return_value = mock_s3

    mock_table.put_item = MagicMock()
    mock_send_notification.return_value = None

    args = {
        'request_id': 'fepre-abc123',
        'project': 'ztp-files',
        'files': [
            {'filename': 'index.html'},
            {'filename': 'assets/main.js'},
        ],
        'reason': 'Deploy new version',
        'source': 'test-agent',
    }

    result = _call_confirm(args)

    assert result['status'] == 'pending_approval'
    assert 'request_id' in result
    assert result['file_count'] == 2
    mock_table.put_item.assert_called_once()
    mock_send_notification.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: Confirm deploy - missing files
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_confirm_deploy_missing_files(
    mock_get_project_config,
    mock_get_frontend_config,
    mock_get_s3_client,
):
    """Step 2: Missing files in staging should return error"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = Exception('NoSuchKey')
    mock_get_s3_client.return_value = mock_s3

    args = {
        'request_id': 'fepre-abc123',
        'project': 'ztp-files',
        'files': [{'filename': 'missing.html'}],
    }

    result = _call_confirm(args)

    assert result['status'] == 'error'
    assert 'Missing files in staging' in result['error']
    assert 'missing.html' in result['error']


# ---------------------------------------------------------------------------
# Test 8: Confirm deploy - unknown project
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend._get_project_config')
def test_confirm_deploy_unknown_project(mock_get_project_config):
    """Step 2: Unknown project should return error"""
    mock_get_project_config.return_value = None

    args = {
        'request_id': 'fepre-abc123',
        'project': 'unknown-project',
        'files': [{'filename': 'index.html'}],
    }

    result = _call_confirm(args)

    assert result['status'] == 'error'
    assert 'Unknown project' in result['error']


# ---------------------------------------------------------------------------
# Test 9: Confirm deploy - DDB write failure
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_confirm_deploy_ddb_write_failure(
    mock_get_project_config,
    mock_get_frontend_config,
    mock_get_s3_client,
    mock_table,
):
    """Step 2: DDB put_item failure should return error"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_get_s3_client.return_value = mock_s3

    mock_table.put_item.side_effect = Exception('DDB error')

    args = {
        'request_id': 'fepre-abc123',
        'project': 'ztp-files',
        'files': [{'filename': 'index.html'}],
    }

    result = _call_confirm(args)

    assert result['status'] == 'error'
    assert 'Failed to create approval record' in result['error']


# ---------------------------------------------------------------------------
# Test 10: Confirm deploy - notification failure
# ---------------------------------------------------------------------------

@patch('mcp_deploy_frontend.send_deploy_frontend_notification')
@patch('mcp_deploy_frontend.table')
@patch('mcp_deploy_frontend.get_s3_client')
@patch('mcp_deploy_frontend._get_frontend_config')
@patch('mcp_deploy_frontend._get_project_config')
def test_confirm_deploy_notification_failure(
    mock_get_project_config,
    mock_get_frontend_config,
    mock_get_s3_client,
    mock_table,
    mock_send_notification,
):
    """Step 2: Notification failure should cleanup DDB and return error"""
    mock_get_project_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }
    mock_get_frontend_config.return_value = {
        'frontend_bucket': 'test-frontend-bucket',
        'distribution_id': 'E1234567890ABC',
    }

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {'ContentLength': 1024}
    mock_get_s3_client.return_value = mock_s3

    mock_table.put_item = MagicMock()
    mock_table.delete_item = MagicMock()
    mock_send_notification.side_effect = Exception('Notification failed')

    args = {
        'request_id': 'fepre-abc123',
        'project': 'ztp-files',
        'files': [{'filename': 'index.html'}],
    }

    result = _call_confirm(args)

    assert result['status'] == 'error'
    assert 'Failed to send notification' in result['error']
    mock_table.delete_item.assert_called_once()
