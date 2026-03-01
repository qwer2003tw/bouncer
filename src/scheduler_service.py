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
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

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

    def create_expiry_schedule(self, request_id: str, expires_at: int) -> bool:
        """Create a one-time EventBridge Scheduler schedule that fires at
        *expires_at* (Unix timestamp) and invokes the cleanup Lambda with the
        *request_id* payload.

        Returns:
            ``True`` on success, ``False`` on any failure (non-raising).
        """
        if not self._enabled:
            logger.debug("[SCHEDULER] Disabled — skipping schedule creation for %s", request_id)
            return False

        if not self._lambda_arn or not self._role_arn:
            logger.warning(
                "[SCHEDULER] Missing LAMBDA_ARN or ROLE_ARN — cannot schedule expiry for %s",
                request_id,
            )
            return False

        try:
            client = self._get_client()
            name = schedule_name(request_id)
            at_expr = _format_schedule_time(expires_at)

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
                    "Input": json.dumps(
                        {
                            "source": "bouncer-scheduler",
                            "action": "cleanup_expired",
                            "request_id": request_id,
                        }
                    ),
                },
            )
            logger.info("[SCHEDULER] Created expiry schedule '%s' for request %s at %s", name, request_id, at_expr)
            return True

        except Exception as exc:
            # Scheduler creation is non-critical; log but don't propagate
            logger.error(
                "[SCHEDULER] Failed to create schedule for %s: %s", request_id, exc
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
            logger.info("[SCHEDULER] Deleted schedule '%s' for request %s", name, request_id)
            return True
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            # Already deleted or never created — treat as success
            return True
        except Exception as exc:
            logger.error("[SCHEDULER] Failed to delete schedule for %s: %s", request_id, exc)
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
