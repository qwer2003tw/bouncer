"""
Tests for sprint9-003 Phase B: handle_deploy_frontend_callback

Covers:
  - deny action: DDB status=rejected, no S3 ops, Telegram update
  - approve action (full success): s3.get_object + put_object called for each file,
    CF invalidation, DDB updated
  - approve action (partial failure): deployed/failed lists correct, DDB records partial_deploy
  - approve action (full failure): deploy_failed status, no CF invalidation
  - CloudFront invalidation failure: S3 result preserved, cf_invalidation_failed=True in response
  - app.py routing: deploy_frontend dispatches to handle_deploy_frontend_callback
"""
import json
import sys
import os
import pytest
from io import BytesIO
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


def _make_body_mock(content=b'file-content'):
    m = MagicMock()
    m.read.return_value = content
    return m


def _make_boto3_mock(get_object_side_effect=None, put_object_side_effect=None,
                     cf_side_effect=None):
    """
    Build a mock boto3 module that returns appropriate mock clients.

    client() call sequence in handle_deploy_frontend_callback (no deploy_role_arn):
      1. _boto3.client('s3')           -> s3_target  (fallback, no creds)
      2. _boto3.client('s3')           -> s3_staging (Lambda role)
      3. _boto3.client('cloudfront')   -> cf  (only if success_count > 0)

    With deploy_role_arn:
      1. _boto3.client('sts')                         -> sts
      2. _boto3.client('s3', aws_access_key_id=...)  -> s3_target (assumed role)
      3. _boto3.client('s3')                          -> s3_staging (Lambda role)
      4. _boto3.client('cloudfront', aws_access_key_id=...) -> cf
    """
    mock_boto3 = MagicMock()

    mock_s3_target = MagicMock()
    mock_s3_staging = MagicMock()
    mock_cf = MagicMock()
    mock_sts = MagicMock()

    # Default get_object success
    if get_object_side_effect is not None:
        mock_s3_staging.get_object.side_effect = get_object_side_effect
    else:
        mock_s3_staging.get_object.return_value = {'Body': _make_body_mock()}

    if put_object_side_effect is not None:
        mock_s3_target.put_object.side_effect = put_object_side_effect

    if cf_side_effect is not None:
        mock_cf.create_invalidation.side_effect = cf_side_effect

    mock_sts.assume_role.return_value = {
        'Credentials': {
            'AccessKeyId': 'FAKEAKID',
            'SecretAccessKey': 'FAKESAK',
            'SessionToken': 'FAKEST',
        }
    }

    # Track s3 calls without credentials (first = s3_target fallback, second = s3_staging)
    s3_no_creds_calls = {'count': 0}

    def client_side_effect(service, **kwargs):
        if service == 'sts':
            return mock_sts
        if service == 'cloudfront':
            return mock_cf
        if service == 's3':
            if kwargs.get('aws_access_key_id'):
                # Called with assumed-role credentials -> s3_target
                return mock_s3_target
            else:
                s3_no_creds_calls['count'] += 1
                if s3_no_creds_calls['count'] == 1:
                    return mock_s3_target
                else:
                    return mock_s3_staging
        return MagicMock()

    mock_boto3.client.side_effect = client_side_effect
    return mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_sts


def _patch_all(get_object_side_effect=None, put_object_side_effect=None, cf_side_effect=None):
    mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_sts = _make_boto3_mock(
        get_object_side_effect=get_object_side_effect,
        put_object_side_effect=put_object_side_effect,
        cf_side_effect=cf_side_effect,
    )
    mock_table = MagicMock()
    return mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table


# ---------------------------------------------------------------------------
# Deny tests
# ---------------------------------------------------------------------------

class TestDenyAction:
    def test_deny_updates_ddb_to_rejected(self):
        mock_boto3, *_ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status:
            _call_callback(action='deny')
        mock_update_status.assert_called_once()
        args = mock_update_status.call_args[0]
        assert args[2] == 'rejected'

    def test_deny_does_not_call_s3(self):
        mock_boto3, mock_s3_target, mock_s3_staging, *_ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            _call_callback(action='deny')
        # No boto3 client should have been created for deny
        mock_boto3.client.assert_not_called()

    def test_deny_returns_200(self):
        mock_boto3, *_ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            result = _call_callback(action='deny')
        assert result['statusCode'] == 200

    def test_deny_sends_telegram_update(self):
        mock_boto3, *_ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks._boto3', mock_boto3), \
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
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table, mock_update, mock_update_status

    def test_s3_copy_called_for_each_file(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_s3_get_object_called_for_each_file(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        assert mock_s3_staging.get_object.call_count == len(_FILES_MANIFEST)

    def test_s3_copy_uses_correct_source_bucket(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        first_call_kwargs = mock_s3_staging.get_object.call_args_list[0][1]
        assert first_call_kwargs.get('Bucket') == _STAGING_BUCKET

    def test_s3_copy_uses_correct_target_bucket(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert first_call_kwargs.get('Bucket') == _FRONTEND_BUCKET

    def test_s3_copy_passes_content_type(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert first_call_kwargs.get('ContentType') == 'text/html'

    def test_s3_copy_passes_cache_control(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert 'no-cache' in first_call_kwargs.get('CacheControl', '')

    def test_s3_copy_metadata_replace(self):
        # In boto3 path, CacheControl is passed explicitly as a kwarg (no REPLACE flag).
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert 'CacheControl' in first_call_kwargs

    def test_cf_invalidation_called(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, *_ = self._run()
        mock_cf.create_invalidation.assert_called_once()
        call_kwargs = mock_cf.create_invalidation.call_args[1]
        assert call_kwargs.get('DistributionId') == _DISTRIBUTION_ID
        items = call_kwargs['InvalidationBatch']['Paths']['Items']
        assert '/*' in items

    def test_cf_caller_reference_is_request_id(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, *_ = self._run()
        call_kwargs = mock_cf.create_invalidation.call_args[1]
        caller_ref = call_kwargs['InvalidationBatch']['CallerReference']
        assert caller_ref == _REQUEST_ID

    def test_ddb_updated_with_approved_status(self):
        result, *_, mock_update, mock_update_status = self._run()
        mock_update_status.assert_called()
        args = mock_update_status.call_args[0]
        assert args[2] == 'approved'

    def test_ddb_updated_with_deploy_status_deployed(self):
        result, *_, mock_update, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_ddb_deployed_count_equals_file_count(self):
        result, *_, mock_update, mock_update_status = self._run()
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
        result, *_, mock_update, _ = self._run()
        last_call_msg = mock_update.call_args_list[-1][0][1]
        assert '完成' in last_call_msg or '✅' in last_call_msg


# ---------------------------------------------------------------------------
# Approve - Partial Failure
# ---------------------------------------------------------------------------

class TestApprovePartialFailure:
    def _run(self):
        # 2nd get_object call fails (assets/app-abc123.js)
        call_count = {'n': 0}

        def get_object_side_effect(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise Exception('S3 access denied')
            body_mock = _make_body_mock()
            return {'Body': body_mock}

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_object_side_effect
        )
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table, mock_update_status

    def test_partial_deploy_status(self):
        result, *_, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'partial_deploy'

    def test_deployed_count_and_failed_count(self):
        result, *_, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deployed_count') == 2
        assert extra.get('failed_count') == 1

    def test_failed_files_recorded(self):
        result, *_, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        failed = json.loads(extra.get('failed_files', '[]'))
        assert len(failed) == 1
        assert 'app-abc123.js' in failed[0]

    def test_cf_invalidation_still_called_on_partial(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, *_ = self._run()
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
        def get_object_side_effect(**kwargs):
            raise Exception('S3 error for all files')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_object_side_effect
        )
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table, mock_update_status

    def test_deploy_failed_status(self):
        result, *_, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deploy_failed'

    def test_cf_not_called_on_full_failure(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, *_ = self._run()
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
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            cf_side_effect=Exception('CF rate limit exceeded')
        )
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            result = _call_callback(action='approve')
        return result, mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table, mock_update, mock_update_status

    def test_s3_still_succeeds(self):
        result, mock_boto3, mock_s3_target, mock_s3_staging, *_ = self._run()
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_cf_invalidation_failed_flag_true(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['cf_invalidation_failed'] is True

    def test_deploy_status_still_deployed(self):
        result, *_, mock_update, mock_update_status = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_telegram_message_warns_about_cf(self):
        result, *_, mock_update, _ = self._run()
        last_msg = mock_update.call_args_list[-1][0][1]
        assert 'CloudFront' in last_msg or 'Invalidation' in last_msg

    def test_ddb_cf_flag_recorded(self):
        result, *_, mock_update, mock_update_status = self._run()
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
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
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
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
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
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_large_item())

        progress_calls = [
            c for c in mock_update.call_args_list
            if len(c[0]) > 1 and '進度' in c[0][1]
        ]
        assert len(progress_calls) >= 1, "Expected at least one progress update_message call"


# ---------------------------------------------------------------------------
# Sprint 11-000: deploy_role_arn forwarded via STS assume_role (Phase B)
# ---------------------------------------------------------------------------

_DEPLOY_ROLE_ARN = "arn:aws:iam::190825685292:role/ztp-files-frontend-deploy-role"


class TestDeployRoleArnPhaseB:
    """Verify Phase B uses sts.assume_role when deploy_role_arn is present,
    and falls back gracefully when absent."""

    def _make_item_with_role(self):
        item = _make_item()
        item['deploy_role_arn'] = _DEPLOY_ROLE_ARN
        return item

    def _make_item_without_role(self):
        """Simulate an older DDB record that has no deploy_role_arn field."""
        item = _make_item()
        item.pop('deploy_role_arn', None)
        return item

    def _make_item_none_role(self):
        """DDB record where deploy_role_arn was explicitly stored as None."""
        item = _make_item()
        item['deploy_role_arn'] = None
        return item

    def test_sts_assume_role_called_with_correct_arn(self):
        """When deploy_role_arn is set, sts.assume_role must be called with it."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        # Capture the sts mock to verify assume_role was called
        sts_mock = MagicMock()
        sts_mock.assume_role.return_value = {
            'Credentials': {
                'AccessKeyId': 'FAKEAKID',
                'SecretAccessKey': 'FAKESAK',
                'SessionToken': 'FAKEST',
            }
        }
        original_side_effect = mock_boto3.client.side_effect

        def patched_client(service, **kwargs):
            if service == 'sts':
                return sts_mock
            return original_side_effect(service, **kwargs)

        mock_boto3.client.side_effect = patched_client

        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_item_with_role())

        sts_mock.assume_role.assert_called_once()
        call_kwargs = sts_mock.assume_role.call_args[1]
        assert call_kwargs['RoleArn'] == _DEPLOY_ROLE_ARN

    def test_s3_copy_called_with_assume_role_arn(self):
        """When deploy_role_arn present, s3_target is created with assumed-role credentials."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_item_with_role())

        # s3 client created with aws_access_key_id = assumed-role credentials
        s3_cred_calls = [
            c for c in mock_boto3.client.call_args_list
            if len(c[0]) > 0 and c[0][0] == 's3' and c[1].get('aws_access_key_id')
        ]
        assert len(s3_cred_calls) > 0, "Expected s3 client created with assumed-role credentials"
        assert s3_cred_calls[0][1]['aws_access_key_id'] == 'FAKEAKID'

    def test_cf_invalidation_called_with_assume_role_arn(self):
        """CF client should also be created with assumed-role credentials."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_item_with_role())

        cf_cred_calls = [
            c for c in mock_boto3.client.call_args_list
            if len(c[0]) > 0 and c[0][0] == 'cloudfront' and c[1].get('aws_access_key_id')
        ]
        assert len(cf_cred_calls) == 1, "Expected CF client created with assumed-role credentials"
        assert cf_cred_calls[0][1]['aws_access_key_id'] == 'FAKEAKID'

    def test_s3_copy_fallback_when_role_absent(self):
        """When deploy_role_arn absent, s3 clients use Lambda role (no credentials)."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_item_without_role())

        # Files are deployed successfully
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)
        # No s3 client with credentials
        s3_cred_calls = [
            c for c in mock_boto3.client.call_args_list
            if len(c[0]) > 0 and c[0][0] == 's3' and c[1].get('aws_access_key_id')
        ]
        assert len(s3_cred_calls) == 0, "No assumed-role s3 client expected when role absent"

    def test_cf_invalidation_fallback_when_role_absent(self):
        """When deploy_role_arn absent, CF client uses Lambda role."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_item_without_role())

        mock_cf.create_invalidation.assert_called_once()
        cf_cred_calls = [
            c for c in mock_boto3.client.call_args_list
            if len(c[0]) > 0 and c[0][0] == 'cloudfront' and c[1].get('aws_access_key_id')
        ]
        assert len(cf_cred_calls) == 0, "No assumed-role CF client expected when role absent"

    def test_s3_copy_fallback_when_role_is_none(self):
        """When deploy_role_arn is explicitly None, s3 uses Lambda role."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks._boto3', mock_boto3), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('notifications._send_message_silent'):
            _call_callback(action='approve', item=self._make_item_none_role())

        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)
        s3_cred_calls = [
            c for c in mock_boto3.client.call_args_list
            if len(c[0]) > 0 and c[0][0] == 's3' and c[1].get('aws_access_key_id')
        ]
        assert len(s3_cred_calls) == 0, "No assumed-role s3 client expected when role is None"
