"""
Sprint 19 Task 003 — Regression + Unit tests:
bouncer_deploy_frontend should write to deploy_history DynamoDB table after deploy.

Covers:
  - Successful deploy writes history with status=SUCCEEDED
  - Partial deploy writes history with status=PARTIAL
  - Failed deploy (all files fail) writes history with status=FAILED
  - deploy_history write failure does NOT break the callback (non-critical)
  - deploy_id format: 'frontend-{request_id}'
  - project_id in history matches project in item
  - files_count, files_deployed, files_failed match actual deploy counts
  - Regression: _write_frontend_deploy_history is importable/callable from callbacks
"""
import json
import sys
import os
import pytest
from io import BytesIO
from unittest.mock import patch, MagicMock, call, ANY

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

_STAGING_BUCKET = 'bouncer-uploads-190825685292'
_FRONTEND_BUCKET = 'ztp-files-dev-frontendbucket-test'
_DISTRIBUTION_ID = 'ETESTDIST001'
_REQUEST_ID = 'req-s19-003-test-001'

_FILES_MANIFEST = [
    {
        'filename': 'index.html',
        's3_key': 'pending/{}/index.html'.format(_REQUEST_ID),
        'content_type': 'text/html',
        'cache_control': 'no-cache, no-store, must-revalidate',
        'size': 1024,
    },
    {
        'filename': 'assets/app-abc.js',
        's3_key': 'pending/{}/assets/app-abc.js'.format(_REQUEST_ID),
        'content_type': 'application/javascript',
        'cache_control': 'max-age=31536000, immutable',
        'size': 8192,
    },
]


def _make_item(files_manifest=None, deploy_role_arn=None):
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
        'source': 'Private Bot (ZTP Files - Sprint 19)',
        'reason': 'Sprint 19 deploy test',
        'files': json.dumps(manifest),
        'file_count': len(manifest),
        'total_size': sum(f['size'] for f in manifest),
        'created_at': 1700000000,
    }
    if deploy_role_arn is not None:
        item['deploy_role_arn'] = deploy_role_arn
    return item


def _make_body_mock(content=b'file-content'):
    m = MagicMock()
    m.read.return_value = content
    return m


def _make_boto3_mock(get_object_side_effect=None, put_object_side_effect=None, cf_side_effect=None):
    """Build boto3 mock for no-role path (Lambda role only)."""
    mock_boto3 = MagicMock()
    mock_s3_target = MagicMock()
    mock_s3_staging = MagicMock()
    mock_cf = MagicMock()

    if get_object_side_effect is not None:
        mock_s3_staging.get_object.side_effect = get_object_side_effect
    else:
        mock_s3_staging.get_object.return_value = {'Body': _make_body_mock()}

    if put_object_side_effect is not None:
        mock_s3_target.put_object.side_effect = put_object_side_effect

    if cf_side_effect is not None:
        mock_cf.create_invalidation.side_effect = cf_side_effect

    # No role: first s3 = target, second s3 = staging, third = cf
    mock_boto3.client.side_effect = [mock_s3_target, mock_s3_staging, mock_cf]
    return mock_boto3, mock_s3_target, mock_s3_staging, mock_cf


def _call_callback(action='approve', item=None, user_id='user-tester'):
    from callbacks import handle_deploy_frontend_callback
    return handle_deploy_frontend_callback(
        request_id=_REQUEST_ID,
        action=action,
        item=item or _make_item(),
        message_id=888,
        callback_id='cb-s19-003',
        user_id=user_id,
    )


class TestDeployFrontendHistoryWrite:
    """Tests that deploy_history is written correctly after handle_deploy_frontend_callback."""

    def test_successful_deploy_calls_write_history(self):
        """Full success -> _write_frontend_deploy_history called with deploy_status=deployed."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock()
        mock_table = MagicMock()

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._write_frontend_deploy_history') as mock_write_history, \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=_make_item())

        mock_write_history.assert_called_once()
        kwargs = mock_write_history.call_args[1]
        assert kwargs['request_id'] == _REQUEST_ID
        assert kwargs['project'] == 'ztp-files'
        assert kwargs['deploy_status'] == 'deployed'
        assert kwargs['file_count'] == len(_FILES_MANIFEST)
        assert kwargs['success_count'] == len(_FILES_MANIFEST)
        assert kwargs['fail_count'] == 0
        assert kwargs['cf_invalidation_failed'] is False

    def test_successful_deploy_puts_item_in_history_table(self):
        """End-to-end: _get_history_table().put_item is called with correct fields."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock()
        mock_table = MagicMock()
        mock_history_table = MagicMock()

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=_make_item())

        mock_history_table.put_item.assert_called_once()
        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['deploy_id'] == 'frontend-{}'.format(_REQUEST_ID)
        assert item['project_id'] == 'ztp-files'
        assert item['status'] == 'SUCCEEDED'
        assert item['deploy_type'] == 'frontend'
        assert item['files_count'] == len(_FILES_MANIFEST)
        assert item['files_deployed'] == len(_FILES_MANIFEST)
        assert item['files_failed'] == 0
        assert item['frontend_bucket'] == _FRONTEND_BUCKET
        assert item['distribution_id'] == _DISTRIBUTION_ID
        assert item['request_id'] == _REQUEST_ID
        assert 'ttl' in item
        assert item['reason'] == 'Sprint 19 deploy test'

    def test_partial_deploy_writes_history_partial(self):
        """Partial failure -> deploy_history with status=PARTIAL."""
        side_effects = [
            {'Body': _make_body_mock()},
            Exception('S3 read error'),
        ]
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock(
            get_object_side_effect=side_effects
        )
        mock_table = MagicMock()
        mock_history_table = MagicMock()

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=_make_item())

        mock_history_table.put_item.assert_called_once()
        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['status'] == 'PARTIAL'
        assert item['files_deployed'] == 1
        assert item['files_failed'] == 1

    def test_full_failure_writes_history_failed(self):
        """All files fail -> deploy_history with status=FAILED."""
        side_effects = [Exception('S3 error')] * len(_FILES_MANIFEST)
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock(
            get_object_side_effect=side_effects
        )
        mock_table = MagicMock()
        mock_history_table = MagicMock()

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=_make_item())

        mock_history_table.put_item.assert_called_once()
        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['status'] == 'FAILED'
        assert item['files_deployed'] == 0
        assert item['files_failed'] == len(_FILES_MANIFEST)

    def test_history_write_failure_does_not_break_callback(self):
        """If deploy_history write fails, callback still returns ok=True (non-critical)."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock()
        mock_table = MagicMock()
        mock_history_table = MagicMock()
        mock_history_table.put_item.side_effect = Exception('DDB write error')

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            result = _call_callback(action='approve', item=_make_item())

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['ok'] is True

    def test_deny_action_does_not_write_history(self):
        """deny action should NOT write to deploy_history."""
        mock_table = MagicMock()
        mock_history_table = MagicMock()

        with patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            result = _call_callback(action='deny', item=_make_item())

        mock_history_table.put_item.assert_not_called()
        assert result['statusCode'] == 200

    def test_history_triggered_by_user_id(self):
        """triggered_by in history should match user_id from callback."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock()
        mock_table = MagicMock()
        mock_history_table = MagicMock()

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            _call_callback(
                action='approve',
                item=_make_item(),
                user_id='steven-12345',
            )

        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['triggered_by'] == 'steven-12345'

    def test_deploy_id_format(self):
        """deploy_id must be 'frontend-{request_id}' for easy identification."""
        mock_boto3, mock_s3_target, mock_s3_staging, mock_cf = _make_boto3_mock()
        mock_table = MagicMock()
        mock_history_table = MagicMock()

        _s3_seq = [mock_s3_target, mock_s3_staging]
        _s3_idx = {'n': 0}
        def _s3_f(role_arn=None, **kw): i = _s3_idx['n']; _s3_idx['n'] += 1; return _s3_seq[i] if i < len(_s3_seq) else MagicMock()
        with patch('callbacks.get_s3_client', side_effect=_s3_f), \
             patch('callbacks.get_cloudfront_client', return_value=mock_cf), \
             patch('callbacks._get_table', return_value=mock_table), \
             patch('callbacks.answer_callback'), \
             patch('callbacks.update_message'), \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._get_history_table', return_value=mock_history_table), \
             patch('telegram.send_message_with_entities'):
            _call_callback(action='approve', item=_make_item())

        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['deploy_id'].startswith('frontend-')
        assert _REQUEST_ID in item['deploy_id']


class TestWriteFrontendDeployHistoryUnit:
    """Unit tests for _write_frontend_deploy_history helper directly."""

    def test_function_importable(self):
        """_write_frontend_deploy_history must be importable from callbacks."""
        from callbacks import _write_frontend_deploy_history
        assert callable(_write_frontend_deploy_history)

    def test_direct_call_success(self):
        """Direct call writes correct item to history table."""
        from callbacks import _write_frontend_deploy_history
        mock_history_table = MagicMock()

        with patch('callbacks._get_history_table', return_value=mock_history_table):
            _write_frontend_deploy_history(
                request_id='test-req-001',
                project='my-project',
                deploy_status='deployed',
                user_id='user-001',
                file_count=5,
                success_count=5,
                fail_count=0,
                reason='Test deploy',
                source='Test Source',
                frontend_bucket='my-bucket',
                distribution_id='EDIST001',
                cf_invalidation_failed=False,
            )

        mock_history_table.put_item.assert_called_once()
        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['deploy_id'] == 'frontend-test-req-001'
        assert item['project_id'] == 'my-project'
        assert item['status'] == 'SUCCEEDED'
        assert item['deploy_type'] == 'frontend'
        assert item['files_count'] == 5
        assert item['files_deployed'] == 5
        assert item['files_failed'] == 0
        assert item['cf_invalidation_failed'] is False
        assert item['ttl'] > 0

    def test_no_none_values_in_item(self):
        """put_item Item must not contain None (DynamoDB constraint)."""
        from callbacks import _write_frontend_deploy_history
        mock_history_table = MagicMock()

        with patch('callbacks._get_history_table', return_value=mock_history_table):
            _write_frontend_deploy_history(
                request_id='test-req-002',
                project='proj',
                deploy_status='deploy_failed',
                user_id='user-002',
                file_count=2,
                success_count=0,
                fail_count=2,
                reason='',
                source='',
                frontend_bucket='bucket',
                distribution_id='DIST',
                cf_invalidation_failed=False,
            )

        item = mock_history_table.put_item.call_args[1]['Item']
        for k, v in item.items():
            assert v is not None, 'Field {!r} must not be None'.format(k)

    def test_status_mapping_partial(self):
        """partial_deploy -> PARTIAL."""
        from callbacks import _write_frontend_deploy_history
        mock_history_table = MagicMock()

        with patch('callbacks._get_history_table', return_value=mock_history_table):
            _write_frontend_deploy_history(
                request_id='test-req-003',
                project='proj',
                deploy_status='partial_deploy',
                user_id='u',
                file_count=3,
                success_count=2,
                fail_count=1,
                reason='',
                source='',
                frontend_bucket='b',
                distribution_id='d',
                cf_invalidation_failed=False,
            )

        item = mock_history_table.put_item.call_args[1]['Item']
        assert item['status'] == 'PARTIAL'

    def test_exception_is_caught_silently(self):
        """If _get_history_table raises, function does not propagate exception."""
        from callbacks import _write_frontend_deploy_history

        with patch('callbacks._get_history_table', side_effect=Exception('DDB down')):
            # Should not raise
            _write_frontend_deploy_history(
                request_id='test-req-004',
                project='proj',
                deploy_status='deployed',
                user_id='u',
                file_count=1,
                success_count=1,
                fail_count=0,
                reason='',
                source='',
                frontend_bucket='b',
                distribution_id='d',
                cf_invalidation_failed=False,
            )
