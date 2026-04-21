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
from botocore.exceptions import ClientError

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


def _get_s3_client_factory(mock_s3_target, mock_s3_staging):
    """Return a side_effect function for patching callbacks.get_s3_client.

    callbacks.py now calls get_s3_client() twice:
      1st call (no role_arn) -> s3_target (or s3_staging for staging bucket)
      2nd call (no role_arn) -> s3_staging

    In the no-deploy-role path both calls go to get_s3_client() with no role_arn.
    We return s3_target first, then s3_staging.
    """
    calls = {'count': 0}
    def factory(role_arn=None, session_name='bouncer-s3', region=None):
        if role_arn:
            return mock_s3_target
        calls['count'] += 1
        if calls['count'] == 1:
            return mock_s3_target
        return mock_s3_staging
    return factory


def _make_boto3_mock(get_object_side_effect=None, put_object_side_effect=None,
                     cf_side_effect=None):
    """Build mock S3/CF clients for deploy_frontend callback tests.

    callbacks.py now uses get_s3_client() and get_cloudfront_client() factories
    instead of _boto3.client(). Tests patch those factories via
    _get_s3_client_factory() and aws_clients.get_cloudfront_client.
    """
    mock_s3_target = MagicMock()
    mock_s3_staging = MagicMock()
    mock_cf = MagicMock()
    mock_sts = MagicMock()  # kept for compat; not used directly now

    # Default get_object success
    if get_object_side_effect is not None:
        mock_s3_staging.get_object.side_effect = get_object_side_effect
    else:
        mock_s3_staging.get_object.return_value = {'Body': _make_body_mock()}

    if put_object_side_effect is not None:
        mock_s3_target.put_object.side_effect = put_object_side_effect

    if cf_side_effect is not None:
        mock_cf.create_invalidation.side_effect = cf_side_effect

    return None, mock_s3_target, mock_s3_staging, mock_cf, mock_sts


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
        _, mock_s3_target, mock_s3_staging, mock_cf, _ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status:
            _call_callback(action='deny')
        mock_update_status.assert_called_once()
        args = mock_update_status.call_args[0]
        assert args[2] == 'rejected'

    def test_deny_does_not_call_s3(self):
        _, mock_s3_target, mock_s3_staging, mock_cf, _ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)) as mock_s3_factory, \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            _call_callback(action='deny')
        # For deny, get_s3_client should NOT have been called
        mock_s3_factory.assert_not_called()

    def test_deny_returns_200(self):
        _, mock_s3_target, mock_s3_staging, mock_cf, _ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            result = _call_callback(action='deny')
        assert result['statusCode'] == 200

    def test_deny_sends_telegram_update(self):
        _, mock_s3_target, mock_s3_staging, mock_cf, _ = _patch_all()
        mock_table = MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
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
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
                raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'S3 access denied'}}, 'GetObject')
            body_mock = _make_body_mock()
            return {'Body': body_mock}

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_object_side_effect
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
            raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'S3 error for all files'}}, 'GetObject')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_object_side_effect
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
            cf_side_effect=ClientError({'Error': {'Code': 'TooManyInvalidationsInProgress', 'Message': 'CF rate limit exceeded'}}, 'CreateInvalidation')
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
        with open(os.path.join(os.path.dirname(__file__), '..', 'src', 'webhook_router.py')) as f:
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
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
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
        """When deploy_role_arn is set, get_s3_client is called with role_arn."""
        _, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)) as mock_s3_factory, \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=self._make_item_with_role())

        calls_with_role = [c for c in mock_s3_factory.call_args_list if c[1].get('role_arn')]
        assert len(calls_with_role) > 0, "Expected get_s3_client called with role_arn"
        assert calls_with_role[0][1]['role_arn'] == _DEPLOY_ROLE_ARN

    def test_s3_copy_called_with_assume_role_arn(self):
        """When deploy_role_arn present, get_s3_client is called with role_arn for s3_target."""
        _, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)) as mock_s3_factory, \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=self._make_item_with_role())

        # get_s3_client called with role_arn for s3_target
        calls_with_role = [c for c in mock_s3_factory.call_args_list if c[1].get('role_arn')]
        assert len(calls_with_role) > 0, "Expected get_s3_client called with role_arn for s3_target"
        assert calls_with_role[0][1]['role_arn'] == _DEPLOY_ROLE_ARN

    def test_cf_invalidation_called_with_assume_role_arn(self):
        """CF client created via get_cloudfront_client with role_arn when deploy_role_arn present."""
        _, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf) as mock_cf_factory, \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=self._make_item_with_role())

        # get_cloudfront_client called with role_arn
        calls_with_role = [c for c in mock_cf_factory.call_args_list if c[1].get('role_arn')]
        assert len(calls_with_role) == 1, "Expected get_cloudfront_client called with role_arn"
        assert calls_with_role[0][1]['role_arn'] == _DEPLOY_ROLE_ARN

    def test_s3_copy_fallback_when_role_absent(self):
        """When deploy_role_arn absent, s3 clients use Lambda role (no credentials)."""
        _, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=self._make_item_without_role())

        # Files are deployed successfully
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)
        # get_s3_client called without role_arn (no assumed-role)

    def test_cf_invalidation_fallback_when_role_absent(self):
        """When deploy_role_arn absent, CF client uses Lambda role."""
        _, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=self._make_item_without_role())

        mock_cf.create_invalidation.assert_called_once()

    def test_s3_copy_fallback_when_role_is_none(self):
        """When deploy_role_arn is explicitly None, s3 uses Lambda role."""
        _, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=self._make_item_none_role())

        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)
        # get_s3_client called without role_arn when role is None


# ---------------------------------------------------------------------------
# Sprint 21-002: Staging cleanup, deploy history, and staged key paths
# ---------------------------------------------------------------------------

class TestStagingCleanupAfterDeploy:
    """Verify that staged objects in the staging bucket are deleted after a successful deploy.

    Phase B reads each file from staging (get_object) then writes to frontend bucket
    (put_object). After a successful put_object, the staged object should be cleaned up
    from the staging bucket (delete_object on s3_staging).
    """

    def _run_with_staging_s3(self):
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        # Track delete_object calls on s3_staging
        deleted_keys = []

        def delete_object_side_effect(**kwargs):
            deleted_keys.append(kwargs.get('Key'))

        mock_s3_staging.delete_object.side_effect = delete_object_side_effect

        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            result = _call_callback(action='approve')
        return result, mock_boto3, mock_s3_target, mock_s3_staging, deleted_keys

    def test_get_object_uses_staged_s3_key(self):
        """get_object must use the s3_key from the files manifest as the Key."""
        _, _, _, mock_s3_staging, _ = self._run_with_staging_s3()
        call_keys = [c[1].get('Key') for c in mock_s3_staging.get_object.call_args_list]
        expected_keys = [fm['s3_key'] for fm in _FILES_MANIFEST]
        assert call_keys == expected_keys, (
            f"get_object keys {call_keys!r} != manifest keys {expected_keys!r}"
        )

    def test_get_object_uses_staging_bucket(self):
        """get_object must read from the staging bucket, not the frontend bucket."""
        _, _, _, mock_s3_staging, _ = self._run_with_staging_s3()
        for c in mock_s3_staging.get_object.call_args_list:
            assert c[1].get('Bucket') == _STAGING_BUCKET, (
                f"Expected staging bucket {_STAGING_BUCKET!r}, got {c[1].get('Bucket')!r}"
            )

    def test_put_object_uses_filename_as_key(self):
        """put_object must write to the frontend bucket using the original filename as key."""
        _, _, mock_s3_target, _, _ = self._run_with_staging_s3()
        call_keys = [c[1].get('Key') for c in mock_s3_target.put_object.call_args_list]
        expected_keys = [fm['filename'] for fm in _FILES_MANIFEST]
        assert call_keys == expected_keys, (
            f"put_object keys {call_keys!r} != filenames {expected_keys!r}"
        )

    def test_put_object_uses_frontend_bucket(self):
        """put_object must write to the frontend bucket."""
        _, _, mock_s3_target, _, _ = self._run_with_staging_s3()
        for c in mock_s3_target.put_object.call_args_list:
            assert c[1].get('Bucket') == _FRONTEND_BUCKET

    def test_index_html_has_no_cache_cache_control(self):
        """index.html must have 'no-cache' in CacheControl (not immutable)."""
        _, _, mock_s3_target, _, _ = self._run_with_staging_s3()
        index_call = next(
            c for c in mock_s3_target.put_object.call_args_list
            if c[1].get('Key') == 'index.html'
        )
        cc = index_call[1].get('CacheControl', '')
        assert 'no-cache' in cc, f"Expected no-cache CacheControl for index.html, got {cc!r}"
        assert 'immutable' not in cc

    def test_assets_have_immutable_cache_control(self):
        """Files under assets/ must have immutable CacheControl."""
        _, _, mock_s3_target, _, _ = self._run_with_staging_s3()
        asset_calls = [
            c for c in mock_s3_target.put_object.call_args_list
            if c[1].get('Key', '').startswith('assets/')
        ]
        assert len(asset_calls) > 0, "Expected at least one assets/ file"
        for c in asset_calls:
            cc = c[1].get('CacheControl', '')
            assert 'immutable' in cc, (
                f"Expected immutable CacheControl for asset {c[1].get('Key')!r}, got {cc!r}"
            )

    def test_get_object_read_count_matches_file_count(self):
        """get_object is called exactly once per file in the manifest."""
        _, _, _, mock_s3_staging, _ = self._run_with_staging_s3()
        assert mock_s3_staging.get_object.call_count == len(_FILES_MANIFEST)

    def test_put_object_write_count_matches_file_count(self):
        """put_object is called exactly once per file (full success)."""
        _, _, mock_s3_target, _, _ = self._run_with_staging_s3()
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_cf_not_called_when_all_files_fail_get_object(self):
        """CloudFront invalidation must NOT be called when all get_object calls fail."""
        def get_fail(**kwargs):
            raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'Staging read error'}}, 'GetObject')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_fail
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve')
        mock_cf.create_invalidation.assert_not_called()

    def test_full_failure_when_all_get_object_fail(self):
        """deploy_status must be deploy_failed when all files fail at get_object."""
        def get_fail(**kwargs):
            raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'Staging read error'}}, 'GetObject')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_fail
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status, \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve')
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deploy_failed'

    def test_put_object_error_does_not_call_cf_on_full_failure(self):
        """When all put_object calls fail, CF invalidation must not be triggered."""
        def put_fail(**kwargs):
            raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'Target S3 write error'}}, 'PutObject')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            put_object_side_effect=put_fail
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve')
        mock_cf.create_invalidation.assert_not_called()

    def test_cf_called_when_at_least_one_file_succeeds(self):
        """CF invalidation is called if at least one file was deployed successfully."""
        call_count = {'n': 0}

        def put_fail_second(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise ClientError({'Error': {'Code': 'ServiceUnavailable', 'Message': 'Second file failed'}}, 'PutObject')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            put_object_side_effect=put_fail_second
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve')
        mock_cf.create_invalidation.assert_called_once()


class TestDeployHistoryWrite:
    """Verify that _write_frontend_deploy_history is called after Phase B deploy."""

    def test_deploy_history_called_on_full_success(self):
        """_write_frontend_deploy_history must be called once on full success."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='approve')
        mock_history.assert_called_once()

    def test_deploy_history_receives_correct_project(self):
        """_write_frontend_deploy_history must receive the project name from DDB item."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='approve')
        kwargs = mock_history.call_args[1]
        assert kwargs['project'] == 'ztp-files'

    def test_deploy_history_status_deployed_on_full_success(self):
        """_write_frontend_deploy_history receives deploy_status='deployed' on full success."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='approve')
        kwargs = mock_history.call_args[1]
        assert kwargs['deploy_status'] == 'deployed'

    def test_deploy_history_status_deploy_failed_on_full_failure(self):
        """_write_frontend_deploy_history receives deploy_status='deploy_failed' on full failure."""
        def get_fail(**kwargs):
            raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'S3 error'}}, 'GetObject')

        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            get_object_side_effect=get_fail
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='approve')
        kwargs = mock_history.call_args[1]
        assert kwargs['deploy_status'] == 'deploy_failed'

    def test_deploy_history_not_called_on_deny(self):
        """_write_frontend_deploy_history must NOT be called when action=deny."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='deny')
        mock_history.assert_not_called()

    def test_deploy_history_success_count_correct(self):
        """_write_frontend_deploy_history receives correct success_count and fail_count."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all()
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='approve')
        kwargs = mock_history.call_args[1]
        assert kwargs['success_count'] == len(_FILES_MANIFEST)
        assert kwargs['fail_count'] == 0

    def test_deploy_history_cf_invalidation_failed_flag(self):
        """_write_frontend_deploy_history receives cf_invalidation_failed=True when CF fails."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf, mock_table = _patch_all(
            cf_side_effect=ClientError({'Error': {'Code': 'TooManyInvalidationsInProgress', 'Message': 'CF error'}}, 'CreateInvalidation')
        )
        with patch('callbacks.get_s3_client', side_effect=_get_s3_client_factory(mock_s3_target, mock_s3_staging)), \
             patch('aws_clients.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('telegram.send_message_with_entities'), \
             patch('callbacks._write_frontend_deploy_history') as mock_history:
            _call_callback(action='approve')
        kwargs = mock_history.call_args[1]
        assert kwargs['cf_invalidation_failed'] is True
