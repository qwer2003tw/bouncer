"""
Sprint 54-001: Deploy lock not released on ClientError — Regression Test

When get_execution_status() encounters a ClientError while fetching SFN execution
status, the deploy lock must be released to prevent permanent lockout.
"""

import sys
import os
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestDeployLockReleaseOnClientError:
    """Test that deploy lock is released when ClientError occurs in get_execution_status."""

    def test_lock_released_on_client_error(self):
        """ClientError in SFN status fetch → release_lock() is called."""
        import deployer

        # Mock record with required fields
        mock_record = {
            'deploy_id': 'deploy-test-001',
            'project_id': 'test-project',
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
        }

        # Mock ClientError from describe_execution
        client_error = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'Not authorized'}},
            'DescribeExecution'
        )

        with patch.object(deployer, 'get_deploy_record', return_value=mock_record):
            with patch.object(deployer, '_get_sfn_client') as mock_sfn:
                # Simulate ClientError when calling describe_execution
                mock_client = MagicMock()
                mock_client.describe_execution.side_effect = client_error
                mock_sfn.return_value = mock_client

                with patch.object(deployer, 'release_lock') as mock_release:
                    result = deployer.get_deploy_status('deploy-test-001')

                    # Verify release_lock was called with the project_id
                    mock_release.assert_called_once_with('test-project', 'deploy-test-001')

        # Result should still return the record (not crash)
        assert result['deploy_id'] == 'deploy-test-001'
        assert result['status'] == 'RUNNING'

    def test_lock_not_released_if_no_project_id(self):
        """ClientError but no project_id → release_lock() not called (no crash)."""
        import deployer

        # Record without project_id
        mock_record = {
            'deploy_id': 'deploy-test-002',
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
        }

        client_error = ClientError(
            {'Error': {'Code': 'Throttling', 'Message': 'Rate exceeded'}},
            'DescribeExecution'
        )

        with patch.object(deployer, 'get_deploy_record', return_value=mock_record):
            with patch.object(deployer, '_get_sfn_client') as mock_sfn:
                mock_client = MagicMock()
                mock_client.describe_execution.side_effect = client_error
                mock_sfn.return_value = mock_client

                with patch.object(deployer, 'release_lock') as mock_release:
                    result = deployer.get_deploy_status('deploy-test-002')

                    # release_lock should NOT be called (no project_id)
                    mock_release.assert_not_called()

        # Should still return the record
        assert result['deploy_id'] == 'deploy-test-002'

    def test_lock_released_only_once_on_error(self):
        """Multiple ClientErrors don't cause multiple release_lock calls."""
        import deployer

        mock_record = {
            'deploy_id': 'deploy-test-003',
            'project_id': 'bouncer',
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
        }

        client_error = ClientError(
            {'Error': {'Code': 'ServiceUnavailable', 'Message': 'Service down'}},
            'DescribeExecution'
        )

        with patch.object(deployer, 'get_deploy_record', return_value=mock_record):
            with patch.object(deployer, '_get_sfn_client') as mock_sfn:
                mock_client = MagicMock()
                mock_client.describe_execution.side_effect = client_error
                mock_sfn.return_value = mock_client

                with patch.object(deployer, 'release_lock') as mock_release:
                    # Call twice
                    deployer.get_deploy_status('deploy-test-003')
                    deployer.get_deploy_status('deploy-test-003')

                    # Should be called once per get_deploy_status call
                    assert mock_release.call_count == 2
                    # Each call should be with the same project_id
                    for call in mock_release.call_args_list:
                        assert call[0][0] == 'bouncer'
