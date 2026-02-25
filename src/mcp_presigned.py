"""
Bouncer - Presigned URL Pipeline (Approach B)

mcp_tool_request_presigned() — generates S3 presigned PUT URLs for direct
client-side uploads without going through Lambda.

Approach B adds:
  - Rate limiting (same pattern as mcp_upload._check_upload_rate_limit)
  - Clearer error messages on S3 generate_presigned_url failures
  - Minimum expires_in validation (>= 60 seconds)
"""

import json
import re
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from constants import DEFAULT_ACCOUNT_ID, AUDIT_TTL_SHORT
from db import table
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit
from utils import generate_request_id, mcp_result

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRESIGNED_DEFAULT_EXPIRES_IN = 900   # 15 minutes
PRESIGNED_MIN_EXPIRES_IN = 60        # 1 minute
PRESIGNED_MAX_EXPIRES_IN = 3600      # 1 hour


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_filename_presigned(filename: str) -> str:
    """Sanitise a filename, preserving subdirectory separators.

    Unlike the batch-upload helper (which strips directories), presigned
    uploads need to preserve the full relative path (e.g.
    ``assets/pdf.worker.min.mjs``) so the S3 key matches what the caller
    expects.  We only strip dangerous sequences.
    """
    if not filename:
        return "unnamed"

    # Remove null bytes
    filename = filename.replace("\x00", "")

    # Normalise Windows separators
    filename = filename.replace("\\", "/")

    # Strip leading slashes (absolute path)
    filename = filename.lstrip("/")

    # Collapse path-traversal sequences: remove any ".." component
    parts = filename.split("/")
    safe_parts = [p for p in parts if p not in (".", "..") and p != ""]
    filename = "/".join(safe_parts)

    # Remove characters that are dangerous in S3 keys (keeping . - _ / alphanumeric)
    filename = re.sub(r"[^\w\-./]", "_", filename)

    return filename or "unnamed"


def _check_presigned_rate_limit(source: str | None, req_id: str) -> dict | None:
    """Rate limit check for presigned URL requests.

    Returns an MCP error result if rate-limited, otherwise None.
    """
    if not source:
        return None
    try:
        check_rate_limit(source)
    except RateLimitExceeded as exc:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": str(exc),
            })}],
            "isError": True,
        })
    except PendingLimitExceeded as exc:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": str(exc),
            })}],
            "isError": True,
        })
    return None


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------

def mcp_tool_request_presigned(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_presigned

    Generate an S3 presigned PUT URL so the client can upload large files
    directly to the staging bucket without going through Lambda.
    """

    # ------------------------------------------------------------------ #
    # 1. Parse & validate arguments                                        #
    # ------------------------------------------------------------------ #
    filename = str(arguments.get("filename", "")).strip()
    content_type = arguments.get("content_type", None)
    if content_type is not None:
        content_type = str(content_type).strip()
    reason = str(arguments.get("reason", "")).strip()
    source = arguments.get("source", None)
    if source is not None:
        source = str(source).strip() or None
    account = arguments.get("account", None)
    if account is not None:
        account = str(account).strip() or None
    expires_in = arguments.get("expires_in", PRESIGNED_DEFAULT_EXPIRES_IN)

    # Required fields
    missing = [f for f, v in [
        ("filename", filename),
        ("content_type", content_type),
        ("reason", reason),
        ("source", source),
    ] if not v]
    if missing:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": f"Missing required parameters: {', '.join(missing)}",
            })}],
            "isError": True,
        })

    # expires_in type coercion
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "expires_in must be an integer",
            })}],
            "isError": True,
        })

    # expires_in range validation (Approach B: also validate minimum)
    if expires_in < PRESIGNED_MIN_EXPIRES_IN:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": (
                    f"expires_in must be at least {PRESIGNED_MIN_EXPIRES_IN} seconds, "
                    f"got {expires_in}"
                ),
            })}],
            "isError": True,
        })

    if expires_in > PRESIGNED_MAX_EXPIRES_IN:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": (
                    f"expires_in must not exceed {PRESIGNED_MAX_EXPIRES_IN} seconds, "
                    f"got {expires_in}"
                ),
            })}],
            "isError": True,
        })

    # ------------------------------------------------------------------ #
    # 2. Filename sanitization (path-traversal guard)                      #
    # ------------------------------------------------------------------ #
    safe_filename = _sanitize_filename_presigned(filename)
    if not safe_filename or safe_filename == "unnamed":
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": f"Invalid filename after sanitization: '{filename}'",
            })}],
            "isError": True,
        })

    # ------------------------------------------------------------------ #
    # 3. Rate limit check (Approach B)                                     #
    # ------------------------------------------------------------------ #
    rate_err = _check_presigned_rate_limit(source, req_id)
    if rate_err is not None:
        return rate_err

    # ------------------------------------------------------------------ #
    # 4. Resolve bucket and S3 key                                         #
    # ------------------------------------------------------------------ #
    target_account_id = account or DEFAULT_ACCOUNT_ID
    bucket = f"bouncer-uploads-{target_account_id}"

    request_id = generate_request_id(f"presigned:{safe_filename}")
    date_str = time.strftime("%Y-%m-%d")
    s3_key = f"{date_str}/{request_id}/{safe_filename}"
    s3_uri = f"s3://{bucket}/{s3_key}"

    # ------------------------------------------------------------------ #
    # 5. Generate presigned URL (Approach B: detailed error messages)      #
    # ------------------------------------------------------------------ #
    now = int(time.time())
    expires_at_ts = now + expires_in
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        s3_client = boto3.client("s3")
        presigned_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        error_msg = exc.response.get("Error", {}).get("Message", str(exc))
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": (
                    f"S3 presigned URL generation failed "
                    f"[{error_code}]: {error_msg}"
                ),
            })}],
            "isError": True,
        })
    except Exception as exc:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": (
                    f"Unexpected error generating presigned URL: "
                    f"{type(exc).__name__}: {exc}"
                ),
            })}],
            "isError": True,
        })

    # ------------------------------------------------------------------ #
    # 6. Write DynamoDB audit record                                       #
    # ------------------------------------------------------------------ #
    audit_item = {
        "request_id": request_id,
        "action": "presigned_upload",
        "status": "url_issued",
        "filename": safe_filename,
        "s3_key": s3_key,
        "bucket": bucket,
        "content_type": content_type,
        "source": source or "__anonymous__",
        "reason": reason,
        "expires_at": expires_at_ts,
        "created_at": now,
        "ttl": now + AUDIT_TTL_SHORT,
    }
    try:
        table.put_item(Item=audit_item)
    except Exception as exc:
        # Audit failure is non-critical — log and continue
        print(f"[PRESIGNED AUDIT] DynamoDB write failed for {request_id}: {exc}")

    # ------------------------------------------------------------------ #
    # 7. Return result                                                     #
    # ------------------------------------------------------------------ #
    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps({
            "status": "ready",
            "presigned_url": presigned_url,
            "s3_key": s3_key,
            "s3_uri": s3_uri,
            "request_id": request_id,
            "expires_at": expires_at,
            "method": "PUT",
            "headers": {"Content-Type": content_type},
        })}],
    })
