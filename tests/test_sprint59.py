"""Tests for Sprint 59: Approval Pending Reminder & Trust Rate Detection."""

import os
import sys
import json
import time
from unittest.mock import patch, MagicMock

import pytest
import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'  # Must override any existing value
os.environ.setdefault('TRUST_RATE_LIMIT_ENABLED', 'true')
os.environ.setdefault('SCHEDULER_ENABLED', 'false')  # Disable scheduler for tests

pytestmark = pytest.mark.xdist_group("sprint59")


def _create_mock_table(dynamodb):
    table = dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[
            {'AttributeName': 'request_id', 'AttributeType': 'S'},
            {'AttributeName': 'status', 'AttributeType': 'S'},
            {'AttributeName': 'created_at', 'AttributeType': 'N'},
        ],
        GlobalSecondaryIndexes=[
            {
                'IndexName': 'status-created-index',
                'KeySchema': [
                    {'AttributeName': 'status', 'KeyType': 'HASH'},
                    {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                ],
                'Projection': {'ProjectionType': 'ALL'},
            }
        ],
        BillingMode='PAY_PER_REQUEST',
    )
    table.wait_until_exists()
    return table


# =============================================================================
# s59-001: Approval Pending Reminder Tests
# =============================================================================

class TestS59_001_PendingReminder:
    """Test s59-001: Approval Pending Reminder functionality."""

    def test_create_pending_reminder_schedule_normal(self):
        """Test creating a pending reminder schedule with normal parameters."""
        from scheduler_service import SchedulerService

        mock_scheduler_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_scheduler_client,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:bouncer',
            role_arn='arn:aws:iam::123456789012:role/scheduler-role',
            group_name='test-group',
            enabled=True,
        )

        now = int(time.time())
        expires_at = now + 600  # 10 minutes

        result = svc.create_pending_reminder_schedule(
            request_id='test-req-123',
            expires_at=expires_at,
            reminder_minutes=5,  # 5 minutes after creation
            command_preview='aws s3 ls',
            source='test-source',
        )

        assert result is True
        mock_scheduler_client.create_schedule.assert_called_once()
        call_args = mock_scheduler_client.create_schedule.call_args
        assert 'bouncer-remind-' in call_args[1]['Name']
        payload = json.loads(call_args[1]['Target']['Input'])
        assert payload['action'] == 'pending_reminder'
        assert payload['request_id'] == 'test-req-123'

    def test_create_pending_reminder_schedule_too_late(self):
        """Test that reminder is skipped if it would fire after expiry."""
        from scheduler_service import SchedulerService

        mock_scheduler_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_scheduler_client,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:bouncer',
            role_arn='arn:aws:iam::123456789012:role/scheduler-role',
            group_name='test-group',
            enabled=True,
        )

        now = int(time.time())
        expires_at = now + 300  # 5 minutes (< 10 minutes default reminder)

        result = svc.create_pending_reminder_schedule(
            request_id='test-req-123',
            expires_at=expires_at,
            reminder_minutes=10,  # 10 minutes > 5 minutes TTL
        )

        assert result is False
        mock_scheduler_client.create_schedule.assert_not_called()

    @pytest.mark.xfail(reason="xdist loadgroup env var pollution — tracked in #238")
    def test_pending_reminder_handler_still_pending(self):
        """Test pending_reminder handler sends notification when status is still pending."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            # Create a pending approval request
            now = int(time.time())
            table.put_item(Item={
                'request_id': 'test-pending-123',
                'status': 'pending_approval',
                'command': 'aws s3 ls s3://my-bucket',
                'source': 'test-source',
                'expires_at': now + 300,
                'created_at': now,
            })

            # Mock the app handler
            import src.db as db_mod
            db_mod.table = table

            event = {
                'source': 'bouncer-scheduler',
                'action': 'pending_reminder',
                'request_id': 'test-pending-123',
                'command_preview': 'aws s3 ls',
                'source_field': 'test-source',
            }

            with patch('telegram.send_telegram_message_silent') as mock_send:
                from app import lambda_handler
                response = lambda_handler(event, {})

                assert response['statusCode'] == 200
                mock_send.assert_called_once()
                call_args = mock_send.call_args[0][0]
                assert '尚未審批的請求' in call_args
                assert 'test-pending-123' in call_args

    @pytest.mark.xfail(reason="xdist loadgroup env var pollution — tracked in #238")
    def test_pending_reminder_handler_already_approved(self):
        """Test pending_reminder handler skips notification when already approved."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            # Create an already-approved request
            now = int(time.time())
            table.put_item(Item={
                'request_id': 'test-approved-123',
                'status': 'approved',
                'command': 'aws s3 ls s3://my-bucket',
                'source': 'test-source',
                'created_at': now,
            })

            import src.db as db_mod
            db_mod.table = table

            event = {
                'source': 'bouncer-scheduler',
                'action': 'pending_reminder',
                'request_id': 'test-approved-123',
                'command_preview': 'aws s3 ls',
                'source_field': 'test-source',
            }

            with patch('telegram.send_telegram_message_silent') as mock_send:
                from app import lambda_handler
                response = lambda_handler(event, {})

                assert response['statusCode'] == 200
                body = json.loads(response['body'])
                assert body['status'] == 'skipped'
                mock_send.assert_not_called()

    def test_delete_reminder_schedule(self):
        """Test deleting a reminder schedule."""
        from scheduler_service import SchedulerService

        mock_scheduler_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_scheduler_client,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:bouncer',
            role_arn='arn:aws:iam::123456789012:role/scheduler-role',
            group_name='test-group',
            enabled=True,
        )

        result = svc.delete_reminder_schedule('test-req-123')

        assert result is True
        mock_scheduler_client.delete_schedule.assert_called_once()
        call_args = mock_scheduler_client.delete_schedule.call_args
        assert 'bouncer-remind-' in call_args[1]['Name']


# =============================================================================
# s59-002: Trust Session Command Rate Detection Tests
# =============================================================================

class TestS59_002_TrustRateDetection:
    """Test s59-002: Trust Session Command Rate Detection."""

    def test_trust_rate_within_limit(self):
        """Test that normal rate is allowed."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            trust_id = 'trust-test-123'

            # Create trust session
            table.put_item(Item={
                'request_id': trust_id,
                'type': 'trust_session',
                'command_count': 0,
                'expires_at': now + 600,
                'rate_window_start': now,
                'rate_window_count': 2,  # 2 commands in current window (< 5 limit)
            })

            import src.trust as trust_mod
            trust_mod._table = table

            # Should succeed
            from trust import increment_trust_command_count
            new_count = increment_trust_command_count(trust_id)

            assert new_count > 0

    def test_trust_rate_exceeded(self):
        """Test that rate exceeded raises exception."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            trust_id = 'trust-test-456'

            # Create trust session with rate window at limit
            table.put_item(Item={
                'request_id': trust_id,
                'type': 'trust_session',
                'command_count': 5,
                'expires_at': now + 600,
                'rate_window_start': now,
                'rate_window_count': 5,  # Already at 5 commands limit
            })

            import src.trust as trust_mod
            trust_mod._table = table

            from trust import increment_trust_command_count, TrustRateExceeded

            # Should raise TrustRateExceeded
            with pytest.raises(TrustRateExceeded):
                increment_trust_command_count(trust_id)

    def test_trust_rate_window_resets(self):
        """Test that rate window resets after 60 seconds."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            trust_id = 'trust-test-789'

            # Create trust session with old window (> 60s ago)
            old_window_start = now - 70  # 70 seconds ago
            table.put_item(Item={
                'request_id': trust_id,
                'type': 'trust_session',
                'command_count': 5,
                'expires_at': now + 600,
                'rate_window_start': old_window_start,
                'rate_window_count': 5,  # Was at limit in old window
            })

            import src.trust as trust_mod
            trust_mod._table = table

            from trust import increment_trust_command_count

            # Should succeed because window has reset
            new_count = increment_trust_command_count(trust_id)
            assert new_count > 0

    def test_trust_rate_disabled(self):
        """Test that rate limiting can be disabled via TRUST_RATE_LIMIT_ENABLED=False."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            trust_id = 'trust-test-disabled'

            # Create trust session already at rate limit
            table.put_item(Item={
                'request_id': trust_id,
                'type': 'trust_session',
                'command_count': 5,
                'expires_at': now + 600,
                'rate_window_start': now,
                'rate_window_count': 10,  # Way over limit
            })

            import src.trust as trust_mod
            trust_mod._table = table

            from trust import increment_trust_command_count

            # Patch TRUST_RATE_LIMIT_ENABLED at the trust module level
            # (env var reload won't work since constant is already imported)
            with patch('trust.TRUST_RATE_LIMIT_ENABLED', False):
                new_count = increment_trust_command_count(trust_id)
                assert new_count > 0, "Rate limit disabled should allow command"

    def test_mcp_execute_catches_rate_exceeded(self):
        """Test that mcp_execute properly catches TrustRateExceeded."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.trust as trust_mod
            trust_mod._table = table

            # Mock increment to raise TrustRateExceeded
            from trust import TrustRateExceeded

            with patch('mcp_execute.increment_trust_command_count', side_effect=TrustRateExceeded('Rate exceeded')):
                with patch('mcp_execute.should_trust_approve', return_value=(True, {'request_id': 'trust-123'}, 'ok')):
                    with patch('mcp_execute.emit_metric') as mock_metric:
                        from mcp_execute import _check_trust_session

                        ctx = MagicMock()
                        ctx.command = 'aws s3 ls'
                        ctx.req_id = 'req-123'
                        ctx.trust_scope = 'test-scope'
                        ctx.account_id = '123456789012'
                        ctx.source = 'test-source'
                        ctx.caller_ip = '127.0.0.1'

                        result = _check_trust_session(ctx)

                        # Should return MCP error result (not None)
                        assert result is not None
                        body = json.loads(result['body'])
                        assert 'error' in body
                        assert body['error']['code'] == 'TRUST_RATE_EXCEEDED'

                        # Should emit TrustRateExceeded metric
                        mock_metric.assert_called()
                        call_args = [call for call in mock_metric.call_args_list if 'TrustRateExceeded' in str(call)]
                        assert len(call_args) > 0
