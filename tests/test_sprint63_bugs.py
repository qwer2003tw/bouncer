"""Regression tests for Sprint 63 bug fixes."""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

pytestmark = pytest.mark.xdist_group("sprint63")


def _create_mock_table(dynamodb):
    table = dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[
            {'AttributeName': 'request_id', 'AttributeType': 'S'},
            {'AttributeName': 'user_id', 'AttributeType': 'S'},
            {'AttributeName': 'created_at', 'AttributeType': 'N'},
        ],
        GlobalSecondaryIndexes=[{
            'IndexName': 'user-id-created-index',
            'KeySchema': [
                {'AttributeName': 'user_id', 'KeyType': 'HASH'},
                {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
            ],
            'Projection': {'ProjectionType': 'ALL'},
        }],
        BillingMode='PAY_PER_REQUEST',
    )
    table.wait_until_exists()
    return table


class TestS63_001_OTPPaginationFix:
    """Bug s63-001: get_pending_otp() missing DynamoDB scan pagination."""

    def test_get_pending_otp_handles_pagination(self):
        """Test that get_pending_otp properly handles LastEvaluatedKey pagination."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            # Create mock query results that simulate pagination
            # First page returns one item and LastEvaluatedKey
            # Second page returns the matching OTP record
            now = int(time.time())
            target_otp = {
                'request_id': 'otp#req-target',
                'otp_code': '123456',
                'user_id': 'user123',
                'original_request_id': 'req-target',
                'message_id': 100,
                'attempts': 0,
                'created_at': now,
                'ttl': now + 300,
                'type': 'otp_pending',
            }

            # Mock table.query to return paginated results
            call_count = [0]

            def mock_query(**kwargs):
                # Verify GSI usage
                assert kwargs.get('IndexName') == 'user-id-created-index', "Should use user-id-created-index GSI"
                call_count[0] += 1
                if call_count[0] == 1:
                    # First page: return empty items with LastEvaluatedKey
                    return {
                        'Items': [],
                        'LastEvaluatedKey': {'request_id': 'otp#dummy', 'user_id': 'user123', 'created_at': now},
                        'Count': 0,
                        'ScannedCount': 100,
                    }
                else:
                    # Second page: return the actual OTP record
                    return {
                        'Items': [target_otp],
                        'Count': 1,
                        'ScannedCount': 100,
                    }

            table.query = mock_query

            # Call get_pending_otp - should handle pagination
            result = otp_mod.get_pending_otp('user123')

            # Verify pagination was handled (query called twice)
            assert call_count[0] == 2, "query should be called twice for pagination"
            assert result is not None, "Should find OTP in second page"
            assert result['request_id'] == 'otp#req-target'
            assert result['otp_code'] == '123456'

    def test_get_pending_otp_returns_most_recent_across_pages(self):
        """Test that get_pending_otp returns most recent OTP across multiple pages."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            now = int(time.time())

            # Newer OTP should be in first page (ScanIndexForward=False sorts desc)
            new_otp = {
                'request_id': 'otp#req-new',
                'otp_code': '222222',
                'user_id': 'user123',
                'original_request_id': 'req-new',
                'message_id': 101,
                'attempts': 0,
                'created_at': now,
                'ttl': now + 300,
                'type': 'otp_pending',
            }

            # Older OTP in second page
            old_otp = {
                'request_id': 'otp#req-old',
                'otp_code': '111111',
                'user_id': 'user123',
                'original_request_id': 'req-old',
                'message_id': 100,
                'attempts': 0,
                'created_at': now - 100,
                'ttl': now + 200,
                'type': 'otp_pending',
            }

            call_count = [0]

            def mock_query(**kwargs):
                # Verify GSI usage and descending sort
                assert kwargs.get('IndexName') == 'user-id-created-index', "Should use user-id-created-index GSI"
                assert kwargs.get('ScanIndexForward') is False, "Should sort descending (newest first)"
                call_count[0] += 1
                if call_count[0] == 1:
                    return {
                        'Items': [new_otp],
                        'LastEvaluatedKey': {'request_id': 'otp#req-new', 'user_id': 'user123', 'created_at': now},
                        'Count': 1,
                        'ScannedCount': 100,
                    }
                else:
                    return {
                        'Items': [old_otp],
                        'Count': 1,
                        'ScannedCount': 100,
                    }

            table.query = mock_query

            result = otp_mod.get_pending_otp('user123')

            # Should return the newer OTP (first item from descending sort)
            assert result is not None
            assert result['otp_code'] == '222222', "Should return most recent OTP"
            assert result['created_at'] == now

    def test_get_pending_otp_logs_info_when_found(self):
        """Test that get_pending_otp logs INFO when OTP is found."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            now = int(time.time())
            table.put_item(Item={
                'request_id': 'otp#req-001',
                'otp_code': '123456',
                'user_id': 'user123',
                'original_request_id': 'req-001',
                'message_id': 100,
                'attempts': 0,
                'created_at': now,
                'ttl': now + 300,
                'type': 'otp_pending',
            })

            with patch.object(otp_mod.logger, 'info') as mock_info:
                result = otp_mod.get_pending_otp('user123')

                # Verify INFO log was called
                assert mock_info.called, "logger.info should be called"
                # Check that the log contains expected fields
                call_args = mock_info.call_args
                assert 'Found pending OTP' in call_args[0][0]
                assert call_args[1]['extra']['found'] is True
                assert call_args[1]['extra']['request_id'] == 'req-001'

    def test_get_pending_otp_logs_info_when_not_found(self):
        """Test that get_pending_otp logs INFO when no OTP is found."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            with patch.object(otp_mod.logger, 'info') as mock_info:
                result = otp_mod.get_pending_otp('user999')

                # Verify INFO log was called
                assert mock_info.called, "logger.info should be called"
                call_args = mock_info.call_args
                assert 'No pending OTP found' in call_args[0][0]
                assert call_args[1]['extra']['found'] is False


class TestS63_001_OTPLogging:
    """Bug s63-001: Add INFO logs for OTP operations."""

    def test_create_otp_record_logs_info(self):
        """Test that create_otp_record logs INFO after creating record."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            with patch.object(otp_mod.logger, 'info') as mock_info:
                otp_mod.create_otp_record('req-001', 'user123', '123456', message_id=100)

                # Verify INFO log was called
                assert mock_info.called, "logger.info should be called"
                call_args = mock_info.call_args
                assert 'OTP record created' in call_args[0][0]
                assert call_args[1]['extra']['request_id'] == 'req-001'
                assert call_args[1]['extra']['user_id'] == 'user123'

    def test_validate_otp_logs_info_on_success(self):
        """Test that validate_otp logs INFO on successful validation."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            # Create OTP record
            otp_mod.create_otp_record('req-001', 'user123', '123456', message_id=100)

            with patch.object(otp_mod.logger, 'info') as mock_info:
                success, msg = otp_mod.validate_otp('req-001', '123456')

                # Verify success
                assert success is True

                # Verify INFO log was called (should be called twice: once in create, once in validate)
                assert mock_info.call_count >= 1
                # Check the last call (validate_otp's log)
                last_call = mock_info.call_args
                assert 'OTP validated successfully' in last_call[0][0]
                assert last_call[1]['extra']['request_id'] == 'req-001'
