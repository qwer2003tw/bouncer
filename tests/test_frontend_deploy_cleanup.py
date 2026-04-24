"""Tests for frontend deploy stale asset cleanup (#429)."""
import os
import sys
import pytest
from unittest.mock import Mock, MagicMock, patch, call

# Setup path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('REQUESTS_TABLE_NAME', 'bouncer-test-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'fake-token')
os.environ.setdefault('APPROVED_CHAT_ID', '123456789')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')


class TestFrontendDeployCleanup:
    """Tests for stale asset cleanup after frontend deployment."""

    def test_cleanup_deletes_stale_assets(self):
        """Deploy 3 files, bucket has 5 assets -> cleanup deletes 2 stale ones."""
        from callbacks import _cleanup_stale_assets

        # Mock S3 client
        s3_client = Mock()
        paginator = Mock()
        s3_client.get_paginator.return_value = paginator

        # Simulate bucket has 5 assets, but only 3 were just deployed
        paginator.paginate.return_value = [
            {
                'Contents': [
                    {'Key': 'assets/index-abc123.js', 'Size': 1000},  # deployed
                    {'Key': 'assets/main-def456.js', 'Size': 2000},   # deployed
                    {'Key': 'assets/vendor-ghi789.js', 'Size': 3000}, # deployed
                    {'Key': 'assets/old-file-1.js', 'Size': 500},     # stale
                    {'Key': 'assets/old-file-2.js', 'Size': 600},     # stale
                ]
            }
        ]

        deployed_keys = {
            'assets/index-abc123.js',
            'assets/main-def456.js',
            'assets/vendor-ghi789.js',
        }

        # Call cleanup
        result = _cleanup_stale_assets(
            s3_client,
            'test-frontend-bucket',
            deployed_keys,
            'test-request-123',
            'test-project'
        )

        # Verify: should delete 2 stale files
        assert result['deleted_count'] == 2
        assert result['deleted_bytes'] == 1100  # 500 + 600
        assert result['errors'] == []

        # Verify delete_objects was called with correct keys
        s3_client.delete_objects.assert_called_once()
        call_args = s3_client.delete_objects.call_args
        assert call_args[1]['Bucket'] == 'test-frontend-bucket'
        deleted_keys = {obj['Key'] for obj in call_args[1]['Delete']['Objects']}
        assert deleted_keys == {'assets/old-file-1.js', 'assets/old-file-2.js'}

    def test_cleanup_no_stale_files(self):
        """Deploy 3 files, bucket has exactly those 3 -> no deletion."""
        from callbacks import _cleanup_stale_assets

        s3_client = Mock()
        paginator = Mock()
        s3_client.get_paginator.return_value = paginator

        # Bucket has exactly the deployed files
        paginator.paginate.return_value = [
            {
                'Contents': [
                    {'Key': 'assets/index-abc123.js', 'Size': 1000},
                    {'Key': 'assets/main-def456.js', 'Size': 2000},
                    {'Key': 'assets/vendor-ghi789.js', 'Size': 3000},
                ]
            }
        ]

        deployed_keys = {
            'assets/index-abc123.js',
            'assets/main-def456.js',
            'assets/vendor-ghi789.js',
        }

        result = _cleanup_stale_assets(
            s3_client,
            'test-frontend-bucket',
            deployed_keys,
            'test-request-123',
            'test-project'
        )

        # Should not delete anything
        assert result['deleted_count'] == 0
        assert result['deleted_bytes'] == 0
        assert result['errors'] == []
        s3_client.delete_objects.assert_not_called()

    def test_cleanup_failure_does_not_crash(self):
        """Cleanup fails -> deploy not affected (best-effort)."""
        from callbacks import _cleanup_stale_assets

        s3_client = Mock()
        s3_client.get_paginator.side_effect = Exception("S3 paginator error")

        deployed_keys = {'assets/index-abc123.js'}

        result = _cleanup_stale_assets(
            s3_client,
            'test-frontend-bucket',
            deployed_keys,
            'test-request-123',
            'test-project'
        )

        # Should return error but not crash
        assert result['deleted_count'] == 0
        assert result['deleted_bytes'] == 0
        assert len(result['errors']) > 0
        assert 'S3 paginator error' in result['errors'][0]

    def test_cleanup_does_not_touch_root_files(self):
        """Cleanup only affects assets/ prefix, not root files like index.html."""
        from callbacks import _cleanup_stale_assets

        s3_client = Mock()
        paginator = Mock()
        s3_client.get_paginator.return_value = paginator

        # Simulate paginate only returns assets/ prefix (as per code)
        # Root files should not appear in results
        paginator.paginate.return_value = [
            {
                'Contents': [
                    {'Key': 'assets/old-file.js', 'Size': 500},
                ]
            }
        ]

        deployed_keys = set()  # Nothing deployed in assets/

        result = _cleanup_stale_assets(
            s3_client,
            'test-frontend-bucket',
            deployed_keys,
            'test-request-123',
            'test-project'
        )

        # Should delete the stale asset file
        assert result['deleted_count'] == 1
        # Verify paginate was called with assets/ prefix
        paginator.paginate.assert_called_once_with(
            Bucket='test-frontend-bucket',
            Prefix='assets/'
        )

    def test_cleanup_batch_delete_over_1000_files(self):
        """Cleanup handles >1000 files by batching delete_objects calls."""
        from callbacks import _cleanup_stale_assets

        s3_client = Mock()
        paginator = Mock()
        s3_client.get_paginator.return_value = paginator

        # Generate 1500 stale files
        stale_files = [
            {'Key': f'assets/old-file-{i}.js', 'Size': 100}
            for i in range(1500)
        ]
        paginator.paginate.return_value = [{'Contents': stale_files}]

        deployed_keys = set()  # No current deployed files

        result = _cleanup_stale_assets(
            s3_client,
            'test-frontend-bucket',
            deployed_keys,
            'test-request-123',
            'test-project'
        )

        # Should delete all 1500 files
        assert result['deleted_count'] == 1500
        assert result['deleted_bytes'] == 150000  # 1500 * 100

        # Verify delete_objects was called twice (1000 + 500)
        assert s3_client.delete_objects.call_count == 2

        # First batch: 1000 files
        first_call = s3_client.delete_objects.call_args_list[0]
        assert len(first_call[1]['Delete']['Objects']) == 1000

        # Second batch: 500 files
        second_call = s3_client.delete_objects.call_args_list[1]
        assert len(second_call[1]['Delete']['Objects']) == 500

    def test_deploy_files_to_frontend_calls_cleanup(self):
        """_deploy_files_to_frontend should call cleanup and return cleanup_result."""
        from callbacks import _deploy_files_to_frontend
        from unittest.mock import patch

        # Mock dependencies
        s3_staging = Mock()
        s3_target = Mock()
        s3_target.get_paginator.return_value.paginate.return_value = []  # No stale files

        # Mock get_object to return file content
        s3_staging.get_object.return_value = {
            'Body': Mock(read=lambda: b'test content')
        }

        files_manifest = [
            {
                'filename': 'assets/index-abc.js',
                's3_key': 'pending/req123/assets/index-abc.js',
                'content_type': 'application/javascript',
                'cache_control': 'max-age=31536000, immutable',
            }
        ]

        params = {
            'staging_bucket': 'test-staging',
            'frontend_bucket': 'test-frontend',
            'file_count': 1,
            'project': 'test-project',
        }

        with patch('callbacks.update_message'):
            deployed, failed, total_bytes, cleanup_result = _deploy_files_to_frontend(
                files_manifest,
                s3_staging,
                s3_target,
                'req123',
                12345,
                params,
                'test-user'
            )

        # Verify cleanup_result is returned
        assert cleanup_result is not None
        assert 'deleted_count' in cleanup_result
        assert 'deleted_bytes' in cleanup_result
        assert 'errors' in cleanup_result

        # Verify file was deployed
        assert len(deployed) == 1
        assert deployed[0]['filename'] == 'assets/index-abc.js'

    def test_finalize_includes_cleanup_info_in_message(self):
        """_finalize_deploy_frontend should include cleanup info in notification."""
        from callbacks import _finalize_deploy_frontend
        from unittest.mock import patch, ANY

        deployed = [{'filename': 'assets/index-abc.js', 's3_key': 'assets/index-abc.js'}]
        failed = []
        cf_invalidation_failed = False
        cleanup_result = {
            'deleted_count': 10,
            'deleted_bytes': 5000,
            'errors': []
        }

        table = Mock()
        params = {
            'source_line': '',
            'source': 'test-source',
            'project': 'test-project',
            'file_count': 1,
            'size_str': '100 B',
            'frontend_bucket': 'test-bucket',
            'distribution_id': 'E123456',
            'safe_reason': 'test deploy',
        }
        item = {'reason': 'test'}

        with patch('callbacks.update_message') as mock_update, \
             patch('callbacks._update_request_status'), \
             patch('callbacks.emit_metric'), \
             patch('callbacks._write_frontend_deploy_history'), \
             patch('callbacks._send_message_silent'):

            _finalize_deploy_frontend(
                deployed, failed, cf_invalidation_failed, cleanup_result,
                table, 'req123', 'user123', 12345, params, item
            )

        # Verify update_message was called with cleanup info
        mock_update.assert_called_once()
        message_text = mock_update.call_args[0][1]
        assert '🧹' in message_text
        assert '10 個舊檔案' in message_text
        assert 'KB' in message_text or 'B' in message_text  # Human-readable size


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
