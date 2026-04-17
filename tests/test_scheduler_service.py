"""
Tests for src/scheduler_service.py - EventBridge Scheduler operations.

Tests schedule name generation, schedule creation/deletion, and error handling.
"""
import os
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Ensure src is on sys.path

import scheduler_service

pytestmark = pytest.mark.xdist_group("scheduler_service")


class TestScheduleNaming:
    """Test schedule name generation functions."""

    def test_schedule_name_basic(self):
        """schedule_name() generates bouncer-expire-{id} format."""
        result = scheduler_service.schedule_name("req-abc123")
        assert result == "bouncer-expire-req-abc123"

    def test_schedule_name_sanitizes_special_chars(self):
        """schedule_name() replaces invalid characters with dash."""
        result = scheduler_service.schedule_name("req@special#chars!")
        assert result == "bouncer-expire-req-special-chars-"
        assert all(c.isalnum() or c in ('-', '_') for c in result)

    def test_schedule_name_truncates_to_64_chars(self):
        """schedule_name() truncates long IDs to EventBridge 64-char limit."""
        long_id = "x" * 100
        result = scheduler_service.schedule_name(long_id)
        assert len(result) == 64
        assert result.startswith("bouncer-expire-")

    def test_warning_schedule_name_prefix(self):
        """warning_schedule_name() uses bouncer-warn- prefix."""
        result = scheduler_service.warning_schedule_name("req-123")
        assert result == "bouncer-warn-req-123"

    def test_reminder_schedule_name_prefix(self):
        """reminder_schedule_name() uses bouncer-remind- prefix."""
        result = scheduler_service.reminder_schedule_name("req-456")
        assert result == "bouncer-remind-req-456"

    def test_escalation_schedule_name_prefix(self):
        """escalation_schedule_name() uses bouncer-escalation- prefix."""
        result = scheduler_service.escalation_schedule_name("req-789")
        assert result == "bouncer-escalation-req-789"


class TestFormatScheduleTime:
    """Test _format_schedule_time timestamp formatting."""

    def test_format_schedule_time_utc(self):
        """_format_schedule_time() converts timestamp to at(YYYY-MM-DDTHH:MM:SS)."""
        # 2024-01-01 00:00:00 UTC
        timestamp = 1704067200
        result = scheduler_service._format_schedule_time(timestamp)
        assert result == "at(2024-01-01T00:00:00)"

    def test_format_schedule_time_with_minutes_seconds(self):
        """_format_schedule_time() preserves hours, minutes, seconds."""
        # 2024-06-15 14:30:45 UTC
        dt = datetime(2024, 6, 15, 14, 30, 45, tzinfo=timezone.utc)
        timestamp = int(dt.timestamp())
        result = scheduler_service._format_schedule_time(timestamp)
        assert result == "at(2024-06-15T14:30:45)"


class TestSchedulerServiceCreateSchedule:
    """Test SchedulerService.create_expiry_schedule() non-raising behavior."""

    @patch.dict(os.environ, {
        'SCHEDULER_ENABLED': 'true',
        'SCHEDULER_ROLE_ARN': 'arn:aws:iam::123456789012:role/SchedulerRole',
        'AWS_LAMBDA_FUNCTION_ARN': 'arn:aws:lambda:us-east-1:123456789012:function:cleanup',
        'SCHEDULER_GROUP_NAME': 'test-group'
    })
    def test_create_expiry_schedule_success(self):
        """create_expiry_schedule() calls create_schedule with correct params."""
        mock_scheduler = MagicMock()
        mock_scheduler.create_schedule.return_value = {}

        # Pass role_arn and lambda_arn explicitly to override env vars
        svc = scheduler_service.SchedulerService(
            scheduler_client=mock_scheduler,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:cleanup',
            role_arn='arn:aws:iam::123456789012:role/SchedulerRole',
            group_name='test-group'
        )
        result = svc.create_expiry_schedule(
            request_id="req-test",
            expires_at=1704067200
        )

        assert result is True
        mock_scheduler.create_schedule.assert_called_once()
        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        assert call_kwargs['Name'] == 'bouncer-expire-req-test'
        assert call_kwargs['GroupName'] == 'test-group'
        assert 'at(2024-01-01T00:00:00)' in call_kwargs['ScheduleExpression']

    @patch.dict(os.environ, {'SCHEDULER_ENABLED': 'false'})
    def test_create_expiry_schedule_disabled(self):
        """create_expiry_schedule() returns False when SCHEDULER_ENABLED=false."""
        mock_scheduler = MagicMock()
        svc = scheduler_service.SchedulerService(scheduler_client=mock_scheduler)

        result = svc.create_expiry_schedule("req-test", 1704067200)

        assert result is False
        mock_scheduler.create_schedule.assert_not_called()

    @patch.dict(os.environ, {'SCHEDULER_ENABLED': 'true', 'SCHEDULER_ROLE_ARN': 'arn:aws:iam::123456789012:role/Test'})
    def test_create_expiry_schedule_handles_conflict_error(self):
        """create_expiry_schedule() logs ConflictException but returns False (error logged)."""
        from botocore.exceptions import ClientError
        mock_scheduler = MagicMock()
        mock_scheduler.create_schedule.side_effect = ClientError(
            {'Error': {'Code': 'ConflictException', 'Message': 'Already exists'}},
            'CreateSchedule'
        )

        svc = scheduler_service.SchedulerService(
            scheduler_client=mock_scheduler,
            lambda_arn='arn:aws:lambda:us-east-1:123456789012:function:cleanup',
            role_arn='arn:aws:iam::123456789012:role/Test'
        )
        result = svc.create_expiry_schedule("req-test", 1704067200)

        # ConflictException is logged and returns False (non-raising)
        assert result is False

    @patch.dict(os.environ, {'SCHEDULER_ENABLED': 'true', 'SCHEDULER_ROLE_ARN': 'arn:aws:iam::123456789012:role/Test'})
    def test_create_expiry_schedule_handles_other_errors(self):
        """create_expiry_schedule() logs other errors and returns False (non-raising)."""
        from botocore.exceptions import ClientError
        mock_scheduler = MagicMock()
        mock_scheduler.create_schedule.side_effect = ClientError(
            {'Error': {'Code': 'ValidationException', 'Message': 'Invalid param'}},
            'CreateSchedule'
        )

        svc = scheduler_service.SchedulerService(scheduler_client=mock_scheduler)
        result = svc.create_expiry_schedule("req-test", 1704067200)

        assert result is False


class TestSchedulerServiceDeleteSchedule:
    """Test SchedulerService.delete_schedule() non-raising behavior."""

    @patch.dict(os.environ, {
        'SCHEDULER_ENABLED': 'true',
        'SCHEDULER_GROUP_NAME': 'test-group'
    })
    def test_delete_schedule_success(self):
        """delete_schedule() calls delete_schedule API."""
        mock_scheduler = MagicMock()
        mock_scheduler.delete_schedule.return_value = {}

        svc = scheduler_service.SchedulerService(
            scheduler_client=mock_scheduler,
            group_name='test-group',
            enabled=True
        )
        result = svc.delete_schedule("req-abc")

        assert result is True
        mock_scheduler.delete_schedule.assert_called_once_with(
            Name='bouncer-expire-req-abc',
            GroupName='test-group'
        )

    @patch.dict(os.environ, {'SCHEDULER_ENABLED': 'true'})
    def test_delete_schedule_not_found_swallowed(self):
        """delete_schedule() swallows ResourceNotFoundException (idempotent)."""
        mock_scheduler = MagicMock()
        # Mock the exceptions attribute to simulate client.exceptions.ResourceNotFoundException
        resource_not_found = type('ResourceNotFoundException', (Exception,), {})
        mock_scheduler.exceptions.ResourceNotFoundException = resource_not_found
        mock_scheduler.delete_schedule.side_effect = resource_not_found()

        svc = scheduler_service.SchedulerService(scheduler_client=mock_scheduler, enabled=True)
        result = svc.delete_schedule("req-missing")

        assert result is True  # idempotent

    @patch.dict(os.environ, {'SCHEDULER_ENABLED': 'false'})
    def test_delete_schedule_disabled(self):
        """delete_schedule() returns False when disabled."""
        mock_scheduler = MagicMock()
        svc = scheduler_service.SchedulerService(scheduler_client=mock_scheduler, enabled=False)

        result = svc.delete_schedule("req-test")

        assert result is False
        mock_scheduler.delete_schedule.assert_not_called()
