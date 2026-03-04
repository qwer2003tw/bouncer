"""
Tests for sprint11-012: deploy_frontend Phase B integration tests
Verifies exact execute_command call arguments for S3 copy and CF invalidation,
as well as _is_execute_failed detection for AWS CLI error format.

Gap-filling tests not present in test_mcp_deploy_frontend_phase_b.py:
 - Full command string format (all flags together per file)
 - CF invalidation command format (distribution-id + paths)
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


def _patch_all(s3_side_effect=None, cf_side_effect=None):
    """Build mock_execute with side effects to simulate S3 + CF calls."""
    mock_table = MagicMock()
    call_state = {'s3_call_index': 0}

    def _execute_side_effect(cmd):
        if 'cloudfront create-invalidation' in cmd:
            if cf_side_effect is not None:
                return cf_side_effect
            return '{}'
        # S3 cp call
        idx = call_state['s3_call_index']
        call_state['s3_call_index'] += 1
        if s3_side_effect is not None:
            if callable(s3_side_effect):
                return s3_side_effect(cmd)
            elif isinstance(s3_side_effect, list):
                return s3_side_effect[idx] if idx < len(s3_side_effect) else 'upload: s3://...'
            return str(s3_side_effect)
        return 'upload: s3://...'

    mock_execute = MagicMock(side_effect=_execute_side_effect)
    return mock_execute, mock_table


# ---------------------------------------------------------------------------
# S3 Copy Command Format — Integration Tests
# ---------------------------------------------------------------------------

class TestS3CopyCmdFormat:
    """Verify that every flag in the s3 cp command is present and correct."""

    def _get_s3_calls(self, item=None):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=item)
        return [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]

    def test_s3_cp_command_starts_with_aws_s3_cp(self):
        """Each s3 command starts with 'aws s3 cp'"""
        s3_calls = self._get_s3_calls()
        for c in s3_calls:
            assert c[0][0].startswith('aws s3 cp'), \
                f"Expected 'aws s3 cp', got: {c[0][0][:30]}"

    def test_s3_cp_includes_correct_staging_source(self):
        """Source path = s3://{staging_bucket}/{staged_key}"""
        s3_calls = self._get_s3_calls()
        for c, fm in zip(s3_calls, _FILES_MANIFEST):
            cmd = c[0][0]
            expected_src = f"s3://{_STAGING_BUCKET}/{fm['s3_key']}"
            assert expected_src in cmd, \
                f"Expected source '{expected_src}' in cmd: {cmd}"

    def test_s3_cp_includes_correct_target_bucket(self):
        """Target path = s3://{frontend_bucket}/{filename}"""
        s3_calls = self._get_s3_calls()
        for c, fm in zip(s3_calls, _FILES_MANIFEST):
            cmd = c[0][0]
            expected_dst = f"s3://{_FRONTEND_BUCKET}/{fm['filename']}"
            assert expected_dst in cmd, \
                f"Expected target '{expected_dst}' in cmd: {cmd}"

    def test_s3_cp_includes_content_type_flag(self):
        """--content-type flag with correct MIME type for each file"""
        s3_calls = self._get_s3_calls()
        for c, fm in zip(s3_calls, _FILES_MANIFEST):
            cmd = c[0][0]
            assert '--content-type' in cmd, f"Missing --content-type in: {cmd}"
            assert fm['content_type'] in cmd, \
                f"Expected content-type '{fm['content_type']}' in cmd: {cmd}"

    def test_s3_cp_includes_cache_control_flag(self):
        """--cache-control flag with correct value for each file"""
        s3_calls = self._get_s3_calls()
        for c, fm in zip(s3_calls, _FILES_MANIFEST):
            cmd = c[0][0]
            assert '--cache-control' in cmd, f"Missing --cache-control in: {cmd}"
            # cache_control value appears in the command
            assert fm['cache_control'] in cmd, \
                f"Expected cache-control '{fm['cache_control']}' in cmd: {cmd}"

    def test_s3_cp_includes_metadata_directive_replace(self):
        """--metadata-directive REPLACE must be in every s3 cp command"""
        s3_calls = self._get_s3_calls()
        for c in s3_calls:
            cmd = c[0][0]
            assert '--metadata-directive' in cmd, f"Missing --metadata-directive in: {cmd}"
            assert 'REPLACE' in cmd, f"Missing REPLACE in: {cmd}"

    def test_s3_cp_includes_region_flag(self):
        """--region us-east-1 must be present"""
        s3_calls = self._get_s3_calls()
        for c in s3_calls:
            cmd = c[0][0]
            assert '--region' in cmd, f"Missing --region in: {cmd}"
            assert 'us-east-1' in cmd, f"Missing 'us-east-1' in: {cmd}"

    def test_s3_cp_html_has_no_cache_control(self):
        """index.html gets no-cache, immutable assets get max-age"""
        s3_calls = self._get_s3_calls()
        html_cmd = s3_calls[0][0][0]  # first file is index.html
        assert 'no-cache' in html_cmd

        js_cmd = s3_calls[1][0][0]  # second file is .js
        assert 'max-age=31536000' in js_cmd
        assert 'immutable' in js_cmd


# ---------------------------------------------------------------------------
# CloudFront Invalidation Command Format
# ---------------------------------------------------------------------------

class TestCFInvalidationCmdFormat:
    """Verify the exact CF invalidation command structure."""

    def _get_cf_calls(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve')
        return [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]

    def test_cf_invalidation_command_starts_correctly(self):
        """CF command starts with 'aws cloudfront create-invalidation'"""
        cf_calls = self._get_cf_calls()
        assert len(cf_calls) == 1
        cmd = cf_calls[0][0][0]
        assert cmd.startswith('aws cloudfront create-invalidation'), \
            f"Unexpected CF command start: {cmd[:50]}"

    def test_cf_invalidation_includes_distribution_id(self):
        """CF command includes --distribution-id {_DISTRIBUTION_ID}"""
        cf_calls = self._get_cf_calls()
        cmd = cf_calls[0][0][0]
        assert '--distribution-id' in cmd, f"Missing --distribution-id in: {cmd}"
        assert _DISTRIBUTION_ID in cmd, \
            f"Expected distribution-id '{_DISTRIBUTION_ID}' in: {cmd}"

    def test_cf_invalidation_paths_wildcard(self):
        """CF command includes --paths with wildcard '/*'"""
        cf_calls = self._get_cf_calls()
        cmd = cf_calls[0][0][0]
        assert '--paths' in cmd, f"Missing --paths in: {cmd}"
        assert '/*' in cmd, f"Missing '/*' wildcard in: {cmd}"

    def test_cf_invalidation_includes_region(self):
        """CF command includes --region"""
        cf_calls = self._get_cf_calls()
        cmd = cf_calls[0][0][0]
        assert '--region' in cmd, f"Missing --region in: {cmd}"

    def test_cf_not_called_when_all_files_fail(self):
        """CF invalidation must NOT be called when all S3 copies fail"""
        mock_execute, mock_table = _patch_all(
            s3_side_effect=lambda cmd: 'An error occurred (AccessDenied): (exit code: 255)'
        )
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve')
        cf_calls = [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]
        assert len(cf_calls) == 0, "CF invalidation should not be called when all files fail"


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
        """❌ prefix (Bouncer-formatted) is treated as failure"""
        from callbacks import _is_execute_failed
        assert _is_execute_failed('❌ S3 access denied') is True

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
        """When execute_command returns AWS CLI error, file goes into failed list"""
        aws_error = (
            'An error occurred (NoSuchBucket) when calling the CopyObject operation: '
            'The specified bucket does not exist (exit code: 255)'
        )
        mock_execute, mock_table = _patch_all(
            s3_side_effect=lambda cmd: aws_error
        )
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')

        body = json.loads(result['body'])
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('failed_count') == len(_FILES_MANIFEST), \
            "All files should be failed when AWS CLI error is returned"
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
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=item)

        progress_texts = [
            c[0][1] for c in mock_update.call_args_list
            if len(c[0]) > 1 and '進度' in c[0][1]
        ]
        # At least one progress call should mention '5/7'
        assert any('5/7' in t for t in progress_texts), \
            f"Expected '5/7' progress update. Got: {progress_texts}"

    def test_progress_update_at_final(self):
        """update_message must be called with '7/7' at the end"""
        item = self._make_7file_item()
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=item)

        progress_texts = [
            c[0][1] for c in mock_update.call_args_list
            if len(c[0]) > 1 and '進度' in c[0][1]
        ]
        assert any('7/7' in t for t in progress_texts), \
            f"Expected '7/7' final progress update. Got: {progress_texts}"

    def test_s3_copy_called_for_all_7_files(self):
        """All 7 files get their own s3 cp command"""
        item = self._make_7file_item()
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=item)

        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        assert len(s3_calls) == 7, f"Expected 7 s3 cp calls, got {len(s3_calls)}"

    def test_cf_called_once_after_all_7_files(self):
        """CF invalidation called once after all files processed"""
        item = self._make_7file_item()
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=item)

        cf_calls = [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]
        assert len(cf_calls) == 1, f"Expected 1 CF invalidation call, got {len(cf_calls)}"
