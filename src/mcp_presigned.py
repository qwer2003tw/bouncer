"""
Bouncer - Presigned Upload Pipeline

PresignedContext + pipeline step functions + mcp_tool_request_presigned().
Follows the same dataclass pipeline style as mcp_upload.py (UploadContext).
"""

import json
import re
import time
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

from constants import DEFAULT_ACCOUNT_ID
from db import table
from rate_limit import PendingLimitExceeded, RateLimitExceeded, check_rate_limit
from utils import generate_request_id, mcp_result


# =============================================================================
# Presigned Upload Pipeline — Context + Step Functions
# =============================================================================

_MIN_EXPIRES_IN = 60
_DEFAULT_EXPIRES_IN = 900
_MAX_EXPIRES_IN = 3600


@dataclass
class PresignedContext:
    """Pipeline context for mcp_tool_request_presigned."""

    req_id: str
    filename: str
    content_type: str
    reason: str
    source: str
    account_id: str
    expires_in: int
    # Resolved in _resolve_presigned_target
    bucket: str = field(default="")
    s3_key: str = field(default="")
    request_id: str = field(default="")


def _sanitize_filename(filename: str) -> str:
    """消毒檔名，移除危險字元（防 path traversal）。

    Mirrors the logic in mcp_upload._sanitize_filename but preserves
    sub-directory structure (e.g. ``assets/foo.js``) so the presigned
    key keeps its intended path.
    """
    # Remove null bytes
    filename = filename.replace("\x00", "")
    # Normalise separators
    filename = filename.replace("\\", "/")
    # Resolve path-traversal components segment by segment
    clean_parts = []
    for part in filename.split("/"):
        # Strip .. and leading dots/spaces from every segment
        part = part.replace("..", "")
        part = part.lstrip(". ")
        # Keep only safe characters per segment
        part = re.sub(r"[^\w\-.]", "_", part)
        if part:
            clean_parts.append(part)
    return "/".join(clean_parts) or "unnamed"


def _parse_presigned_request(
    req_id: str, arguments: dict
) -> "PresignedContext | dict":
    """Parse and validate request arguments.

    Returns a :class:`PresignedContext` on success, or an MCP error dict
    on validation failure.
    """
    filename = str(arguments.get("filename", "")).strip()
    content_type = str(arguments.get("content_type", "")).strip()
    reason = str(arguments.get("reason", "")).strip()
    source = str(arguments.get("source", "")).strip()
    account_id = str(arguments.get("account", DEFAULT_ACCOUNT_ID or "")).strip()

    try:
        expires_in = int(arguments.get("expires_in", _DEFAULT_EXPIRES_IN))
    except (TypeError, ValueError):
        return mcp_result(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": "expires_in must be an integer",
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )

    # Validate required fields
    for param, value in [
        ("filename", filename),
        ("content_type", content_type),
        ("reason", reason),
        ("source", source),
    ]:
        if not value:
            return mcp_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "status": "error",
                                    "error": f"{param} is required",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )

    # Validate expires_in
    if expires_in > _MAX_EXPIRES_IN:
        return mcp_result(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": (
                                    f"expires_in exceeds maximum allowed value "
                                    f"of {_MAX_EXPIRES_IN} seconds"
                                ),
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )

    if expires_in <= 0:
        return mcp_result(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": "expires_in must be a positive integer",
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )

    if expires_in < _MIN_EXPIRES_IN:
        return mcp_result(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": f"expires_in must be at least {_MIN_EXPIRES_IN} seconds",
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )

    return PresignedContext(
        req_id=req_id,
        filename=filename,
        content_type=content_type,
        reason=reason,
        source=source,
        account_id=account_id or DEFAULT_ACCOUNT_ID or "",
        expires_in=expires_in,
    )


def _resolve_presigned_target(ctx: PresignedContext) -> None:
    """Determine bucket, key, and request_id.  Mutates *ctx* in-place."""
    safe_filename = _sanitize_filename(ctx.filename)
    ctx.request_id = generate_request_id(f"presigned:{safe_filename}")
    date_str = time.strftime("%Y-%m-%d")
    ctx.bucket = f"bouncer-uploads-{ctx.account_id}"
    ctx.s3_key = f"{date_str}/{ctx.request_id}/{safe_filename}"


def _generate_presigned_url(ctx: PresignedContext) -> dict:
    """Generate a presigned PUT URL and write an audit record to DynamoDB.

    Returns an MCP result dict on success, or an MCP error dict on failure.
    """
    try:
        s3_client = boto3.client("s3")
        presigned_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": ctx.bucket,
                "Key": ctx.s3_key,
                "ContentType": ctx.content_type,
            },
            ExpiresIn=ctx.expires_in,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        return mcp_result(
            ctx.req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": f"S3 error [{code}]: {msg}",
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )
    except Exception as exc:
        return mcp_result(
            ctx.req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": f"Failed to generate presigned URL: {exc}",
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )

    now = int(time.time())
    expires_at_ts = now + ctx.expires_in
    expires_at_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_ts)
    )
    s3_uri = f"s3://{ctx.bucket}/{ctx.s3_key}"

    # Write DynamoDB audit record
    audit_item = {
        "request_id": ctx.request_id,
        "action": "presigned_upload",
        "status": "url_issued",
        "filename": ctx.filename,
        "s3_key": ctx.s3_key,
        "bucket": ctx.bucket,
        "content_type": ctx.content_type,
        "source": ctx.source,
        "reason": ctx.reason,
        "account_id": ctx.account_id,
        "expires_at": expires_at_ts,
        "created_at": now,
        "ttl": expires_at_ts + 60,  # small buffer after expiry
    }
    table.put_item(Item=audit_item)

    payload = {
        "status": "ready",
        "presigned_url": presigned_url,
        "s3_key": ctx.s3_key,
        "s3_uri": s3_uri,
        "request_id": ctx.request_id,
        "expires_at": expires_at_iso,
        "method": "PUT",
        "headers": {"Content-Type": ctx.content_type},
    }
    return mcp_result(
        ctx.req_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload),
                }
            ]
        },
    )


# =============================================================================
# Public MCP tool entry-point
# =============================================================================


def mcp_tool_request_presigned(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_presigned.

    Issues a presigned S3 PUT URL for the staging bucket.  No human
    approval is required — the file goes to the staging bucket only.
    """
    # Phase 1: parse & validate
    ctx = _parse_presigned_request(req_id, arguments)
    if not isinstance(ctx, PresignedContext):
        return ctx  # validation error dict

    # Phase 2: rate limit check
    if ctx.source:
        try:
            check_rate_limit(ctx.source)
        except (RateLimitExceeded, PendingLimitExceeded) as e:
            return mcp_result(
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps({"status": "error", "error": str(e)})}],
                    "isError": True,
                },
            )

    # Phase 3: resolve target (bucket / key / request_id)
    _resolve_presigned_target(ctx)

    # Phase 4: generate URL + write audit record
    return _generate_presigned_url(ctx)
