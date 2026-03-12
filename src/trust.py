"""
Bouncer - Trust Session 模組
處理信任時段的建立、查詢、撤銷和自動批准判斷

信任匹配基於 trust_scope + account_id + bound_source：
- trust_scope 是呼叫端提供的穩定識別符（如 session key）
- bound_source 在建立時綁定，防止不同來源重用同一 trust session
- Legacy sessions（無 bound_source）向下相容，但會記錄警告
"""
import time
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

from botocore.exceptions import ClientError

import db as _db



from constants import (

    TRUST_SESSION_ENABLED, TRUST_SESSION_DURATION, TRUST_SESSION_MAX_COMMANDS,
    TRUST_EXCLUDED_SERVICES, TRUST_EXCLUDED_ACTIONS, TRUST_EXCLUDED_FLAGS,
    TRUST_UPLOAD_MAX_BYTES_PER_FILE,
    TRUST_UPLOAD_MAX_BYTES_TOTAL, TRUST_UPLOAD_BLOCKED_EXTENSIONS,
    TRUST_IP_BINDING_MODE,
)
from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")

__all__ = [
    'TrustSession',
    'get_trust_session',
    'create_trust_session',
    'revoke_trust_session',
    'increment_trust_command_count',
    'increment_trust_upload_count',
    'is_trust_excluded',
    'should_trust_approve',
    'should_trust_approve_upload',
    'track_command_executed',
]

# DynamoDB - via db.py (lazy init)
# Tests may inject directly: trust._table = moto_table
# Tests may reset: trust._table = None
_table = None


def _get_table():
    if _table is not None:
        return _table
    return _db.table


# ============================================================================
# TrustSession dataclass — typed wrapper over raw DynamoDB item

@dataclass
class TrustSession:
    """Typed representation of a trust session record.

    Wraps raw DynamoDB dict access with attribute-level type safety.
    Use ``TrustSession.from_item()`` to construct from a raw DynamoDB item.
    """
    request_id: str
    trust_scope: str
    account_id: str
    approved_by: str
    created_at: int
    expires_at: int
    command_count: int = 0
    max_uploads: int = 0
    upload_count: int = 0
    upload_bytes_total: int = 0
    source: str = ''
    bound_source: str = ''          # security-critical: bound at creation time
    creator_ip: str = ''            # IP of the approver at trust session creation time
    ttl: int = 0
    _raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> 'TrustSession':
        """Construct a TrustSession from a raw DynamoDB item dict."""
        return cls(
            request_id=str(item.get('request_id', '')),
            trust_scope=str(item.get('trust_scope', '')),
            account_id=str(item.get('account_id', '')),
            approved_by=str(item.get('approved_by', '')),
            created_at=int(item.get('created_at', 0)),
            expires_at=int(item.get('expires_at', 0)),
            command_count=int(item.get('command_count', 0)),
            max_uploads=int(item.get('max_uploads', 0)),
            upload_count=int(item.get('upload_count', 0)),
            upload_bytes_total=int(item.get('upload_bytes_total', 0)),
            source=str(item.get('source', '')),
            bound_source=str(item.get('bound_source', '')),
            creator_ip=str(item.get('creator_ip', '')),
            ttl=int(item.get('ttl', 0)),
            _raw=item,
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.expires_at - int(time.time()))

    @property
    def is_expired(self) -> bool:
        return self.remaining_seconds <= 0

    def as_dict(self) -> Dict[str, Any]:
        """Return the underlying raw item dict (for backward-compatible callers)."""
        return self._raw

    # ──── source binding ────────────────────────────────────────────────

    def matches_source(self, source: str) -> bool:
        """Return True if *source* is allowed to use this trust session.

        Logic:
          - Legacy session (empty bound_source) → always allow, emit warning.
          - Non-legacy → exact match required.
        """
        if not self.bound_source:
            # Legacy / migrated session — backward-compatible pass-through
            logger.warning(
                "Trust session %s has no bound_source (legacy). "
                "Allowing source=%r — consider re-creating the session.",
                self.request_id, source,
                extra={"src_module": "trust", "operation": "matches_source", "trust_id": self.request_id},
            )
            return True
        return self.bound_source == source


# ============================================================================
# Internal helpers
# ============================================================================

def _compute_trust_id(trust_scope: str, account_id: str) -> str:
    scope_hash = hashlib.sha256(trust_scope.encode()).hexdigest()[:16]
    return f"trust-{scope_hash}-{account_id}"


# ============================================================================
# Public API
# ============================================================================

def get_trust_session(
    trust_scope: str,
    account_id: str,
    source: str = '',
) -> Optional[Dict]:
    """Query for an active trust session, validating source binding.

    Args:
        trust_scope: Stable caller-provided identifier (session key etc.)
        account_id:  AWS account ID
        source:      Caller source string — must match ``bound_source`` stored at
                     creation time.  Empty/None source is allowed for legacy callers
                     (treated as unknown — session must itself be legacy).

    Returns:
        Raw trust session dict (for backward compatibility), or ``None`` when:
        - no session exists / expired / wrong type
        - source mismatch with bound_source
    """
    if not TRUST_SESSION_ENABLED or not trust_scope:
        return None

    trust_id = _compute_trust_id(trust_scope, account_id)
    now = int(time.time())

    try:
        response = _get_table().get_item(Key={'request_id': trust_id})
        item = response.get('Item')

        if not item:
            return None

        # Validate type
        if item.get('type') != 'trust_session':
            return None

        # Validate expiry
        if int(item.get('expires_at', 0)) <= now:
            return None

        # Validate source binding (core security fix)
        session = TrustSession.from_item(item)
        if not session.matches_source(source):
            logger.warning(
                "Trust session source mismatch: trust_id=%s "
                "bound_source=%r incoming_source=%r — blocked.",
                trust_id, session.bound_source, source,
                extra={"src_module": "trust", "operation": "get_active_trust_session", "trust_id": trust_id},
            )
            return None

        return item

    except ClientError as e:
        logger.error("Trust session check error: %s", e, extra={"src_module": "trust", "operation": "get_active_trust_session", "trust_id": trust_id, "error": str(e)})
        return None


def create_trust_session(
    trust_scope: str,
    account_id: str,
    approved_by: str,
    source: str = '',
    max_uploads: int = 0,
    creator_ip: str = '',
) -> str:
    """Create a trust session and store ``bound_source`` for future validation.

    Also schedules an EventBridge one-time trigger (via TrustExpiryNotifier) so
    that when the session expires, pending requests from the same source are
    notified.

    Args:
        trust_scope:  Stable caller-provided identifier
        account_id:   AWS account ID
        approved_by:  Approver ID (Telegram user ID etc.)
        source:       Caller source — bound to this session; future calls with a
                      different source will be rejected.
        max_uploads:  Maximum trusted uploads (0 = upload trust disabled)
        creator_ip:   IP address of the approver (best-effort, may be empty or
                      differ from caller IP due to NAT/Telegram routing).

    Returns:
        trust_id string
    """
    trust_id = _compute_trust_id(trust_scope, account_id)

    now = int(time.time())
    expires_at = now + TRUST_SESSION_DURATION

    # bound_source is the security-critical binding; source is display-only
    display_source = source or trust_scope  # GSI does not accept empty strings

    item = {
        'request_id': trust_id,
        'type': 'trust_session',
        'trust_scope': trust_scope,
        'source': display_source,
        'bound_source': source,          # ← NEW: security binding
        'creator_ip': creator_ip,        # ← record approver IP (best-effort)
        'account_id': account_id,
        'approved_by': approved_by,
        'created_at': now,
        'expires_at': expires_at,
        'command_count': 0,
        'max_uploads': max_uploads,
        'upload_count': 0,
        'upload_bytes_total': 0,
        'ttl': expires_at,
    }

    _get_table().put_item(Item=item)

    # Schedule expiry notification (best-effort, non-raising)
    try:
        from scheduler_service import get_trust_expiry_notifier
        get_trust_expiry_notifier().schedule(trust_id=trust_id, expires_at=expires_at)
    except ClientError as exc:  # pragma: no cover
        logger.error("Failed to schedule trust expiry notification for %s: %s", trust_id, exc, extra={"src_module": "trust", "operation": "create_trust_session", "trust_id": trust_id, "error": str(exc)})

    return trust_id


def revoke_trust_session(trust_id: str) -> bool:
    """Revoke (delete) a trust session and cancel its expiry schedule.

    Args:
        trust_id: Trust session ID

    Returns:
        True on success
    """
    try:
        _get_table().delete_item(Key={'request_id': trust_id})

        # Cancel the EventBridge expiry schedule (best-effort, non-raising)
        try:
            from scheduler_service import get_trust_expiry_notifier
            get_trust_expiry_notifier().cancel(trust_id=trust_id)
        except ClientError as exc:  # pragma: no cover
            logger.error("Failed to cancel trust expiry schedule for %s: %s", trust_id, exc, extra={"src_module": "trust", "operation": "revoke_trust_session", "trust_id": trust_id, "error": str(exc)})

        return True
    except ClientError as e:
        logger.error("Revoke trust session error: %s", e, extra={"src_module": "trust", "operation": "revoke_trust_session", "trust_id": trust_id, "error": str(e)})
        return False


def increment_trust_command_count(trust_id: str) -> int:
    """Atomically increment trust session command counter (SEC-007).

    Uses DynamoDB conditional update for concurrency safety:
    - Only increments when under limit and session is still active
    - ConditionalCheckFailedException → returns 0 (reject)

    Returns:
        New counter value, or 0 when condition is not met
    """
    now = int(time.time())
    try:
        response = _get_table().update_item(
            Key={'request_id': trust_id},
            UpdateExpression='SET command_count = if_not_exists(command_count, :zero) + :one',
            ConditionExpression='command_count < :max AND #status = :active AND expires_at > :now',
            ExpressionAttributeNames={
                '#status': 'type',  # 'type' = 'trust_session' is our status indicator
            },
            ExpressionAttributeValues={
                ':zero': 0,
                ':one': 1,
                ':max': TRUST_SESSION_MAX_COMMANDS,
                ':active': 'trust_session',
                ':now': now,
            },
            ReturnValues='UPDATED_NEW'
        )
        return int(response.get('Attributes', {}).get('command_count', 0))
    except _get_table().meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning("Trust command count conditional update failed for %s (limit or expired)", trust_id, extra={"src_module": "trust", "operation": "increment_trust_command_count", "trust_id": trust_id})
        return 0
    except ClientError as e:
        logger.error("Increment trust command count error: %s", e, extra={"src_module": "trust", "operation": "increment_trust_command_count", "trust_id": trust_id, "error": str(e)})
        return 0


def is_trust_excluded(command: str) -> bool:
    """Check whether a command is excluded from trust (high-risk).

    Args:
        command: AWS CLI command string

    Returns:
        True if the command is excluded (should not be auto-approved)
    """
    cmd_lower = command.lower()

    for service in TRUST_EXCLUDED_SERVICES:
        if f'aws {service} ' in cmd_lower or f'aws {service}\t' in cmd_lower:
            return True

    for action in TRUST_EXCLUDED_ACTIONS:
        if action in cmd_lower:
            return True

    for flag in TRUST_EXCLUDED_FLAGS:
        if flag in cmd_lower:
            return True

    return False


def should_trust_approve(
    command: str,
    trust_scope: str,
    account_id: str,
    source: str = '',
    caller_ip: str = '',
) -> tuple:
    """Check whether a command should be auto-approved via trust session.

    Args:
        command:     AWS CLI command
        trust_scope: Trust scope identifier
        account_id:  AWS account ID
        source:      Caller source — validated against ``bound_source``
        caller_ip:   IP of the current caller (best-effort, may be empty).
                     If both creator_ip and caller_ip are non-empty and differ,
                     a warning is logged and a metric is emitted — but the
                     session is NOT blocked (IP mismatch is informational only,
                     because Telegram callbacks and MCP calls have different IPs
                     by design).

    Returns:
        (should_approve: bool, trust_session: dict or None, reason: str)
    """
    if not TRUST_SESSION_ENABLED or not trust_scope:
        return False, None, "Trust session disabled or no trust_scope"

    session = get_trust_session(trust_scope, account_id, source=source)
    if not session:
        return False, None, "No active trust session"

    if session.get('command_count', 0) >= TRUST_SESSION_MAX_COMMANDS:
        return False, session, f"Trust session command limit reached ({TRUST_SESSION_MAX_COMMANDS})"

    if is_trust_excluded(command):
        return False, session, "Command excluded from trust"

    remaining = int(session.get('expires_at', 0)) - int(time.time())
    if remaining <= 0:
        return False, None, "Trust session expired"

    # IP binding check (configurable: strict/warn/disabled — best-effort defence in depth)
    creator_ip = session.get('creator_ip', '')
    if creator_ip and caller_ip and creator_ip != caller_ip:
        trust_id = session.get('request_id', trust_scope)

        if TRUST_IP_BINDING_MODE == 'disabled':
            # Skip IP check entirely
            pass
        elif TRUST_IP_BINDING_MODE == 'strict':
            # Block request on IP mismatch
            logger.warning(
                "Trust session %s IP mismatch (strict mode): BLOCKED - creator_ip=%r caller_ip=%r",
                trust_id, creator_ip, caller_ip,
                extra={"src_module": "trust", "operation": "should_trust_approve", "trust_id": trust_id, "mode": "strict"},
            )
            try:
                from metrics import emit_metric
                emit_metric('Bouncer', 'TrustIPBlocked', 1, dimensions={'Event': 'blocked', 'Mode': 'strict'})
            except Exception:  # noqa: BLE001 — best-effort metrics
                pass
            return False, session, f"IP mismatch blocked (strict mode): creator={creator_ip} caller={caller_ip}"
        else:
            # Default 'warn' mode: log warning + metric but allow the request
            logger.warning(
                "Trust session %s IP mismatch (warn mode): allowed - creator_ip=%r caller_ip=%r "
                "(note: Telegram callbacks and MCP calls intentionally use different IPs)",
                trust_id, creator_ip, caller_ip,
                extra={"src_module": "trust", "operation": "should_trust_approve", "trust_id": trust_id, "mode": "warn"},
            )
            try:
                from metrics import emit_metric
                emit_metric('Bouncer', 'TrustIPMismatch', 1, dimensions={'Event': 'mismatch', 'Mode': 'warn'})
            except Exception:  # noqa: BLE001 — best-effort metrics
                pass

    return True, session, f"Trust session active ({remaining}s remaining)"


# ============================================================================
# Command tracking (sprint9-007-phase-a)
# ============================================================================

def track_command_executed(trust_id: str, command: str, success: bool) -> None:
    """Append a command summary entry to the trust session's commands_executed list.

    Uses DynamoDB list_append with if_not_exists to initialise the list on first
    write.  This is fire-and-forget: errors are logged but never propagated.

    Args:
        trust_id: Trust session DDB primary key (request_id)
        command:  AWS CLI command string (truncated to 100 chars)
        success:  True if command succeeded, False if it failed
    """
    try:
        entry = {
            'cmd': command[:100],
            'ts': int(time.time()),
            'success': success,
        }
        _get_table().update_item(
            Key={'request_id': trust_id},
            UpdateExpression=(
                'SET commands_executed = '
                'list_append(if_not_exists(commands_executed, :empty), :cmd)'
            ),
            ExpressionAttributeValues={
                ':empty': [],
                ':cmd': [entry],
            },
        )
    except ClientError as exc:
        logger.error('track_command_executed failed for %s: %s', trust_id, exc, extra={"src_module": "trust", "operation": "track_command_executed", "trust_id": trust_id, "error": str(exc)})


# ============================================================================
# Upload helpers
# ============================================================================

def _is_upload_filename_safe(filename: str) -> bool:
    """Return True if filename is free of path-traversal / unsafe characters."""
    if not filename:
        return False
    if '\x00' in filename:
        return False
    if '..' in filename:
        return False
    if '/' in filename or '\\' in filename:
        return False
    return True


def _is_upload_extension_blocked(filename: str) -> bool:
    """Return True if the file extension is on the block-list."""
    lower = filename.lower()
    for ext in TRUST_UPLOAD_BLOCKED_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def should_trust_approve_upload(
    trust_scope: str,
    account_id: str,
    filename: str,
    content_size: int,
    source: str = '',
) -> tuple:
    """Check whether an upload should be auto-approved via trust session.

    Args:
        trust_scope:   Trust scope identifier
        account_id:    AWS account ID
        filename:      Upload filename
        content_size:  File size in bytes
        source:        Caller source — validated against ``bound_source``

    Returns:
        (should_approve: bool, trust_session: dict or None, reason: str)
    """
    if not TRUST_SESSION_ENABLED or not trust_scope:
        return False, None, "Trust session disabled or no trust_scope"

    if not _is_upload_filename_safe(filename):
        return False, None, "Filename contains unsafe characters"

    if _is_upload_extension_blocked(filename):
        return False, None, f"File extension blocked: {filename}"

    session = get_trust_session(trust_scope, account_id, source=source)
    if not session:
        return False, None, "No active trust session"

    remaining = int(session.get('expires_at', 0)) - int(time.time())
    if remaining <= 0:
        return False, None, "Trust session expired"

    max_uploads = int(session.get('max_uploads', 0))
    if max_uploads <= 0:
        return False, session, "Trust session upload not enabled"

    upload_count = int(session.get('upload_count', 0))
    if upload_count >= max_uploads:
        return False, session, f"Upload quota exhausted ({upload_count}/{max_uploads})"

    if content_size > TRUST_UPLOAD_MAX_BYTES_PER_FILE:
        return False, session, f"File too large: {content_size} > {TRUST_UPLOAD_MAX_BYTES_PER_FILE}"

    upload_bytes_total = int(session.get('upload_bytes_total', 0))
    if upload_bytes_total + content_size > TRUST_UPLOAD_MAX_BYTES_TOTAL:
        return False, session, "Total upload bytes would exceed limit"

    return True, session, f"Trust upload approved ({upload_count + 1}/{max_uploads})"


def increment_trust_upload_count(trust_id: str, content_size: int) -> bool:
    """Atomically increment trust session upload count and byte total.

    Uses DynamoDB conditional update for concurrency safety.

    Args:
        trust_id:     Trust session ID
        content_size: Bytes uploaded in this request

    Returns:
        True on success; False when condition is not met (quota full or expired)
    """
    now = int(time.time())
    try:
        _get_table().update_item(
            Key={'request_id': trust_id},
            UpdateExpression=(
                'SET upload_count = if_not_exists(upload_count, :zero) + :one, '
                'upload_bytes_total = if_not_exists(upload_bytes_total, :zero) + :size'
            ),
            ConditionExpression=(
                'upload_count < max_uploads '
                'AND expires_at > :now'
            ),
            ExpressionAttributeValues={
                ':zero': 0,
                ':one': 1,
                ':size': content_size,
                ':now': now,
            },
        )
        return True
    except _get_table().meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning("Trust upload conditional update failed for %s", trust_id, extra={"src_module": "trust", "operation": "increment_trust_upload_count", "trust_id": trust_id})
        return False
    except ClientError as e:
        logger.error("Increment trust upload count error: %s", e, extra={"src_module": "trust", "operation": "increment_trust_upload_count", "trust_id": trust_id, "error": str(e)})
        return False
