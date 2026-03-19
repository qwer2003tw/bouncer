"""SchedulerService — EventBridge Scheduler encapsulation.

Provides a clean, testable interface for creating and managing one-time
EventBridge Scheduler schedules that trigger the cleanup_expired Lambda
endpoint when a request's TTL is reached.

Design goals (Aggressive approach):
- Single-responsibility: all scheduler logic in one place
- Fail-safe: scheduler creation is non-critical (logged, not raised)
- Naming convention: ``bouncer-expire-{request_id}`` for easy debugging
- Idempotent: duplicate creation attempts are silently swallowed
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

# ─── env vars ────────────────────────────────────────────────────────────────

SCHEDULE_GROUP_NAME: str = os.environ.get(
    "SCHEDULER_GROUP_NAME", "bouncer-expiry-schedules"
)
SCHEDULER_ROLE_ARN: str = os.environ.get("SCHEDULER_ROLE_ARN", "")
LAMBDA_FUNCTION_ARN: str = os.environ.get("AWS_LAMBDA_FUNCTION_ARN", "")
SCHEDULER_ENABLED: bool = os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"

# ─── naming helpers ──────────────────────────────────────────────────────────


def schedule_name(request_id: str) -> str:
    """Return a deterministic, debuggable schedule name for *request_id*.

    EventBridge Scheduler names must match ``[a-zA-Z0-9_-]{1,64}``.
    We replace any disallowed characters with ``-`` and truncate to 64.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in request_id)
    return f"bouncer-expire-{safe}"[:64]


def warning_schedule_name(request_id: str) -> str:
    """Return the schedule name for expiry *warning* (60s before expiry).

    This is distinct from the cleanup schedule (``schedule_name``) so both
    can coexist and be cancelled independently when a request is approved/denied.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in request_id)
    return f"bouncer-warn-{safe}"[:64]


def reminder_schedule_name(request_id: str) -> str:
    """Return the schedule name for pending approval *reminder*.

    This is distinct from both cleanup and warning schedules, so all three
    can coexist and be cancelled independently when a request is approved/denied.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in request_id)
    return f"bouncer-remind-{safe}"[:64]


def escalation_schedule_name(request_id: str) -> str:
    """Return the schedule name for pending approval *escalation* (2nd reminder).

    This is distinct from the initial reminder schedule, so both can coexist
    and be cancelled independently when a request is approved/denied.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in request_id)
    return f"bouncer-escalation-{safe}"[:64]


def _format_schedule_time(expires_at_ts: int) -> str:
    """Convert a Unix timestamp to the ``at(...)`` expression required by
    EventBridge Scheduler.

    Format: ``at(YYYY-MM-DDTHH:MM:SS)`` in UTC.
    """
    dt = datetime.fromtimestamp(expires_at_ts, tz=timezone.utc)
    return f"at({dt.strftime('%Y-%m-%dT%H:%M:%S')})"


# ─── SchedulerService ─────────────────────────────────────────────────────────


class SchedulerService:
    """Encapsulates EventBridge Scheduler operations for Bouncer.

    Usage::

        svc = SchedulerService()
        svc.create_expiry_schedule(request_id="req-abc", expires_at=1700000000)
        svc.delete_schedule(request_id="req-abc")  # optional cleanup

    All public methods are *non-raising*: failures are logged at ERROR level
    and a falsy value is returned so callers can treat scheduling as best-effort.
    """

    def __init__(
        self,
        *,
        scheduler_client=None,
        lambda_arn: Optional[str] = None,
        role_arn: Optional[str] = None,
        group_name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        """
        Args:
            scheduler_client: Pre-built boto3 scheduler client (injected for testing).
            lambda_arn:  Override the Lambda target ARN (default: env var).
            role_arn:    Override the IAM role ARN (default: env var).
            group_name:  Override the schedule group name (default: env var).
            enabled:     Override the SCHEDULER_ENABLED flag (default: env var).
        """
        self._client = scheduler_client  # lazy-init if None
        self._lambda_arn = lambda_arn or LAMBDA_FUNCTION_ARN
        self._role_arn = role_arn or SCHEDULER_ROLE_ARN
        self._group_name = group_name or SCHEDULE_GROUP_NAME
        self._enabled = enabled if enabled is not None else SCHEDULER_ENABLED

    # ── public API ────────────────────────────────────────────────────────────

    def create_expiry_schedule(
        self,
        request_id: str,
        expires_at: int,
        *,
        telegram_message_id: Optional[int] = None,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Create a one-time EventBridge Scheduler schedule that fires at
        *expires_at* (Unix timestamp) and invokes the cleanup Lambda with the
        *request_id* payload.

        Args:
            request_id:          Bouncer request ID (DynamoDB primary key).
            expires_at:          Unix timestamp when the request expires.
            telegram_message_id: Optional Telegram message_id to embed in the
                                 schedule payload as a fallback for the cleanup
                                 handler when the DDB record is missing.
            chat_id:             Optional Telegram chat_id to accompany
                                 ``telegram_message_id`` in the fallback payload.

        Returns:
            ``True`` on success, ``False`` on any failure (non-raising).
        """
        if not self._enabled:
            logger.debug("Disabled — skipping schedule creation for %s", request_id, extra={"src_module": "scheduler", "operation": "create_expiry_schedule", "request_id": request_id})
            return False

        if not self._lambda_arn or not self._role_arn:
            logger.warning(
                "Missing LAMBDA_ARN or ROLE_ARN — cannot schedule expiry for %s",
                request_id,
                extra={"src_module": "scheduler", "operation": "create_expiry_schedule", "request_id": request_id},
            )
            return False

        try:
            client = self._get_client()
            name = schedule_name(request_id)
            at_expr = _format_schedule_time(expires_at)

            payload: dict = {
                "source": "bouncer-scheduler",
                "action": "cleanup_expired",
                "request_id": request_id,
            }
            if telegram_message_id is not None:
                payload["telegram_message_id"] = telegram_message_id
            if chat_id is not None:
                payload["chat_id"] = chat_id

            client.create_schedule(
                Name=name,
                GroupName=self._group_name,
                ScheduleExpression=at_expr,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": self._lambda_arn,
                    "RoleArn": self._role_arn,
                    "Input": json.dumps(payload),
                },
            )
            logger.info("Created expiry schedule '%s' for request %s at %s", name, request_id, at_expr, extra={"src_module": "scheduler", "operation": "create_expiry_schedule", "request_id": request_id, "schedule_name": name})
            return True

        except ClientError as exc:
            # Scheduler creation is non-critical; log but don't propagate
            logger.error(
                "Failed to create schedule for %s: %s", request_id, exc,
                extra={"src_module": "scheduler", "operation": "create_expiry_schedule", "request_id": request_id, "error": str(exc)},
            )
            return False

    def create_expiry_warning_schedule(
        self,
        request_id: str,
        expires_at: int,
        *,
        command_preview: str = '',
        source: str = '',
    ) -> bool:
        """Create a one-time EventBridge Scheduler schedule that fires 60 seconds
        *before* *expires_at* and sends a Telegram warning notification.

        This is distinct from the cleanup schedule (which fires *at* expires_at).
        Both schedules can coexist and are cancelled when the request is approved/denied.

        Args:
            request_id:      Bouncer request ID (DynamoDB primary key).
            expires_at:      Unix timestamp when the request expires.
            command_preview: First ~100 chars of the command for the notification.
            source:          Optional source string (e.g., 'ztp-files').

        Returns:
            ``True`` on success, ``False`` on any failure (non-raising).
        """
        if not self._enabled:
            logger.debug("Disabled — skipping warning schedule for %s", request_id, extra={"src_module": "scheduler", "operation": "create_expiry_warning_schedule", "request_id": request_id})
            return False

        if not self._lambda_arn or not self._role_arn:
            logger.warning(
                "Missing LAMBDA_ARN or ROLE_ARN — cannot schedule warning for %s",
                request_id,
                extra={"src_module": "scheduler", "operation": "create_expiry_warning_schedule", "request_id": request_id},
            )
            return False

        # Fire 60 seconds *before* expiry
        warning_time = expires_at - 60
        if warning_time <= int(time.time()):
            # Already past the warning time (e.g., TTL < 60s) — skip
            logger.debug("Warning time already past for %s — skipping", request_id, extra={"src_module": "scheduler", "operation": "create_expiry_warning_schedule", "request_id": request_id})
            return False

        try:
            client = self._get_client()
            name = warning_schedule_name(request_id)
            at_expr = _format_schedule_time(warning_time)

            payload = {
                "source": "bouncer-scheduler",
                "action": "expiry_warning",
                "request_id": request_id,
                "command_preview": command_preview,
                "source_field": source,  # renamed to avoid collision with top-level 'source'
            }

            client.create_schedule(
                Name=name,
                GroupName=self._group_name,
                ScheduleExpression=at_expr,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": self._lambda_arn,
                    "RoleArn": self._role_arn,
                    "Input": json.dumps(payload),
                },
            )
            logger.info("Created warning schedule '%s' for request %s at %s", name, request_id, at_expr, extra={"src_module": "scheduler", "operation": "create_expiry_warning_schedule", "request_id": request_id, "schedule_name": name})
            return True

        except ClientError as exc:
            logger.error(
                "Failed to create warning schedule for %s: %s", request_id, exc,
                extra={"src_module": "scheduler", "operation": "create_expiry_warning_schedule", "request_id": request_id, "error": str(exc)},
            )
            return False

    def create_pending_reminder_schedule(
        self,
        request_id: str,
        expires_at: int,
        *,
        reminder_minutes: int = 10,
        command_preview: str = '',
        source: str = '',
    ) -> bool:
        """Create a one-time EventBridge Scheduler schedule that fires
        *reminder_minutes* after request creation to remind if still pending.

        This is distinct from expiry_warning (which fires 60s *before* expiry).
        The reminder fires *reminder_minutes* after creation to nudge approval.

        Args:
            request_id:       Bouncer request ID (DynamoDB primary key).
            expires_at:       Unix timestamp when the request expires.
            reminder_minutes: Minutes after creation to send reminder (default: 10).
            command_preview:  First ~100 chars of the command for the notification.
            source:           Optional source string (e.g., 'ztp-files').

        Returns:
            ``True`` on success, ``False`` on any failure (non-raising).
        """
        if not self._enabled:
            logger.debug("Disabled — skipping reminder schedule for %s", request_id, extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id})
            return False

        if not self._lambda_arn or not self._role_arn:
            logger.warning(
                "Missing LAMBDA_ARN or ROLE_ARN — cannot schedule reminder for %s",
                request_id,
                extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id},
            )
            return False

        now = int(time.time())
        reminder_time = now + (reminder_minutes * 60)

        # Skip if reminder would fire after or at expiry time
        if reminder_time >= expires_at:
            logger.debug(
                "Reminder time >= expiry time for %s — skipping (reminder would be pointless)",
                request_id,
                extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id},
            )
            return False

        try:
            client = self._get_client()
            name = reminder_schedule_name(request_id)
            at_expr = _format_schedule_time(reminder_time)

            payload = {
                "source": "bouncer-scheduler",
                "action": "pending_reminder",
                "request_id": request_id,
                "command_preview": command_preview,
                "source_field": source,
            }

            client.create_schedule(
                Name=name,
                GroupName=self._group_name,
                ScheduleExpression=at_expr,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": self._lambda_arn,
                    "RoleArn": self._role_arn,
                    "Input": json.dumps(payload),
                },
            )
            logger.info("Created reminder schedule '%s' for request %s at %s", name, request_id, at_expr, extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id, "schedule_name": name})

            # Create escalation schedule (2nd reminder at reminder_minutes * 3)
            escalation_minutes = reminder_minutes * 3
            escalation_time = now + (escalation_minutes * 60)

            # Only create escalation if it would fire before expiry
            if escalation_time < expires_at:
                escalation_name = escalation_schedule_name(request_id)
                escalation_at_expr = _format_schedule_time(escalation_time)
                escalation_payload = {
                    "source": "bouncer-scheduler",
                    "action": "pending_reminder",
                    "request_id": request_id,
                    "command_preview": command_preview,
                    "source_field": source,
                    "escalation": True,  # Mark as escalation (2nd reminder)
                }

                try:
                    client.create_schedule(
                        Name=escalation_name,
                        GroupName=self._group_name,
                        ScheduleExpression=escalation_at_expr,
                        ScheduleExpressionTimezone="UTC",
                        FlexibleTimeWindow={"Mode": "OFF"},
                        ActionAfterCompletion="DELETE",
                        Target={
                            "Arn": self._lambda_arn,
                            "RoleArn": self._role_arn,
                            "Input": json.dumps(escalation_payload),
                        },
                    )
                    logger.info("Created escalation schedule '%s' for request %s at %s", escalation_name, request_id, escalation_at_expr, extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id, "schedule_name": escalation_name})
                except ClientError as exc:
                    logger.warning(
                        "Failed to create escalation schedule for %s: %s (first reminder created successfully)",
                        request_id, exc,
                        extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id, "error": str(exc)},
                    )
                    # Don't fail overall operation if escalation fails
            else:
                logger.debug(
                    "Escalation time >= expiry time for %s — skipping escalation (would be pointless)",
                    request_id,
                    extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id},
                )

            return True

        except ClientError as exc:
            logger.error(
                "Failed to create reminder schedule for %s: %s", request_id, exc,
                extra={"src_module": "scheduler", "operation": "create_pending_reminder_schedule", "request_id": request_id, "error": str(exc)},
            )
            return False

    def delete_schedule(self, request_id: str) -> bool:
        """Delete the expiry schedule for *request_id* (e.g. after approval).

        Returns:
            ``True`` on success or if schedule did not exist, ``False`` on error.
        """
        if not self._enabled:
            return False

        try:
            client = self._get_client()
            name = schedule_name(request_id)
            client.delete_schedule(Name=name, GroupName=self._group_name)
            logger.info("Deleted schedule '%s' for request %s", name, request_id, extra={"src_module": "scheduler", "operation": "delete_schedule", "request_id": request_id, "schedule_name": name})
            return True
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            # Already deleted or never created — treat as success
            return True
        except ClientError as exc:
            logger.error("Failed to delete schedule for %s: %s", request_id, exc, extra={"src_module": "scheduler", "operation": "delete_schedule", "request_id": request_id, "error": str(exc)})
            return False

    def delete_warning_schedule(self, request_id: str) -> bool:
        """Delete the expiry *warning* schedule for *request_id*.

        Returns:
            ``True`` on success or if schedule did not exist, ``False`` on error.
        """
        if not self._enabled:
            return False

        try:
            client = self._get_client()
            name = warning_schedule_name(request_id)
            client.delete_schedule(Name=name, GroupName=self._group_name)
            logger.info("Deleted warning schedule '%s' for request %s", name, request_id, extra={"src_module": "scheduler", "operation": "delete_warning_schedule", "request_id": request_id, "schedule_name": name})
            return True
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            return True
        except ClientError as exc:
            logger.error("Failed to delete warning schedule for %s: %s", request_id, exc, extra={"src_module": "scheduler", "operation": "delete_warning_schedule", "request_id": request_id, "error": str(exc)})
            return False

    def delete_reminder_schedule(self, request_id: str) -> bool:
        """Delete the pending approval *reminder* schedule for *request_id*.

        Returns:
            ``True`` on success or if schedule did not exist, ``False`` on error.
        """
        if not self._enabled:
            return False

        try:
            client = self._get_client()
            name = reminder_schedule_name(request_id)
            client.delete_schedule(Name=name, GroupName=self._group_name)
            logger.info("Deleted reminder schedule '%s' for request %s", name, request_id, extra={"src_module": "scheduler", "operation": "delete_reminder_schedule", "request_id": request_id, "schedule_name": name})
            return True
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            return True
        except ClientError as exc:
            logger.error("Failed to delete reminder schedule for %s: %s", request_id, exc, extra={"src_module": "scheduler", "operation": "delete_reminder_schedule", "request_id": request_id, "error": str(exc)})
            return False

    def delete_escalation_schedule(self, request_id: str) -> bool:
        """Delete the pending approval *escalation* schedule (2nd reminder) for *request_id*.

        Returns:
            ``True`` on success or if schedule did not exist, ``False`` on error.
        """
        if not self._enabled:
            return False

        try:
            client = self._get_client()
            name = escalation_schedule_name(request_id)
            client.delete_schedule(Name=name, GroupName=self._group_name)
            logger.info("Deleted escalation schedule '%s' for request %s", name, request_id, extra={"src_module": "scheduler", "operation": "delete_escalation_schedule", "request_id": request_id, "schedule_name": name})
            return True
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            return True
        except ClientError as exc:
            logger.error("Failed to delete escalation schedule for %s: %s", request_id, exc, extra={"src_module": "scheduler", "operation": "delete_escalation_schedule", "request_id": request_id, "error": str(exc)})
            return False

    # ── private helpers ───────────────────────────────────────────────────────

    def _get_client(self):
        """Return the boto3 scheduler client, lazy-initialising if needed."""
        if self._client is None:
            import boto3

            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            self._client = boto3.client("scheduler", region_name=region)
        return self._client


# ─── module-level singleton ───────────────────────────────────────────────────

_default_service: Optional[SchedulerService] = None


def get_scheduler_service() -> SchedulerService:
    """Return the module-level singleton SchedulerService.

    Callers may replace this with ``set_scheduler_service()`` in tests.
    """
    global _default_service
    if _default_service is None:
        _default_service = SchedulerService()
    return _default_service


def set_scheduler_service(svc: SchedulerService) -> None:
    """Override the module-level singleton (for testing)."""
    global _default_service
    _default_service = svc


# ─── TrustExpiryNotifier ──────────────────────────────────────────────────────


def trust_expiry_schedule_name(trust_id: str) -> str:
    """Return a deterministic schedule name for a trust session expiry.

    Prefixed with ``bouncer-trust-`` to distinguish from request expiry
    schedules (``bouncer-expire-``).

    EventBridge Scheduler names must match ``[a-zA-Z0-9_-]{1,64}``.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in trust_id)
    return f"bouncer-trust-{safe}"[:64]


class TrustExpiryNotifier:
    """Manages EventBridge Scheduler schedules for trust session expiry notification.

    Responsibilities (single class, clean design):
    - ``schedule()``: Create a one-time EventBridge schedule that fires when a
      trust session expires, triggering the ``trust_expiry`` handler.
    - ``cancel()``: Delete the schedule when a trust session is revoked early.

    Design principles:
    - All operations are non-raising (failures logged at ERROR, falsy returned).
    - Naming: ``bouncer-trust-{safe_trust_id}`` distinguishes from request
      expiry schedules (``bouncer-expire-``).
    - Injected ``scheduler_service`` for full testability without AWS calls.
    """

    def __init__(self, scheduler_service: Optional[SchedulerService] = None):
        """
        Args:
            scheduler_service: Inject a SchedulerService (or mock) for testing.
                               Defaults to the module-level singleton.
        """
        self._svc = scheduler_service  # resolved lazily to avoid import-time side-effects

    def _get_svc(self) -> SchedulerService:
        if self._svc is None:
            self._svc = get_scheduler_service()
        return self._svc

    def schedule(self, trust_id: str, expires_at: int) -> bool:
        """Schedule an EventBridge one-time trigger for *trust_id* at *expires_at*.

        The trigger fires the Lambda with ``action=trust_expiry`` so the handler
        can query pending requests and send a Telegram notification.

        Args:
            trust_id:   Trust session ID (DynamoDB primary key).
            expires_at: Unix timestamp when the trust session expires.

        Returns:
            ``True`` on success, ``False`` on any failure (non-raising).
        """
        svc = self._get_svc()
        if not svc._enabled:
            logger.debug(
                "Scheduler disabled — skipping trust expiry schedule for %s",
                trust_id,
                extra={"src_module": "scheduler", "operation": "trust_expiry_schedule", "trust_id": trust_id},
            )
            return False

        if not svc._lambda_arn or not svc._role_arn:
            logger.warning(
                "Missing LAMBDA_ARN or ROLE_ARN — cannot schedule trust expiry for %s",
                trust_id,
                extra={"src_module": "scheduler", "operation": "trust_expiry_schedule", "trust_id": trust_id},
            )
            return False

        try:
            client = svc._get_client()
            name = trust_expiry_schedule_name(trust_id)
            at_expr = _format_schedule_time(expires_at)

            client.create_schedule(
                Name=name,
                GroupName=svc._group_name,
                ScheduleExpression=at_expr,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": svc._lambda_arn,
                    "RoleArn": svc._role_arn,
                    "Input": json.dumps(
                        {
                            "source": "bouncer-scheduler",
                            "action": "trust_expiry",
                            "trust_id": trust_id,
                        }
                    ),
                },
            )
            logger.info(
                "Created expiry schedule '%s' for trust %s at %s",
                name, trust_id, at_expr,
                extra={"src_module": "scheduler", "operation": "trust_expiry_schedule", "trust_id": trust_id, "schedule_name": name},
            )
            return True

        except ClientError as exc:
            logger.error(
                "Failed to create expiry schedule for trust %s: %s",
                trust_id, exc,
                extra={"src_module": "scheduler", "operation": "trust_expiry_schedule", "trust_id": trust_id, "error": str(exc)},
            )
            return False

    def cancel(self, trust_id: str) -> bool:
        """Delete the expiry schedule for *trust_id* (called on revoke).

        Returns:
            ``True`` on success or if schedule did not exist, ``False`` on error.
        """
        svc = self._get_svc()
        if not svc._enabled:
            return False

        name = trust_expiry_schedule_name(trust_id)
        try:
            client = svc._get_client()
            client.delete_schedule(Name=name, GroupName=svc._group_name)
            logger.info(
                "Cancelled trust expiry schedule '%s' for trust %s",
                name, trust_id,
                extra={"src_module": "scheduler", "operation": "cancel_trust_schedule", "trust_id": trust_id, "schedule_name": name},
            )
            return True
        except ClientError as exc:
            # ResourceNotFoundException → already fired or never created → OK
            exc_name = type(exc).__name__
            exc_str = str(exc)
            if "ResourceNotFound" in exc_name or "NotFound" in exc_name \
                    or "ResourceNotFound" in exc_str:
                logger.debug(
                    "Schedule '%s' not found (already fired or never created)",
                    name,
                    extra={"src_module": "scheduler", "operation": "cancel_trust_schedule", "trust_id": trust_id},
                )
                return True
            logger.error(
                "Failed to cancel trust expiry schedule for trust %s: %s",
                trust_id, exc,
                extra={"src_module": "scheduler", "operation": "cancel_trust_schedule", "trust_id": trust_id, "error": str(exc)},
            )
            return False


# ─── module-level TrustExpiryNotifier singleton ───────────────────────────────

_default_notifier: Optional[TrustExpiryNotifier] = None


def get_trust_expiry_notifier() -> TrustExpiryNotifier:
    """Return the module-level singleton TrustExpiryNotifier."""
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = TrustExpiryNotifier()
    return _default_notifier


def set_trust_expiry_notifier(notifier: TrustExpiryNotifier) -> None:
    """Override the module-level singleton (for testing)."""
    global _default_notifier
    _default_notifier = notifier
