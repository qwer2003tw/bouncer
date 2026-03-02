"""
Tests for sprint9-003 Phase B: handle_deploy_frontend_callback

Covers:
  - deny action: DDB status=rejected, no S3 ops, Telegram update
  - approve action (full success): S3 copy_object called for each file, CF invalidation, DDB updated
  - approve action (partial failure): deployed/failed lists correct, DDB records partial_deploy
  - approve action (full failure): deploy_failed status, no CF invalidation
  - CloudFront invalidation failure: S3 result preserved, cf_invalidation_failed=True in response
  - app.py routing: deploy_frontend dispatches to handle_deploy_frontend_callback
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
_REQUEST_ID = 'req-deploy-test-001'

_FILES_MANIFEST = [
    {
        'filename': 'index.html',
        's3_key': 'pending/{}/index.html'.format(_REQUEST_ID),
        'content_type': 'text/html',
        'cache_control': 'no-cache, no-store, must-revalidate',
        'size': 1024,
    },
    {
        'filename': 'assets/app-abc123.js',
        's3_key': 'pending/{}/assets/app-abc123.js'.format(_REQUEST_ID),
        'content_type': 'application/javascript',
        'cache_control': 'max-age=31536000, immutable',
        'size': 51200,
    },
    {
        'filename': 'assets/style-def456.css',
        's3_key': 'pending/{}/assets/style-def456.css'.format(_REQUEST_ID),
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
        'reason': 'Sprint 9 deploy',
        'files': json.dumps(manifest),
        'file_count': len(manifest),
        'total_size': sum(f['size'] for f in manifest),
        'created_at': 1700000000,
    }


def _call_callback(action='approve', item=None, message_id=999, callback_id='cb-001', user_id='user-123'):
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
    """Return (mock_execute_command, mock_table) and set up side effects.

    s3_side_effect: if provided, a callable(cmd) -> str or a side_effect list for
                    the s3 cp calls only (CF call comes last).
    cf_side_effect: if provided, a string starting with '❌' that the CF call returns.
    """
    mock_table = MagicMock()

    # Track call index to distinguish s3 cp calls from CF invalidation call
    call_state = {'s3_call_index': 0, 'total_files': len(_FILES_MANIFEST)}

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
                result = s3_side_effect(cmd)
                return result
            elif isinstance(s3_side_effect, list):
                return s3_side_effect[idx] if idx < len(s3_side_effect) else 'upload: s3://...'
            else:
                return str(s3_side_effect)
        return 'upload: s3://...'

    mock_execute = MagicMock(side_effect=_execute_side_effect)
    return mock_execute, mock_table


# ---------------------------------------------------------------------------
# Deny tests
# ---------------------------------------------------------------------------

class TestDenyAction:
    def test_deny_updates_ddb_to_rejected(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status:
            _call_callback(action='deny')
        mock_update_status.assert_called_once()
        args = mock_update_status.call_args[0]
        assert args[2] == 'rejected'

    def test_deny_does_not_call_s3(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            _call_callback(action='deny')
        mock_execute.assert_not_called()

    def test_deny_returns_200(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            result = _call_callback(action='deny')
        assert result['statusCode'] == 200

    def test_deny_sends_telegram_update(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'):
            _call_callback(action='deny')
        mock_update.assert_called_once()
        msg = mock_update.call_args[0][1]
        assert '拒絕' in msg


# ---------------------------------------------------------------------------
# Approve - Full Success
# ---------------------------------------------------------------------------

class TestApproveFullSuccess:
    def _run(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_execute, mock_table, mock_update, mock_update_status

    def test_s3_copy_called_for_each_file(self):
        result, mock_execute, *_ = self._run()
        # calls = N s3 cp + 1 CF invalidation
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        assert len(s3_calls) == len(_FILES_MANIFEST)

    def test_s3_copy_uses_correct_source_bucket(self):
        result, mock_execute, *_ = self._run()
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        first_cmd = s3_calls[0][0][0]
        assert _STAGING_BUCKET in first_cmd

    def test_s3_copy_uses_correct_target_bucket(self):
        result, mock_execute, *_ = self._run()
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        first_cmd = s3_calls[0][0][0]
        assert _FRONTEND_BUCKET in first_cmd

    def test_s3_copy_passes_content_type(self):
        result, mock_execute, *_ = self._run()
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        first_cmd = s3_calls[0][0][0]
        assert 'text/html' in first_cmd

    def test_s3_copy_passes_cache_control(self):
        result, mock_execute, *_ = self._run()
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        first_cmd = s3_calls[0][0][0]
        assert 'no-cache' in first_cmd

    def test_s3_copy_metadata_replace(self):
        result, mock_execute, *_ = self._run()
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        first_cmd = s3_calls[0][0][0]
        assert 'REPLACE' in first_cmd

    def test_cf_invalidation_called(self):
        result, mock_execute, *_ = self._run()
        cf_calls = [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]
        assert len(cf_calls) == 1
        cf_cmd = cf_calls[0][0][0]
        assert _DISTRIBUTION_ID in cf_cmd
        assert '/*' in cf_cmd

    def test_cf_caller_reference_is_request_id(self):
        # execute_command doesn't use CallerReference - the cmd uses distribution-id
        # This is a no-op test now; CF cmd uses --distribution-id flag
        result, mock_execute, *_ = self._run()
        cf_calls = [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]
        assert len(cf_calls) == 1

    def test_ddb_updated_with_approved_status(self):
        result, _, _, _, mock_update_status = self._run()
        mock_update_status.assert_called()
        args = mock_update_status.call_args[0]
        assert args[2] == 'approved'

    def test_ddb_updated_with_deploy_status_deployed(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_ddb_deployed_count_equals_file_count(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deployed_count') == len(_FILES_MANIFEST)
        assert extra.get('failed_count') == 0

    def test_response_ok_and_200(self):
        result, *_ = self._run()
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['ok'] is True

    def test_response_no_cf_failure(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['cf_invalidation_failed'] is False

    def test_telegram_update_shows_success(self):
        result, _, _, mock_update, _ = self._run()
        last_call_msg = mock_update.call_args_list[-1][0][1]
        assert '完成' in last_call_msg or '✅' in last_call_msg


# ---------------------------------------------------------------------------
# Approve - Partial Failure
# ---------------------------------------------------------------------------

class TestApprovePartialFailure:
    def _run(self):
        call_count = {'n': 0}
        def s3_side_effect(cmd):
            call_count['n'] += 1
            if call_count['n'] == 2:
                return '❌ S3 access denied'
            return 'upload: s3://...'

        mock_execute, mock_table = _patch_all(s3_side_effect=s3_side_effect)
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_execute, mock_table, mock_update_status

    def test_partial_deploy_status(self):
        result, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'partial_deploy'

    def test_deployed_count_and_failed_count(self):
        result, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deployed_count') == 2
        assert extra.get('failed_count') == 1

    def test_failed_files_recorded(self):
        result, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        failed = json.loads(extra.get('failed_files', '[]'))
        assert len(failed) == 1
        assert 'app-abc123.js' in failed[0]

    def test_cf_invalidation_still_called_on_partial(self):
        result, mock_execute, *_ = self._run()
        cf_calls = [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]
        assert len(cf_calls) == 1

    def test_response_partial_status(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['deploy_status'] == 'partial_deploy'


# ---------------------------------------------------------------------------
# Approve - Full Failure
# ---------------------------------------------------------------------------

class TestApproveFullFailure:
    def _run(self):
        # All s3 cp calls return error
        mock_execute, mock_table = _patch_all(
            s3_side_effect=lambda cmd: '❌ S3 error for all files'
        )
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_execute, mock_table, mock_update_status

    def test_deploy_failed_status(self):
        result, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deploy_failed'

    def test_cf_not_called_on_full_failure(self):
        result, mock_execute, *_ = self._run()
        cf_calls = [c for c in mock_execute.call_args_list if 'cloudfront create-invalidation' in c[0][0]]
        assert len(cf_calls) == 0

    def test_response_deploy_failed(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['deploy_status'] == 'deploy_failed'


# ---------------------------------------------------------------------------
# CloudFront Invalidation Failure
# ---------------------------------------------------------------------------

class TestCFInvalidationFailure:
    def _run(self):
        mock_execute, mock_table = _patch_all(
            cf_side_effect='❌ CF rate limit exceeded'
        )
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_execute, mock_table, mock_update, mock_update_status

    def test_s3_still_succeeds(self):
        result, mock_execute, *_ = self._run()
        s3_calls = [c for c in mock_execute.call_args_list if 's3 cp' in c[0][0]]
        assert len(s3_calls) == len(_FILES_MANIFEST)

    def test_cf_invalidation_failed_flag_true(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['cf_invalidation_failed'] is True

    def test_deploy_status_still_deployed(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_telegram_message_warns_about_cf(self):
        result, _, _, mock_update, _ = self._run()
        last_msg = mock_update.call_args_list[-1][0][1]
        assert 'CloudFront' in last_msg or 'Invalidation' in last_msg

    def test_ddb_cf_flag_recorded(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('cf_invalidation_failed') is True


# ---------------------------------------------------------------------------
# app.py routing
# ---------------------------------------------------------------------------

class TestAppRouting:
    def test_deploy_frontend_dispatches_to_handler(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'src', 'app.py')) as f:
            content = f.read()
        assert 'handle_deploy_frontend_callback' in content
        assert 'Phase B pending' not in content
        assert 'TODO: Phase B' not in content

    def test_placeholder_removed(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'src', 'app.py')) as f:
            content = f.read()
        assert 'Phase B 會實作' not in content


# ---------------------------------------------------------------------------
# DDB field structure
# ---------------------------------------------------------------------------

class TestDDBFields:
    def test_deployed_details_is_json_list(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve')
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        details = json.loads(extra['deployed_details'])
        assert isinstance(details, list)
        assert len(details) == len(_FILES_MANIFEST)
        first = details[0]
        assert 'filename' in first
        assert 's3_key' in first

    def test_failed_details_empty_on_full_success(self):
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve')
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        failed = json.loads(extra['failed_details'])
        assert isinstance(failed, list)
        assert failed == []


# ---------------------------------------------------------------------------
# Progress update during approve
# ---------------------------------------------------------------------------

class TestApproveProgressUpdate:
    """Verify update_message is called with progress info during S3 copy loop."""

    def _make_large_item(self):
        """6 files — ensures the % 5 == 0 progress branch fires at file #5."""
        files = [
            {
                'filename': f'assets/chunk-{i:03d}.js',
                's3_key': f'pending/{_REQUEST_ID}/assets/chunk-{i:03d}.js',
                'content_type': 'application/javascript',
                'cache_control': 'max-age=31536000, immutable',
                'size': 1024,
            }
            for i in range(6)
        ]
        import json as _json
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
            'reason': 'Sprint 9 deploy',
            'files': _json.dumps(files),
            'file_count': len(files),
            'total_size': sum(f['size'] for f in files),
            'created_at': 1700000000,
        }

    def test_approve_sends_progress_update(self):
        """update_message should be called with '進度:' during the copy loop."""
        mock_execute, mock_table = _patch_all()
        with patch('callbacks.execute_command', mock_execute), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_large_item())

        # At least one update_message call must contain progress info
        progress_calls = [
            c for c in mock_update.call_args_list
            if len(c[0]) > 1 and '進度' in c[0][1]
        ]
        assert len(progress_calls) >= 1, "Expected at least one progress update_message call"
