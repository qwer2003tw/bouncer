"""
Tests for sprint9-003 Phase B: handle_deploy_frontend_callback

Covers:
  - deny action: DDB status=rejected, no S3 ops, Telegram update
  - approve action (full success): s3_target.put_object called for each file, CF invalidation, DDB updated
  - approve action (partial failure): deployed/failed lists correct, DDB records partial_deploy
  - approve action (full failure): deploy_failed status, no CF invalidation
  - CloudFront invalidation failure: S3 result preserved, cf_invalidation_failed=True in response
  - app.py routing: deploy_frontend dispatches to handle_deploy_frontend_callback

NOTE: Refactored in sprint12 — uses boto3 directly instead of execute_command.
      s3_staging.get_object (Lambda role) + s3_target.put_object (assumed role).
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
_DEPLOY_ROLE_ARN = "arn:aws:iam::190825685292:role/ztp-files-frontend-deploy-role"

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


def _make_item(files_manifest=None, deploy_role_arn=_DEPLOY_ROLE_ARN):
    manifest = files_manifest if files_manifest is not None else _FILES_MANIFEST
    item = {
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
    if deploy_role_arn is not None:
        item['deploy_role_arn'] = deploy_role_arn
    return item


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


def _run_approve(item=None, s3_get_side_effects=None, s3_put_side_effects=None,
                 cf_side_effect=None, assume_role_fail=False):
    """Run approve callback with boto3 mocked. Returns (result, mock_s3_staging, mock_s3_target, mock_cf, mock_update_status, mock_update)."""
    if item is None:
        item = _make_item()

    mock_s3_staging = MagicMock()
    mock_s3_target = MagicMock()
    mock_cf = MagicMock()
    mock_sts = MagicMock()

    # Setup staging get_object
    if s3_get_side_effects is not None:
        get_call_idx = {'n': 0}
        def _get(Bucket, Key):
            idx = get_call_idx['n']
            get_call_idx['n'] += 1
            e = s3_get_side_effects[idx] if idx < len(s3_get_side_effects) else None
            if isinstance(e, Exception):
                raise e
            bm = MagicMock()
            bm.read.return_value = b'content'
            return {'Body': bm}
        mock_s3_staging.get_object.side_effect = _get
    else:
        bm = MagicMock()
        bm.read.return_value = b'content'
        mock_s3_staging.get_object.return_value = {'Body': bm}

    # Setup target put_object
    if s3_put_side_effects is not None:
        put_call_idx = {'n': 0}
        def _put(**kwargs):
            idx = put_call_idx['n']
            put_call_idx['n'] += 1
            e = s3_put_side_effects[idx] if idx < len(s3_put_side_effects) else None
            if isinstance(e, Exception):
                raise e
            return {}
        mock_s3_target.put_object.side_effect = _put
    else:
        mock_s3_target.put_object.return_value = {}

    # Setup CF
    if cf_side_effect is not None:
        mock_cf.create_invalidation.side_effect = cf_side_effect
    else:
        mock_cf.create_invalidation.return_value = {'Invalidation': {'Id': 'INV-001'}}

    # Setup STS
    if assume_role_fail:
        mock_sts.assume_role.side_effect = Exception("AccessDenied: cannot assume role")
    else:
        mock_sts.assume_role.return_value = {
            'Credentials': {
                'AccessKeyId': 'AKIA-test',
                'SecretAccessKey': 'secret-test',
                'SessionToken': 'token-test',
            }
        }

    deploy_role_arn = item.get('deploy_role_arn')
    no_role_s3_count = {'n': 0}

    def _boto3_client_smart(service, **kwargs):
        if service == 'sts':
            return mock_sts
        if service == 'cloudfront':
            return mock_cf
        if service == 's3':
            if kwargs:  # has credentials kwargs -> assumed role -> target
                return mock_s3_target
            if not deploy_role_arn:
                # no role path: first call = s3_target, second = s3_staging
                no_role_s3_count['n'] += 1
                if no_role_s3_count['n'] == 1:
                    return mock_s3_target
                return mock_s3_staging
            # role path: no-kwargs call is always staging
            return mock_s3_staging
        return MagicMock()

    with patch('callbacks._boto3') as mock_boto3_mod, \
         patch('callbacks._get_table', return_value=MagicMock()), \
         patch('callbacks.answer_callback'), \
         patch('callbacks.update_message') as mock_update, \
         patch('callbacks._update_request_status') as mock_update_status, \
         patch('callbacks.emit_metric'), \
         patch('notifications._send_message_silent'):
        mock_boto3_mod.client.side_effect = _boto3_client_smart
        result = _call_callback(action='approve', item=item)

    return result, mock_s3_staging, mock_s3_target, mock_cf, mock_update_status, mock_update


# ---------------------------------------------------------------------------
# Deny tests
# ---------------------------------------------------------------------------

class TestDenyAction:
    def test_deny_updates_ddb_to_rejected(self):
        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status') as mock_update_status:
            _call_callback(action='deny')
        mock_update_status.assert_called_once()
        args = mock_update_status.call_args[0]
        assert args[2] == 'rejected'

    def test_deny_does_not_call_s3(self):
        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._boto3') as mock_boto3, \
             patch('callbacks._update_request_status'):
            _call_callback(action='deny')
        mock_boto3.client.assert_not_called()

    def test_deny_returns_200(self):
        with patch('callbacks._get_table', return_value=MagicMock()), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'):
            result = _call_callback(action='deny')
        assert result['statusCode'] == 200

    def test_deny_sends_telegram_update(self):
        with patch('callbacks._get_table', return_value=MagicMock()), \
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
        return _run_approve()

    def test_s3_put_object_called_for_each_file(self):
        result, _, mock_s3_target, *_ = self._run()
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_s3_get_object_called_for_each_file(self):
        result, mock_s3_staging, *_ = self._run()
        assert mock_s3_staging.get_object.call_count == len(_FILES_MANIFEST)

    def test_s3_get_uses_correct_staging_bucket(self):
        result, mock_s3_staging, *_ = self._run()
        first_call_kwargs = mock_s3_staging.get_object.call_args_list[0][1]
        assert first_call_kwargs['Bucket'] == _STAGING_BUCKET

    def test_s3_put_uses_correct_frontend_bucket(self):
        result, _, mock_s3_target, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert first_call_kwargs['Bucket'] == _FRONTEND_BUCKET

    def test_s3_put_passes_content_type(self):
        result, _, mock_s3_target, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert first_call_kwargs['ContentType'] == 'text/html'

    def test_s3_put_passes_cache_control(self):
        result, _, mock_s3_target, *_ = self._run()
        first_call_kwargs = mock_s3_target.put_object.call_args_list[0][1]
        assert 'no-cache' in first_call_kwargs['CacheControl']

    def test_cf_invalidation_called(self):
        result, _, _, mock_cf, *_ = self._run()
        mock_cf.create_invalidation.assert_called_once()
        call_kwargs = mock_cf.create_invalidation.call_args[1]
        assert call_kwargs['DistributionId'] == _DISTRIBUTION_ID
        paths = call_kwargs['InvalidationBatch']['Paths']['Items']
        assert '/*' in paths

    def test_cf_caller_reference_is_request_id(self):
        result, _, _, mock_cf, *_ = self._run()
        call_kwargs = mock_cf.create_invalidation.call_args[1]
        assert call_kwargs['InvalidationBatch']['CallerReference'] == _REQUEST_ID

    def test_ddb_updated_with_approved_status(self):
        result, _, _, _, mock_update_status, _ = self._run()
        mock_update_status.assert_called()
        args = mock_update_status.call_args[0]
        assert args[2] == 'approved'

    def test_ddb_updated_with_deploy_status_deployed(self):
        result, _, _, _, mock_update_status, _ = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_ddb_deployed_count_equals_file_count(self):
        result, _, _, _, mock_update_status, _ = self._run()
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
        result, _, _, _, _, mock_update = self._run()
        last_call_msg = mock_update.call_args_list[-1][0][1]
        assert '完成' in last_call_msg or '✅' in last_call_msg


# ---------------------------------------------------------------------------
# Approve - Partial Failure (second put_object fails)
# ---------------------------------------------------------------------------

class TestApprovePartialFailure:
    def _run(self):
        put_side_effects = [
            None,                                          # index.html -> success
            Exception("S3 access denied for file 2"),    # app-abc123.js -> fail
            None,                                          # style-def456.css -> success
        ]
        return _run_approve(s3_put_side_effects=put_side_effects)

    def test_partial_deploy_status(self):
        result, _, _, _, mock_update_status, _ = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'partial_deploy'

    def test_deployed_count_and_failed_count(self):
        result, _, _, _, mock_update_status, _ = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deployed_count') == 2
        assert extra.get('failed_count') == 1

    def test_failed_files_recorded(self):
        result, _, _, _, mock_update_status, _ = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        failed = json.loads(extra.get('failed_files', '[]'))
        assert len(failed) == 1
        assert 'app-abc123.js' in failed[0]

    def test_cf_invalidation_still_called_on_partial(self):
        result, _, _, mock_cf, *_ = self._run()
        mock_cf.create_invalidation.assert_called_once()

    def test_response_partial_status(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['deploy_status'] == 'partial_deploy'


# ---------------------------------------------------------------------------
# Approve - Full Failure (all put_object fail)
# ---------------------------------------------------------------------------

class TestApproveFullFailure:
    def _run(self):
        put_side_effects = [
            Exception("S3 error file 1"),
            Exception("S3 error file 2"),
            Exception("S3 error file 3"),
        ]
        return _run_approve(s3_put_side_effects=put_side_effects)

    def test_deploy_failed_status(self):
        result, _, _, _, mock_update_status, _ = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deploy_failed'

    def test_cf_not_called_on_full_failure(self):
        result, _, _, mock_cf, *_ = self._run()
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
        return _run_approve(cf_side_effect=Exception("CF rate limit exceeded"))

    def test_s3_still_succeeds(self):
        result, _, mock_s3_target, *_ = self._run()
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_cf_invalidation_failed_flag_true(self):
        result, *_ = self._run()
        body = json.loads(result['body'])
        assert body['cf_invalidation_failed'] is True

    def test_deploy_status_still_deployed(self):
        result, _, _, _, mock_update_status, _ = self._run()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        assert extra.get('deploy_status') == 'deployed'

    def test_telegram_message_warns_about_cf(self):
        result, _, _, _, _, mock_update = self._run()
        last_msg = mock_update.call_args_list[-1][0][1]
        assert 'CloudFront' in last_msg or 'Invalidation' in last_msg

    def test_ddb_cf_flag_recorded(self):
        result, _, _, _, mock_update_status, _ = self._run()
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
        result, _, _, _, mock_update_status, _ = _run_approve()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        details = json.loads(extra['deployed_details'])
        assert isinstance(details, list)
        assert len(details) == len(_FILES_MANIFEST)
        first = details[0]
        assert 'filename' in first
        assert 's3_key' in first

    def test_failed_details_empty_on_full_success(self):
        result, _, _, _, mock_update_status, _ = _run_approve()
        extra = mock_update_status.call_args[1].get('extra_attrs', {})
        failed = json.loads(extra['failed_details'])
        assert isinstance(failed, list)
        assert failed == []


# ---------------------------------------------------------------------------
# Progress update during approve
# ---------------------------------------------------------------------------

class TestApproveProgressUpdate:
    """Verify update_message is called with progress info during copy loop."""

    def _make_large_item(self):
        """6 files -- ensures the % 5 == 0 progress branch fires at file #5."""
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
            'files': json.dumps(files),
            'file_count': len(files),
            'total_size': sum(f['size'] for f in files),
            'created_at': 1700000000,
            'deploy_role_arn': _DEPLOY_ROLE_ARN,
        }

    def test_approve_sends_progress_update(self):
        """update_message should be called with progress info during the copy loop."""
        result, _, _, _, _, mock_update = _run_approve(item=self._make_large_item())

        # At least one update_message call must contain progress info
        progress_calls = [
            c for c in mock_update.call_args_list
            if len(c[0]) > 1 and '進度' in c[0][1]
        ]
        assert len(progress_calls) >= 1, "Expected at least one progress update_message call"


# ---------------------------------------------------------------------------
# Sprint 12: deploy_role_arn -- boto3 assumed role vs Lambda role
# ---------------------------------------------------------------------------

class TestDeployRoleArnPhaseB:
    """Verify Phase B uses assumed-role boto3 client for S3 target and CF,
    and falls back to Lambda role when deploy_role_arn is absent."""

    def test_sts_assume_role_success_deploys_all_files(self):
        """When deploy_role_arn is set and assume_role succeeds, all files deploy."""
        result, *_ = _run_approve()
        body = json.loads(result['body'])
        assert body['deployed_count'] == len(_FILES_MANIFEST)

    def test_s3_target_put_object_called_with_assumed_creds(self):
        """s3_target (assumed role) put_object must be called for each file."""
        result, _, mock_s3_target, *_ = _run_approve()
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_no_role_fallback_deploys_all_files(self):
        """When deploy_role_arn is absent, Lambda role is used and all files deploy."""
        item = _make_item(deploy_role_arn=None)
        item.pop('deploy_role_arn', None)
        result, _, mock_s3_target, *_ = _run_approve(item=item)
        body = json.loads(result['body'])
        assert body['deployed_count'] == len(_FILES_MANIFEST)
        assert mock_s3_target.put_object.call_count == len(_FILES_MANIFEST)

    def test_assume_role_fail_all_files_go_to_failed(self):
        """When assume_role raises, all files must be in failed[], deploy_failed."""
        result, *_ = _run_approve(assume_role_fail=True)
        body = json.loads(result['body'])
        assert body['deploy_status'] == 'deploy_failed'
        assert body['failed_count'] == len(_FILES_MANIFEST)
        assert body['deployed_count'] == 0
