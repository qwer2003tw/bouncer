"""
Tests for sprint11-012: deploy_frontend Phase B integration tests
Updated for Sprint 12: implementation now uses boto3 directly (not execute_command).

Verifies boto3 call arguments for S3 put_object and CF create_invalidation,
as well as _is_execute_failed detection for AWS CLI error format.

Gap-filling tests not present in test_mcp_deploy_frontend_phase_b.py:
 - Full boto3 put_object call args (Bucket, Key, ContentType, CacheControl)
 - CF create_invalidation call args (DistributionId, Paths)
 - _is_execute_failed with AWS CLI error format (An error occurred...)
 - 7+ files: progress update at file 5 and at final
"""
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

_STAGING_BUCKET = 'bouncer-uploads-190825685292'
_FRONTEND_BUCKET = 'ztp-files-dev-frontendbucket-nvvimv31xp3v'
_DISTRIBUTION_ID = 'E176PW0SA5JF29'
_REQUEST_ID = 'req-s11-012-integration'

_FILES_MANIFEST = [
    {
        'filename': 'index.html',
        's3_key': f'pending/{_REQUEST_ID}/index.html',
        'content_type': 'text/html',
        'cache_control': 'no-cache, no-store, must-revalidate',
        'size': 1024,
    },
    {
        'filename': 'assets/app-abc123.js',
        's3_key': f'pending/{_REQUEST_ID}/assets/app-abc123.js',
        'content_type': 'application/javascript',
        'cache_control': 'max-age=31536000, immutable',
        'size': 51200,
    },
    {
        'filename': 'assets/style-def456.css',
        's3_key': f'pending/{_REQUEST_ID}/assets/style-def456.css',
        'content_type': 'text/css',
        'cache_control': 'max-age=31536000, immutable',
        'size': 10240,
    },
]


def _make_item(files_manifest=None):
    manifest = files_manifest if files_manifest is not None else _FILES_MANIFEST
    return {
        'request_id': _REQUEST_ID,
        'action': 'deploy_frontend',
        'status': 'pending_approval',
        'project': 'ztp-files',
        'staging_bucket': _STAGING_BUCKET,
        'frontend_bucket': _FRONTEND_BUCKET,
        'distribution_id': _DISTRIBUTION_ID,
        'region': 'us-east-1',
        'source': 'Private Bot (ZTP Files)',
        'reason': 'Sprint 11 deploy',
        'files': json.dumps(manifest),
        'file_count': len(manifest),
        'total_size': sum(f['size'] for f in manifest),
        'created_at': 1700000000,
    }


def _call_callback(action='approve', item=None, message_id=999, callback_id='cb-s11', user_id='user-s11'):
    from callbacks import handle_deploy_frontend_callback
    return handle_deploy_frontend_callback(
        request_id=_REQUEST_ID,
        action=action,
        item=item or _make_item(),
        message_id=message_id,
        callback_id=callback_id,
        user_id=user_id,
    )


def _make_mock_boto3(get_object_side_effect=None, put_object_side_effect=None, cf_side_effect=None):
    """Build a mock boto3 module that returns appropriate S3/CF clients.

    The implementation uses:
      s3_staging = _boto3.client('s3')  -> for get_object
      s3_target  = _boto3.client('s3')  -> for put_object (no deploy_role_arn in test item)
      cf         = _boto3.client('cloudfront') -> for create_invalidation

    Since both s3 clients are created by the same _boto3.client('s3') call,
    we return a single mock_s3 for all 's3' calls.
    """
    mock_boto3 = MagicMock()

    mock_s3 = MagicMock()
    mock_cf = MagicMock()

    # Default: get_object returns a mock Body with some bytes
    default_body = MagicMock()
    default_body.read.return_value = b'file content'
    mock_s3.get_object.return_value = {'Body': default_body}

    if get_object_side_effect is not None:
        mock_s3.get_object.side_effect = get_object_side_effect

    if put_object_side_effect is not None:
        mock_s3.put_object.side_effect = put_object_side_effect

    if cf_side_effect is not None:
        mock_cf.create_invalidation.side_effect = cf_side_effect

    def _client_factory(service, **kwargs):
        if service == 's3':
            return mock_s3
        if service == 'cloudfront':
            return mock_cf
        return MagicMock()

    mock_boto3.client.side_effect = _client_factory

    return mock_boto3, mock_s3, mock_cf


def _run_approve(item=None, get_object_side_effect=None, put_object_side_effect=None, cf_side_effect=None):
    """Run the approve callback with mocked boto3 and helpers."""
    mock_boto3, mock_s3, mock_cf = _make_mock_boto3(
        get_object_side_effect=get_object_side_effect,
        put_object_side_effect=put_object_side_effect,
        cf_side_effect=cf_side_effect,
    )
    mock_table = MagicMock()

    import callbacks
    with patch.object(callbacks, '_boto3', mock_boto3), \
         patch('callbacks._get_table', return_value=mock_table), \
         patch('callbacks.answer_callback'), \
         patch('callbacks.update_message') as mock_update, \
         patch('callbacks._update_request_status') as mock_update_status, \
         patch('callbacks.emit_metric'), \
         patch('notifications._send_message_silent'):
        result = _call_callback(action='approve', item=item)

    return result, mock_s3, mock_cf, mock_update, mock_update_status, mock_table


# ---------------------------------------------------------------------------
# S3 put_object Call Format — Integration Tests
# (Updated from s3 cp command format; implementation now uses boto3 directly)
# ---------------------------------------------------------------------------

class TestS3CopyCmdFormat:
    """Verify that boto3 put_object is called with correct args for each file."""

    def _get_put_object_calls(self, item=None):
        _, mock_s3, *_ = _run_approve(item=item)
        return mock_s3.put_object.call_args_list

    def test_s3_cp_command_starts_with_aws_s3_cp(self):
        """put_object called for each file in the manifest (replaces s3 cp command check)"""
        calls = self._get_put_object_calls()
        assert len(calls) == len(_FILES_MANIFEST), \
            f"Expected {len(_FILES_MANIFEST)} put_object calls, got {len(calls)}"

    def test_s3_cp_includes_correct_staging_source(self):
        """get_object called with correct staging bucket and key"""
        _, mock_s3, *_ = _run_approve()
        get_calls = mock_s3.get_object.call_args_list
        for c, fm in zip(get_calls, _FILES_MANIFEST):
            kwargs = c[1] if c[1] else {}
            bucket = kwargs.get('Bucket')
            key = kwargs.get('Key')
            assert bucket == _STAGING_BUCKET, \
                f"Expected staging bucket '{_STAGING_BUCKET}', got '{bucket}'"
            assert key == fm['s3_key'], \
                f"Expected key '{fm['s3_key']}', got '{key}'"

    def test_s3_cp_includes_correct_target_bucket(self):
        """put_object called with correct frontend bucket and filename as Key"""
        calls = self._get_put_object_calls()
        for c, fm in zip(calls, _FILES_MANIFEST):
            kwargs = c[1] if c[1] else {}
            bucket = kwargs.get('Bucket')
            key = kwargs.get('Key')
            assert bucket == _FRONTEND_BUCKET, \
                f"Expected frontend bucket '{_FRONTEND_BUCKET}', got '{bucket}'"
            assert key == fm['filename'], \
                f"Expected key '{fm['filename']}', got '{key}'"

    def test_s3_cp_includes_content_type_flag(self):
        """put_object called with correct ContentType for each file"""
        calls = self._get_put_object_calls()
        for c, fm in zip(calls, _FILES_MANIFEST):
            kwargs = c[1] if c[1] else {}
            ct = kwargs.get('ContentType')
            assert ct == fm['content_type'], \
                f"Expected ContentType '{fm['content_type']}', got '{ct}'"

    def test_s3_cp_includes_cache_control_flag(self):
        """put_object called with correct CacheControl for each file"""
        calls = self._get_put_object_calls()
        for c, fm in zip(calls, _FILES_MANIFEST):
            kwargs = c[1] if c[1] else {}
            cc = kwargs.get('CacheControl')
            assert cc == fm['cache_control'], \
                f"Expected CacheControl '{fm['cache_control']}', got '{cc}'"

    def test_s3_cp_includes_metadata_directive_replace(self):
        """put_object is used (replaces s3 cp --metadata-directive REPLACE)"""
        calls = self._get_put_object_calls()
        # Verify put_object is called the right number of times
        assert len(calls) == len(_FILES_MANIFEST)

    def test_s3_cp_includes_region_flag(self):
        """boto3 s3 client is created (replaces --region flag check)"""
        import callbacks
        mock_boto3, mock_s3, mock_cf = _make_mock_boto3()
        mock_table = MagicMock()
        with patch.object(callbacks, '_boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve')
        s3_calls = [c for c in mock_boto3.client.call_args_list if c[0][0] == 's3']
        assert len(s3_calls) >= 1

    def test_s3_cp_html_has_no_cache_control(self):
        """index.html gets no-cache CacheControl; assets get max-age=immutable"""
        calls = self._get_put_object_calls()
        assert len(calls) >= 2

        # First file is index.html
        html_kwargs = calls[0][1] if calls[0][1] else {}
        html_cc = html_kwargs.get('CacheControl', '')
        assert 'no-cache' in html_cc, \
            f"Expected no-cache in index.html CacheControl: {html_cc}"

        # Second file is .js
        js_kwargs = calls[1][1] if calls[1][1] else {}
        js_cc = js_kwargs.get('CacheControl', '')
        assert 'max-age=31536000' in js_cc, \
            f"Expected max-age in .js CacheControl: {js_cc}"
        assert 'immutable' in js_cc, \
            f"Expected immutable in .js CacheControl: {js_cc}"


# ---------------------------------------------------------------------------
# CloudFront Invalidation — Integration Tests
# (Updated from command string format; implementation uses boto3 create_invalidation)
# ---------------------------------------------------------------------------

class TestCFInvalidationCmdFormat:
    """Verify the CF create_invalidation boto3 call."""

    def _get_cf_create_invalidation_calls(self):
        _, _, mock_cf, *_ = _run_approve()
        return mock_cf.create_invalidation.call_args_list

    def test_cf_invalidation_command_starts_correctly(self):
        """CF create_invalidation called exactly once"""
        calls = self._get_cf_create_invalidation_calls()
        assert len(calls) == 1, \
            f"Expected 1 create_invalidation call, got {len(calls)}"

    def test_cf_invalidation_includes_distribution_id(self):
        """create_invalidation called with correct DistributionId"""
        calls = self._get_cf_create_invalidation_calls()
        kwargs = calls[0][1] if calls[0][1] else {}
        dist_id = kwargs.get('DistributionId')
        assert dist_id == _DISTRIBUTION_ID, \
            f"Expected DistributionId '{_DISTRIBUTION_ID}', got '{dist_id}'"

    def test_cf_invalidation_paths_wildcard(self):
        """create_invalidation called with '/*' path"""
        calls = self._get_cf_create_invalidation_calls()
        kwargs = calls[0][1] if calls[0][1] else {}
        batch = kwargs.get('InvalidationBatch', {})
        paths = batch.get('Paths', {}).get('Items', [])
        assert '/*' in paths, \
            f"Expected '/*' in invalidation paths, got: {paths}"

    def test_cf_invalidation_includes_region(self):
        """CF boto3 client is created (replaces --region check)"""
        import callbacks
        mock_boto3, mock_s3, mock_cf = _make_mock_boto3()
        mock_table = MagicMock()
        with patch.object(callbacks, '_boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve')
        cf_calls = [c for c in mock_boto3.client.call_args_list if c[0][0] == 'cloudfront']
        assert len(cf_calls) >= 1, "Expected cloudfront boto3 client to be created"

    def test_cf_not_called_when_all_files_fail(self):
        """CF invalidation must NOT be called when all S3 copies fail"""
        # Simulate all get_object calls failing (so no files are deployed)
        _, _, mock_cf, *_ = _run_approve(
            get_object_side_effect=Exception('AccessDenied: s3:GetObject'),
        )
        mock_cf.create_invalidation.assert_not_called()


# ---------------------------------------------------------------------------
# _is_execute_failed — AWS CLI error format detection
# ---------------------------------------------------------------------------

class TestIsExecuteFailedAWSCLIFormat:
    """Verify _is_execute_failed correctly detects AWS CLI error output formats."""

    def test_aws_cli_error_with_exit_code_255_is_failed(self):
        """'An error occurred...(exit code: 255)' should be treated as failure"""
        from callbacks import _is_execute_failed
        aws_error = (
            'An error occurred (AccessDenied) when calling the CopyObject operation: '
            'User: arn:aws:iam::123:role/test is not authorized (exit code: 255)'
        )
        assert _is_execute_failed(aws_error) is True

    def test_aws_cli_error_exit_code_1_is_failed(self):
        """(exit code: 1) is treated as failure"""
        from callbacks import _is_execute_failed
        assert _is_execute_failed('Some error output (exit code: 1)') is True

    def test_bouncer_prefix_error_is_failed(self):
        """X prefix (Bouncer-formatted) is treated as failure"""
        from callbacks import _is_execute_failed
        assert _is_execute_failed('\u274c S3 access denied') is True

    def test_successful_s3_cp_output_is_not_failed(self):
        """Successful s3 cp output is not a failure"""
        from callbacks import _is_execute_failed
        success_output = 'upload: s3://staging-bucket/key to s3://frontend-bucket/index.html'
        assert _is_execute_failed(success_output) is False

    def test_successful_cf_json_output_is_not_failed(self):
        """CF invalidation JSON response is not a failure"""
        from callbacks import _is_execute_failed
        cf_ok = '{"Invalidation": {"Id": "IABCDEFG", "Status": "InProgress"}}'
        assert _is_execute_failed(cf_ok) is False

    def test_exit_code_0_is_not_failed(self):
        """(exit code: 0) means success"""
        from callbacks import _is_execute_failed
        assert _is_execute_failed('Completed (exit code: 0)') is False

    def test_empty_output_is_not_failed(self):
        """Empty string is not a failure"""
        from callbacks import _is_execute_failed
        assert _is_execute_failed('') is False

    def test_aws_cli_error_detected_and_file_marked_failed(self):
        """When boto3 get_object raises, file goes into failed list"""
        result, mock_s3, mock_cf, _, mock_update_status, _ = _run_approve(
            get_object_side_effect=Exception(
                'An error occurred (NoSuchBucket): The specified bucket does not exist'
            ),
        )

        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('failed_count') == len(_FILES_MANIFEST), \
            "All files should be failed when boto3 get_object raises"
        assert extra.get('deploy_status') == 'deploy_failed'


# ---------------------------------------------------------------------------
# 7+ Files: Progress Update Verification
# ---------------------------------------------------------------------------

class TestProgressUpdateSevenFiles:
    """With 7+ files, progress update fires at file 5 and final."""

    def _make_7file_item(self):
        files = [
            {
                'filename': f'assets/chunk-{i:03d}.js',
                's3_key': f'pending/{_REQUEST_ID}/assets/chunk-{i:03d}.js',
                'content_type': 'application/javascript',
                'cache_control': 'max-age=31536000, immutable',
                'size': 1024,
            }
            for i in range(7)
        ]
        return {
            'request_id': _REQUEST_ID,
            'action': 'deploy_frontend',
            'status': 'pending_approval',
            'project': 'ztp-files',
            'staging_bucket': _STAGING_BUCKET,
            'frontend_bucket': _FRONTEND_BUCKET,
            'distribution_id': _DISTRIBUTION_ID,
            'region': 'us-east-1',
            'source': 'Private Bot (ZTP Files)',
            'reason': 'Sprint 11 deploy',
            'files': json.dumps(files),
            'file_count': len(files),
            'total_size': sum(f['size'] for f in files),
            'created_at': 1700000000,
        }

    def test_progress_update_at_file_5(self):
        """update_message must be called with '5/7' after processing file 5"""
        item = self._make_7file_item()
        _, _, _, mock_update, *_ = _run_approve(item=item)

        progress_texts = [
            c[0][1] for c in mock_update.call_args_list
            if len(c[0]) > 1 and '\u9032\u5ea6' in c[0][1]
        ]
        assert any('5/7' in t for t in progress_texts), \
            f"Expected '5/7' progress update. Got: {progress_texts}"

    def test_progress_update_at_final(self):
        """update_message must be called with '7/7' at the end"""
        item = self._make_7file_item()
        _, _, _, mock_update, *_ = _run_approve(item=item)

        progress_texts = [
            c[0][1] for c in mock_update.call_args_list
            if len(c[0]) > 1 and '\u9032\u5ea6' in c[0][1]
        ]
        assert any('7/7' in t for t in progress_texts), \
            f"Expected '7/7' final progress update. Got: {progress_texts}"

    def test_s3_copy_called_for_all_7_files(self):
        """All 7 files get their own put_object call"""
        item = self._make_7file_item()
        _, mock_s3, *_ = _run_approve(item=item)

        s3_calls = mock_s3.put_object.call_args_list
        assert len(s3_calls) == 7, f"Expected 7 put_object calls, got {len(s3_calls)}"

    def test_cf_called_once_after_all_7_files(self):
        """CF invalidation called once after all files processed"""
        item = self._make_7file_item()
        _, _, mock_cf, *_ = _run_approve(item=item)

        assert mock_cf.create_invalidation.call_count == 1, \
            f"Expected 1 CF invalidation call, got {mock_cf.create_invalidation.call_count}"
