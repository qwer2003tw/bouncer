"""Sprint 60 test suite.

s60-008: template_s3_url format validation
s60-004: Pending reminder escalation (2nd reminder at 3x interval)
"""
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.xdist_group("app_module")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'  # Must override any existing value
os.environ.setdefault('SCHEDULER_ENABLED', 'false')

from src.deployer import validate_template_s3_url  # noqa: E402


def _create_mock_table(dynamodb):
    """Create a mock DynamoDB table for testing."""
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

class TestS60004EscalationSchedule:
    """Tests for s60-004: Pending reminder escalation."""

    def test_escalation_schedule_created(self):
        """Test that both reminder and escalation schedules are created."""
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
        expires_at = now + 3600  # 60 minutes

        result = svc.create_pending_reminder_schedule(
            request_id='test-req-123',
            expires_at=expires_at,
            reminder_minutes=10,  # 1st at 10min, 2nd at 30min
            command_preview='aws s3 ls',
            source='test-source',
        )

        assert result is True
        # Should create 2 schedules: reminder + escalation
        assert mock_scheduler_client.create_schedule.call_count == 2

        # Check 1st call (reminder)
        call1 = mock_scheduler_client.create_schedule.call_args_list[0]
        assert 'bouncer-remind-' in call1[1]['Name']
        payload1 = json.loads(call1[1]['Target']['Input'])
        assert payload1['action'] == 'pending_reminder'
        assert payload1['request_id'] == 'test-req-123'
        assert payload1.get('escalation') is not True  # Not present or False

        # Check 2nd call (escalation)
        call2 = mock_scheduler_client.create_schedule.call_args_list[1]
        assert 'bouncer-escalation-' in call2[1]['Name']
        payload2 = json.loads(call2[1]['Target']['Input'])
        assert payload2['action'] == 'pending_reminder'
        assert payload2['request_id'] == 'test-req-123'
        assert payload2['escalation'] is True

    def test_escalation_not_created_when_too_late(self):
        """Test that escalation is skipped if it would fire after expiry."""
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
        expires_at = now + 1200  # 20 minutes

        result = svc.create_pending_reminder_schedule(
            request_id='test-req-123',
            expires_at=expires_at,
            reminder_minutes=10,  # 1st at 10min, 2nd at 30min (> 20min expiry)
            command_preview='aws s3 ls',
            source='test-source',
        )

        assert result is True
        # Should only create 1 schedule: reminder (escalation at 30min > expires_at at 20min)
        assert mock_scheduler_client.create_schedule.call_count == 1

        call = mock_scheduler_client.create_schedule.call_args
        assert 'bouncer-remind-' in call[1]['Name']
        assert 'bouncer-escalation-' not in call[1]['Name']

    def test_escalation_message_format(self):
        """Test that escalation=True shows red circle header."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            table.put_item(Item={
                'request_id': 'test-escalation-123',
                'status': 'pending_approval',
                'command': 'aws s3 ls s3://my-bucket',
                'source': 'test-source',
                'expires_at': now + 600,
                'created_at': now,
            })

            import src.db as db_mod
            db_mod.table = table

            event = {
                'source': 'bouncer-scheduler',
                'action': 'pending_reminder',
                'request_id': 'test-escalation-123',
                'command_preview': 'aws s3 ls',
                'source_field': 'test-source',
                'escalation': True,  # Escalation flag
            }

            with patch('app.send_telegram_message_silent') as mock_send:
                # Ensure src/app.py is imported (xdist isolation fix)
                import sys
                import os
                src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
                if src_path in sys.path:
                    sys.path.remove(src_path)
                sys.path.insert(0, src_path)
                if 'app' in sys.modules:
                    app_file = getattr(sys.modules['app'], '__file__', '')
                    if 'deployer' in app_file:
                        del sys.modules['app']
                        import app  # Re-import from src/
                from app import lambda_handler
                response = lambda_handler(event, {})

                assert response['statusCode'] == 200
                mock_send.assert_called_once()
                message = mock_send.call_args[0][0]
                assert '🔴' in message
                assert '第 2 次提醒' in message
                assert 'test-escalation-123' in message

    def test_normal_reminder_message_format(self):
        """Test that escalation=False shows clock icon header."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            table.put_item(Item={
                'request_id': 'test-reminder-123',
                'status': 'pending_approval',
                'command': 'aws s3 ls s3://my-bucket',
                'source': 'test-source',
                'expires_at': now + 600,
                'created_at': now,
            })

            import src.db as db_mod
            db_mod.table = table

            event = {
                'source': 'bouncer-scheduler',
                'action': 'pending_reminder',
                'request_id': 'test-reminder-123',
                'command_preview': 'aws s3 ls',
                'source_field': 'test-source',
                # escalation not set (or False)
            }

            with patch('app.send_telegram_message_silent') as mock_send:
                # Ensure src/app.py is imported (xdist isolation fix)
                import sys
                import os
                src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
                if src_path in sys.path:
                    sys.path.remove(src_path)
                sys.path.insert(0, src_path)
                if 'app' in sys.modules:
                    app_file = getattr(sys.modules['app'], '__file__', '')
                    if 'deployer' in app_file:
                        del sys.modules['app']
                        import app  # Re-import from src/
                from app import lambda_handler
                response = lambda_handler(event, {})

                assert response['statusCode'] == 200
                mock_send.assert_called_once()
                message = mock_send.call_args[0][0]
                assert '⏰' in message
                assert '尚未審批的請求' in message
                assert '🔴' not in message  # No escalation marker
                assert 'test-reminder-123' in message

    def test_pending_reminder_minutes_env_var(self):
        """Test that PENDING_REMINDER_MINUTES reads from env var."""
        # Set env var
        with patch.dict(os.environ, {'PENDING_REMINDER_MINUTES': '15'}):
            # Reimport to pick up the env var
            import importlib
            import constants
            importlib.reload(constants)

            assert constants.PENDING_REMINDER_MINUTES == 15

        # Restore default
        import importlib
        import constants
        importlib.reload(constants)

    def test_delete_escalation_schedule(self):
        """Test deleting an escalation schedule."""
        from scheduler_service import SchedulerService

        mock_scheduler_client = MagicMock()
        svc = SchedulerService(
            scheduler_client=mock_scheduler_client,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:bouncer',
            role_arn='arn:aws:iam::123456789012:role/scheduler-role',
            group_name='test-group',
            enabled=True,
        )

        result = svc.delete_escalation_schedule('test-req-123')

        assert result is True
        mock_scheduler_client.delete_schedule.assert_called_once()
        call_args = mock_scheduler_client.delete_schedule.call_args
        assert 'bouncer-escalation-' in call_args[1]['Name']


class TestValidateTemplateS3Url:
    """Test validate_template_s3_url format validation (s60-008)."""

    def test_valid_virtual_hosted_style_url(self):
        url = "https://my-bucket.s3.us-east-1.amazonaws.com/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True
        assert reason == ""

    def test_valid_path_style_url(self):
        url = "https://s3.amazonaws.com/my-bucket/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True

    def test_valid_dash_region_url(self):
        url = "https://s3-us-east-1.amazonaws.com/my-bucket/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True

    def test_empty_url(self):
        is_valid, reason = validate_template_s3_url("")
        assert is_valid is False
        assert "empty" in reason.lower()

    def test_s3_protocol_url(self):
        is_valid, reason = validate_template_s3_url("s3://bucket/template.yaml")
        assert is_valid is False
        assert "https" in reason.lower()

    def test_http_url(self):
        is_valid, reason = validate_template_s3_url("http://bucket.s3.amazonaws.com/t.yaml")
        assert is_valid is False
        assert "https" in reason.lower()

    def test_no_s3_domain(self):
        is_valid, reason = validate_template_s3_url("https://example.com/template.yaml")
        assert is_valid is False

    def test_url_too_long(self):
        url = "https://bucket.s3.amazonaws.com/" + "a" * 1024
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is False
        assert "long" in reason.lower()

    def test_valid_url_at_max_length(self):
        url = "https://bucket.s3.amazonaws.com/" + "a" * (1024 - 33)
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True

    def test_valid_url_with_query_params(self):
        url = "https://bucket.s3.us-east-1.amazonaws.com/template.yaml?versionId=abc"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True

