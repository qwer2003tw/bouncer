"""
Tests for bouncer_request_presigned MCP tool.

Covers:
- Happy path (normal flow)
- Missing required parameters
- expires_in > 3600 validation
- expires_in <= 0 validation
- Filename sanitization (path traversal)
- DynamoDB audit record write
- S3 generate_presigned_url failure
"""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def set_env():
    """Set required environment variables before any imports."""
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ['DEFAULT_ACCOUNT_ID'] = '190825685292'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'fake-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    yield


@pytest.fixture
def mock_aws_resources():
    """Spin up moto-backed DynamoDB table and S3 bucket."""
    with mock_aws():
        # DynamoDB
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
        table.wait_until_exists()

        # S3 staging bucket
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='bouncer-uploads-190825685292')

        yield {'table': table, 's3': s3}


@pytest.fixture
def presigned_module(mock_aws_resources):
    """Import (or reload) mcp_presigned with moto active."""
    # Reload to pick up fresh moto-backed clients
    import importlib
    for mod_name in list(sys.modules.keys()):
        if mod_name in ('mcp_presigned', 'db', 'constants', 'utils'):
            del sys.modules[mod_name]

    import mcp_presigned
    # Point the module's `table` at the moto-backed table
    mcp_presigned.table = mock_aws_resources['table']
    return mcp_presigned


# =============================================================================
# Helper
# =============================================================================

def _parse_result(result: dict) -> dict:
    """Extract inner JSON from an mcp_result response."""
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    return json.loads(content)


def _valid_args(**overrides) -> dict:
    base = {
        'filename': 'assets/pdf.worker.min.mjs',
        'content_type': 'application/javascript',
        'reason': 'deploy ZTP Files frontend',
        'source': 'Private Bot (deploy)',
    }
    base.update(overrides)
    return base


# =============================================================================
# Happy Path
# =============================================================================

class TestHappyPath:
    def test_returns_ready_status(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert data['status'] == 'ready'

    def test_returns_presigned_url(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert 'presigned_url' in data
        assert data['presigned_url'].startswith('https://')

    def test_returns_correct_s3_key_format(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        # key format: YYYY-MM-DD/{request_id}/{filename}
        parts = data['s3_key'].split('/')
        assert len(parts) >= 3
        date_part = parts[0]
        assert len(date_part) == 10  # YYYY-MM-DD
        assert data['s3_key'].endswith('assets/pdf.worker.min.mjs')

    def test_returns_s3_uri(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert data['s3_uri'].startswith('s3://bouncer-uploads-190825685292/')

    def test_returns_request_id(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert 'request_id' in data
        assert len(data['request_id']) > 0

    def test_returns_expires_at_iso(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert 'expires_at' in data
        # ISO 8601 format: 2026-02-25T05:00:00Z
        assert data['expires_at'].endswith('Z')
        assert 'T' in data['expires_at']

    def test_returns_method_put(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert data['method'] == 'PUT'

    def test_returns_content_type_in_headers(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert data['headers']['Content-Type'] == 'application/javascript'

    def test_default_expires_in_900(self, presigned_module):
        """Default expires_in should be 900 seconds."""
        before = int(time.time())
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        after = int(time.time())
        data = _parse_result(result)
        # Parse expires_at and verify roughly 900s in the future
        import datetime
        exp = datetime.datetime.strptime(data['expires_at'], '%Y-%m-%dT%H:%M:%SZ')
        exp_ts = int(exp.replace(tzinfo=datetime.timezone.utc).timestamp())
        assert before + 895 <= exp_ts <= after + 905

    def test_custom_expires_in(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(expires_in=1800)
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'

    def test_custom_account(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(account='111111111111')
        )
        data = _parse_result(result)
        assert 'bouncer-uploads-111111111111' in data['s3_uri']

    def test_simple_filename(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='report.pdf')
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'
        assert data['s3_key'].endswith('report.pdf')


# =============================================================================
# Parameter Validation
# =============================================================================

class TestParameterValidation:
    def test_missing_filename(self, presigned_module):
        args = {k: v for k, v in _valid_args().items() if k != 'filename'}
        result = presigned_module.mcp_tool_request_presigned('req-1', args)
        data = _parse_result(result)
        assert data['status'] == 'error'
        assert 'filename' in data['error'].lower()

    def test_empty_filename(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='')
        )
        data = _parse_result(result)
        assert data['status'] == 'error'

    def test_missing_content_type(self, presigned_module):
        args = {k: v for k, v in _valid_args().items() if k != 'content_type'}
        result = presigned_module.mcp_tool_request_presigned('req-1', args)
        data = _parse_result(result)
        assert data['status'] == 'error'
        assert 'content_type' in data['error'].lower()

    def test_empty_content_type(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(content_type='')
        )
        data = _parse_result(result)
        assert data['status'] == 'error'

    def test_missing_reason(self, presigned_module):
        args = {k: v for k, v in _valid_args().items() if k != 'reason'}
        result = presigned_module.mcp_tool_request_presigned('req-1', args)
        data = _parse_result(result)
        assert data['status'] == 'error'
        assert 'reason' in data['error'].lower()

    def test_empty_reason(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(reason='')
        )
        data = _parse_result(result)
        assert data['status'] == 'error'

    def test_missing_source(self, presigned_module):
        args = {k: v for k, v in _valid_args().items() if k != 'source'}
        result = presigned_module.mcp_tool_request_presigned('req-1', args)
        data = _parse_result(result)
        assert data['status'] == 'error'
        assert 'source' in data['error'].lower()

    def test_empty_source(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(source='')
        )
        data = _parse_result(result)
        assert data['status'] == 'error'

    def test_expires_in_exceeds_3600(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(expires_in=3601)
        )
        data = _parse_result(result)
        assert data['status'] == 'error'
        assert '3600' in data['error']

    def test_expires_in_exactly_3600_is_valid(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(expires_in=3600)
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'

    def test_expires_in_zero_is_invalid(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(expires_in=0)
        )
        data = _parse_result(result)
        assert data['status'] == 'error'

    def test_expires_in_negative_is_invalid(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(expires_in=-1)
        )
        data = _parse_result(result)
        assert data['status'] == 'error'

    def test_expires_in_non_integer_string(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(expires_in='abc')
        )
        data = _parse_result(result)
        assert data['status'] == 'error'


# =============================================================================
# Filename Sanitization
# =============================================================================

class TestFilenameSanitization:
    def test_path_traversal_double_dot(self, presigned_module):
        """../../etc/passwd should be sanitized."""
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='../../etc/passwd')
        )
        data = _parse_result(result)
        # Should succeed but with sanitized name
        assert data['status'] == 'ready'
        assert '..' not in data['s3_key']
        assert 'etc' in data['s3_key'] or 'passwd' in data['s3_key']

    def test_path_traversal_windows_style(self, presigned_module):
        """..\\..\\Windows\\System32 should be sanitized."""
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='..\\..\\Windows\\System32\\cmd.exe')
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'
        assert '..' not in data['s3_key']

    def test_absolute_path_stripped(self, presigned_module):
        """/etc/passwd should not start from root."""
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='/etc/passwd')
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'
        # Should not have absolute path in key
        # s3_key starts with date/request_id, not /
        assert not data['s3_key'].startswith('/')

    def test_null_bytes_removed(self, presigned_module):
        """Null bytes should be stripped."""
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='file\x00name.txt')
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'
        assert '\x00' not in data['s3_key']

    def test_valid_subdirectory_path_preserved(self, presigned_module):
        """assets/subdir/file.js should be preserved."""
        result = presigned_module.mcp_tool_request_presigned(
            'req-1', _valid_args(filename='assets/subdir/file.js')
        )
        data = _parse_result(result)
        assert data['status'] == 'ready'
        assert 'assets/subdir/file.js' in data['s3_key']

    def test_sanitize_standalone_function_traversal(self, presigned_module):
        from mcp_presigned import _sanitize_filename
        assert '..' not in _sanitize_filename('../../secret')
        assert _sanitize_filename('../../secret') != ''

    def test_sanitize_standalone_empty(self, presigned_module):
        from mcp_presigned import _sanitize_filename
        assert _sanitize_filename('') == 'unnamed'
        assert _sanitize_filename('..') == 'unnamed'
        assert _sanitize_filename('/') == 'unnamed'


# =============================================================================
# DynamoDB Audit Record
# =============================================================================

class TestDynamoDBWrite:
    def test_audit_record_written(self, presigned_module, mock_aws_resources):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)
        assert data['status'] == 'ready'

        # Verify DynamoDB record
        table = mock_aws_resources['table']
        item = table.get_item(Key={'request_id': data['request_id']}).get('Item')
        assert item is not None
        assert item['action'] == 'presigned_upload'
        assert item['status'] == 'url_issued'

    def test_audit_record_has_correct_fields(self, presigned_module, mock_aws_resources):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)

        table = mock_aws_resources['table']
        item = table.get_item(Key={'request_id': data['request_id']}).get('Item')

        assert item['filename'] is not None
        assert item['s3_key'] == data['s3_key']
        assert item['bucket'] == 'bouncer-uploads-190825685292'
        assert item['content_type'] == 'application/javascript'
        assert item['source'] == 'Private Bot (deploy)'
        assert item['reason'] == 'deploy ZTP Files frontend'
        assert 'expires_at' in item
        assert 'created_at' in item
        assert 'ttl' in item

    def test_audit_record_ttl_in_future(self, presigned_module, mock_aws_resources):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        data = _parse_result(result)

        table = mock_aws_resources['table']
        item = table.get_item(Key={'request_id': data['request_id']}).get('Item')
        assert int(item['ttl']) > int(time.time())

    def test_audit_failure_does_not_block_response(self, presigned_module):
        """Even if DynamoDB write fails, the presigned URL should still be returned."""
        with patch.object(presigned_module.table, 'put_item', side_effect=Exception('DDB error')):
            result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
            data = _parse_result(result)
            # Should still return success
            assert data['status'] == 'ready'
            assert 'presigned_url' in data


# =============================================================================
# S3 Generate Presigned URL Failure
# =============================================================================

class TestS3Failure:
    def test_s3_generate_failure_returns_error(self, presigned_module):
        """When S3 generate_presigned_url raises, return error."""
        with patch('mcp_presigned.boto3') as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3
            mock_s3.generate_presigned_url.side_effect = Exception('S3 connection refused')

            result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
            data = _parse_result(result)
            assert data['status'] == 'error'
            assert 'presigned' in data['error'].lower() or 'S3' in data['error'] or 'failed' in data['error'].lower()

    def test_s3_failure_includes_error_detail(self, presigned_module):
        with patch('mcp_presigned.boto3') as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3
            mock_s3.generate_presigned_url.side_effect = RuntimeError('NoSuchBucket')

            result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
            data = _parse_result(result)
            assert 'NoSuchBucket' in data['error']


# =============================================================================
# Integration: MCP Response Format
# =============================================================================

class TestMCPResponseFormat:
    def test_http_200_status_code(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        assert result['statusCode'] == 200

    def test_jsonrpc_envelope(self, presigned_module):
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args())
        body = json.loads(result['body'])
        assert body['jsonrpc'] == '2.0'
        assert body['id'] == 'req-1'
        assert 'result' in body

    def test_error_response_is_error_true(self, presigned_module):
        """Error responses should have isError: True in result."""
        result = presigned_module.mcp_tool_request_presigned('req-1', _valid_args(filename=''))
        body = json.loads(result['body'])
        assert body['result'].get('isError') is True
