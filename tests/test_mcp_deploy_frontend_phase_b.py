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
    mock_s3 = MagicMock()
    mock_cf = MagicMock()
    mock_table = MagicMock()

    if s3_side_effect is not None:
        mock_s3.copy_object.side_effect = s3_side_effect
    if cf_side_effect is not None:
        mock_cf.create_invalidation.side_effect = cf_side_effect

    def boto3_client(service, **kwargs):
        if service == 's3':
            return mock_s3
        if service == 'cloudfront':
            return mock_cf
        return MagicMock()

    return mock_s3, mock_cf, mock_table, boto3_client


# ---------------------------------------------------------------------------
# Deny tests
# ---------------------------------------------------------------------------

class TestDenyAction:
    def test_deny_updates_ddb_to_rejected(self):
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status:
            _call_callback(action='deny')
        mock_update_status.assert_called_once()
        args = mock_update_status.call_args[0]
        assert args[2] == 'rejected'

    def test_deny_does_not_call_s3(self):
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            _call_callback(action='deny')
        mock_s3.copy_object.assert_not_called()
        mock_cf.create_invalidation.assert_not_called()

    def test_deny_returns_200(self):
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            result = _call_callback(action='deny')
        assert result['statusCode'] == 200

    def test_deny_sends_telegram_update(self):
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
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
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_s3, mock_cf, mock_table, mock_update, mock_update_status

    def test_s3_copy_called_for_each_file(self):
        result, mock_s3, *_ = self._run()
        assert mock_s3.copy_object.call_count == len(_FILES_MANIFEST)

    def test_s3_copy_uses_correct_source_bucket(self):
        result, mock_s3, *_ = self._run()
        first_call = mock_s3.copy_object.call_args_list[0]
        copy_source = first_call[1]['CopySource']
        assert copy_source['Bucket'] == _STAGING_BUCKET

    def test_s3_copy_uses_correct_target_bucket(self):
        result, mock_s3, *_ = self._run()
        first_call = mock_s3.copy_object.call_args_list[0]
        assert first_call[1]['Bucket'] == _FRONTEND_BUCKET

    def test_s3_copy_passes_content_type(self):
        result, mock_s3, *_ = self._run()
        first_call = mock_s3.copy_object.call_args_list[0]
        assert first_call[1]['ContentType'] == 'text/html'

    def test_s3_copy_passes_cache_control(self):
        result, mock_s3, *_ = self._run()
        first_call = mock_s3.copy_object.call_args_list[0]
        assert 'no-cache' in first_call[1]['CacheControl']

    def test_s3_copy_metadata_replace(self):
        result, mock_s3, *_ = self._run()
        first_call = mock_s3.copy_object.call_args_list[0]
        assert first_call[1]['MetadataDirective'] == 'REPLACE'

    def test_cf_invalidation_called(self):
        result, _, mock_cf, *_ = self._run()
        mock_cf.create_invalidation.assert_called_once()
        call_args = mock_cf.create_invalidation.call_args[1]
        assert call_args['DistributionId'] == _DISTRIBUTION_ID
        paths = call_args['InvalidationBatch']['Paths']['Items']
        assert '/*' in paths

    def test_cf_caller_reference_is_request_id(self):
        result, _, mock_cf, *_ = self._run()
        call_args = mock_cf.create_invalidation.call_args[1]
        assert call_args['InvalidationBatch']['CallerReference'] == _REQUEST_ID

    def test_ddb_updated_with_approved_status(self):
        result, _, _, _, _, mock_update_status = self._run()
        mock_update_status.assert_called()
        args = mock_update_status.call_args[0]
        assert args[2] == 'approved'

    def test_ddb_updated_with_deploy_status_deployed(self):
        result, _, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_ddb_deployed_count_equals_file_count(self):
        result, _, _, _, _, mock_update_status = self._run()
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
        result, _, _, _, mock_update, _ = self._run()
        last_call_msg = mock_update.call_args_list[-1][0][1]
        assert '完成' in last_call_msg or '✅' in last_call_msg


# ---------------------------------------------------------------------------
# Approve - Partial Failure
# ---------------------------------------------------------------------------

class TestApprovePartialFailure:
    def _run(self):
        call_count = {'n': 0}
        def s3_side_effect(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise Exception("S3 access denied")
            return {}

        mock_s3, mock_cf, mock_table, boto3_client = _patch_all(s3_side_effect=s3_side_effect)
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_s3, mock_cf, mock_table, mock_update_status

    def test_partial_deploy_status(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'partial_deploy'

    def test_deployed_count_and_failed_count(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deployed_count') == 2
        assert extra.get('failed_count') == 1

    def test_failed_files_recorded(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        failed = json.loads(extra.get('failed_files', '[]'))
        assert len(failed) == 1
        assert 'app-abc123.js' in failed[0]

    def test_cf_invalidation_still_called_on_partial(self):
        result, _, mock_cf, *_ = self._run()
        mock_cf.create_invalidation.assert_called_once()

    def test_response_partial_status(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['deploy_status'] == 'partial_deploy'


# ---------------------------------------------------------------------------
# Approve - Full Failure
# ---------------------------------------------------------------------------

class TestApproveFullFailure:
    def _run(self):
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all(
            s3_side_effect=Exception("S3 error for all files")
        )
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_s3, mock_cf, mock_table, mock_update_status

    def test_deploy_failed_status(self):
        result, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deploy_failed'

    def test_cf_not_called_on_full_failure(self):
        result, _, mock_cf, *_ = self._run()
        mock_cf.create_invalidation.assert_not_called()

    def test_response_deploy_failed(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['deploy_status'] == 'deploy_failed'


# ---------------------------------------------------------------------------
# CloudFront Invalidation Failure
# ---------------------------------------------------------------------------

class TestCFInvalidationFailure:
    def _run(self):
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all(
            cf_side_effect=Exception("CF rate limit exceeded")
        )
        with patch('boto3.client', side_effect=boto3_client), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_s3, mock_cf, mock_table, mock_update, mock_update_status

    def test_s3_still_succeeds(self):
        result, mock_s3, *_ = self._run()
        assert mock_s3.copy_object.call_count == len(_FILES_MANIFEST)

    def test_cf_invalidation_failed_flag_true(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['cf_invalidation_failed'] is True

    def test_deploy_status_still_deployed(self):
        result, _, _, _, _, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_telegram_message_warns_about_cf(self):
        result, _, _, _, mock_update, _ = self._run()
        last_msg = mock_update.call_args_list[-1][0][1]
        assert 'CloudFront' in last_msg or 'Invalidation' in last_msg

    def test_ddb_cf_flag_recorded(self):
        result, _, _, _, _, mock_update_status = self._run()
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
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
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
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
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
        mock_s3, mock_cf, mock_table, boto3_client = _patch_all()
        with patch('boto3.client', side_effect=boto3_client), \
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
