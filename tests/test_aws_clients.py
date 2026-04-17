"""
Tests for src/aws_clients.py - S3 client factory.

Mocks aws_clients.boto3 to verify the factory correctly:
1. Returns a plain S3 client when role_arn is None.
2. Assumes the specified role and returns credentialed S3 client.
3. Passes region_name when specified.
4. Passes session_name to assume_role.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, call

# Ensure src is on sys.path

import aws_clients

# Force all tests in this module to run in the same xdist worker
# to prevent boto3 mock pollution from other test files
pytestmark = pytest.mark.xdist_group("aws_clients")


@pytest.fixture(autouse=True)
def _clean_aws_clients_cache():
    """Reset aws_clients module-level caches before each test."""
    # Clear any cached clients to prevent xdist cross-worker pollution
    for attr in list(vars(aws_clients)):
        if attr.startswith('_cached'):
            setattr(aws_clients, attr, None)
    yield


FAKE_CREDS = {
    'AccessKeyId': 'AKIAIOSFODNN7EXAMPLE',
    'SecretAccessKey': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
    'SessionToken': 'FQoGZXIvYXdzEJr//////////fake',
}

ROLE_ARN = 'arn:aws:iam::123456789012:role/TestRole'


class TestGetS3ClientNoRole:
    """get_s3_client() without role_arn."""

    def test_returns_s3_client_without_region(self):
        """No role, no region → boto3.client('s3') called once, no STS."""
        with patch('aws_clients.boto3') as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3

            result = aws_clients.get_s3_client()

        assert result is mock_s3
        mock_boto3.client.assert_called_once_with('s3')
        # STS must NOT be called
        for c in mock_boto3.client.call_args_list:
            assert c.args[0] != 'sts', "STS should not be called without role_arn"

    def test_returns_s3_client_with_region(self):
        """No role but region provided → boto3.client('s3', region_name=...) called."""
        with patch('aws_clients.boto3') as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3

            result = aws_clients.get_s3_client(region='ap-east-1')

        assert result is mock_s3
        mock_boto3.client.assert_called_once_with('s3', region_name='ap-east-1')


class TestGetS3ClientWithRole:
    """get_s3_client() with role_arn."""

    def _make_boto3_mock(self):
        """Return a boto3 mock where client() returns different objects for 'sts' and 's3'."""
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {'Credentials': FAKE_CREDS}

        mock_s3 = MagicMock()

        def _client_factory(service, **kwargs):
            if service == 'sts':
                return mock_sts
            return mock_s3

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = _client_factory
        return mock_boto3, mock_sts, mock_s3

    def test_assumes_role_and_returns_credentialed_s3(self):
        """Role provided → STS assume_role called, S3 client created with temp creds."""
        mock_boto3, mock_sts, mock_s3 = self._make_boto3_mock()

        with patch('aws_clients.boto3', mock_boto3):
            result = aws_clients.get_s3_client(role_arn=ROLE_ARN)

        assert result is mock_s3
        mock_sts.assume_role.assert_called_once_with(
            RoleArn=ROLE_ARN,
            RoleSessionName='bouncer-s3',
        )
        mock_boto3.client.assert_any_call(
            's3',
            aws_access_key_id=FAKE_CREDS['AccessKeyId'],
            aws_secret_access_key=FAKE_CREDS['SecretAccessKey'],
            aws_session_token=FAKE_CREDS['SessionToken'],
        )

    def test_custom_session_name(self):
        """Custom session_name is forwarded to assume_role."""
        mock_boto3, mock_sts, _ = self._make_boto3_mock()

        with patch('aws_clients.boto3', mock_boto3):
            aws_clients.get_s3_client(role_arn=ROLE_ARN, session_name='my-custom-session')

        mock_sts.assume_role.assert_called_once_with(
            RoleArn=ROLE_ARN,
            RoleSessionName='my-custom-session',
        )

    def test_region_included_in_s3_client_with_role(self):
        """Region forwarded to S3 client when role_arn provided."""
        mock_boto3, mock_sts, mock_s3 = self._make_boto3_mock()

        with patch('aws_clients.boto3', mock_boto3):
            result = aws_clients.get_s3_client(role_arn=ROLE_ARN, region='us-west-2')

        assert result is mock_s3
        mock_boto3.client.assert_any_call(
            's3',
            aws_access_key_id=FAKE_CREDS['AccessKeyId'],
            aws_secret_access_key=FAKE_CREDS['SecretAccessKey'],
            aws_session_token=FAKE_CREDS['SessionToken'],
            region_name='us-west-2',
        )

    def test_no_region_not_passed_to_s3(self):
        """region_name must NOT appear in the S3 call when region=None."""
        mock_boto3, mock_sts, mock_s3 = self._make_boto3_mock()

        with patch('aws_clients.boto3', mock_boto3):
            aws_clients.get_s3_client(role_arn=ROLE_ARN)

        s3_call = [c for c in mock_boto3.client.call_args_list if c.args[0] == 's3'][0]
        assert 'region_name' not in s3_call.kwargs, \
            "region_name should not be in S3 call when region=None"


class TestGetS3ClientCallerIsolation:
    """Confirm that mocking aws_clients.boto3 does NOT bleed into other modules."""

    def test_mock_is_scoped_to_aws_clients(self):
        """Patching aws_clients.boto3 leaves the top-level boto3 module unaffected."""
        import boto3 as real_boto3
        sentinel = MagicMock()
        sentinel.client.return_value = MagicMock()
        sentinel.client.return_value.assume_role.return_value = {'Credentials': FAKE_CREDS}

        with patch('aws_clients.boto3', sentinel):
            # Outside the factory, boto3 should still be the real thing
            assert sys.modules['boto3'] is real_boto3


class TestGetCloudfrontClient:
    """get_cloudfront_client() basic tests."""

    def test_returns_cf_client_without_role(self):
        with patch('aws_clients.boto3') as mock_boto3:
            mock_cf = MagicMock()
            mock_boto3.client.return_value = mock_cf
            result = aws_clients.get_cloudfront_client()
        assert result is mock_cf
        mock_boto3.client.assert_called_once_with('cloudfront')

    def test_assumes_role_for_cf(self):
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {'Credentials': FAKE_CREDS}
        mock_cf = MagicMock()

        def _cf(service, **kwargs):
            return mock_sts if service == 'sts' else mock_cf

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = _cf

        with patch('aws_clients.boto3', mock_boto3):
            result = aws_clients.get_cloudfront_client(role_arn=ROLE_ARN, session_name='bouncer-deploy')

        assert result is mock_cf
        mock_sts.assume_role.assert_called_once_with(
            RoleArn=ROLE_ARN,
            RoleSessionName='bouncer-deploy',
        )
        mock_boto3.client.assert_any_call(
            'cloudfront',
            aws_access_key_id=FAKE_CREDS['AccessKeyId'],
            aws_secret_access_key=FAKE_CREDS['SecretAccessKey'],
            aws_session_token=FAKE_CREDS['SessionToken'],
        )


class TestGetS3ClientFactoryConsistency:
    """Sprint 58 s58-003: Verify factory returns consistent client objects."""

    def test_multiple_calls_create_new_clients(self):
        """Each call to get_s3_client() should create a new boto3 client."""
        with patch('aws_clients.boto3') as mock_boto3:
            mock_s3_1 = MagicMock()
            mock_s3_2 = MagicMock()
            mock_boto3.client.side_effect = [mock_s3_1, mock_s3_2]

            result1 = aws_clients.get_s3_client()
            result2 = aws_clients.get_s3_client()

        assert result1 is mock_s3_1
        assert result2 is mock_s3_2
        assert mock_boto3.client.call_count == 2

    def test_factory_accepts_all_expected_params(self):
        """Factory should accept role_arn, session_name, region without error."""
        with patch('aws_clients.boto3') as mock_boto3:
            mock_sts = MagicMock()
            mock_sts.assume_role.return_value = {'Credentials': FAKE_CREDS}
            mock_s3 = MagicMock()

            def _factory(service, **kwargs):
                return mock_sts if service == 'sts' else mock_s3

            mock_boto3.client.side_effect = _factory

            # Should not raise
            result = aws_clients.get_s3_client(
                role_arn=ROLE_ARN,
                session_name='test-session',
                region='us-east-1'
            )

        assert result is mock_s3
