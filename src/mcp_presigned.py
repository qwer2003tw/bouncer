"""
Bouncer - Presigned Upload Pipeline

PresignedContext + pipeline step functions + mcp_tool_request_presigned().
Follows the same dataclass pipeline style as mcp_upload.py (UploadContext).
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import List

import boto3
from botocore.exceptions import ClientError

from constants import DEFAULT_ACCOUNT_ID
from db import table
from notifications import send_presigned_notification, send_presigned_batch_notification
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


def _generate_presigned_url_for_file(
    bucket: str, s3_key: str, content_type: str, expires_in: int
) -> "tuple[str | None, str | None]":
    """Generate a single presigned PUT URL via boto3.

    Returns ``(url, None)`` on success, ``(None, error_message)`` on failure.
    Shared by both single-file and batch pipelines.
    """
    try:
        s3_client = boto3.client("s3")
        url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
        return url, None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        return None, f"S3 error [{code}]: {msg}"
    except Exception as exc:
        return None, f"Failed to generate presigned URL: {exc}"


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
    Uses the shared _generate_presigned_url_for_file helper.
    """
    presigned_url, error = _generate_presigned_url_for_file(
        ctx.bucket, ctx.s3_key, ctx.content_type, ctx.expires_in
    )
    if error:
        return mcp_result(
            ctx.req_id,
            {
                "content": [{"type": "text", "text": json.dumps({"status": "error", "error": error})}],
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

    # Notify (silent) — must not include the presigned URL itself
    try:
        send_presigned_notification(
            filename=ctx.filename,
            source=ctx.source,
            account_id=ctx.account_id,
            expires_at=expires_at_iso,
        )
    except Exception as _notify_exc:
        print(f"[PRESIGNED] notification error (non-fatal): {_notify_exc}")

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


# =============================================================================
# Presigned Batch Pipeline — Context + Step Functions
# =============================================================================

_BATCH_MAX_FILES = 50


@dataclass
class PresignedBatchContext:
    """Pipeline context for mcp_tool_request_presigned_batch."""

    req_id: str
    files: List[dict]          # validated [{filename, content_type}]
    reason: str
    source: str
    account_id: str
    expires_in: int
    # Resolved in _resolve_batch_target
    batch_id: str = field(default="")
    bucket: str = field(default="")
    date_str: str = field(default="")


def _parse_presigned_batch_request(
    req_id: str, arguments: dict
) -> "PresignedBatchContext | dict":
    """Parse and validate batch request arguments."""

    def _error(msg: str) -> dict:
        return mcp_result(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"status": "error", "error": msg}),
                    }
                ],
                "isError": True,
            },
        )

    # --- required string fields ---
    reason = str(arguments.get("reason", "")).strip()
    source = str(arguments.get("source", "")).strip() or "__anonymous__"
    account_id = str(
        arguments.get("account", DEFAULT_ACCOUNT_ID or "")
    ).strip() or (DEFAULT_ACCOUNT_ID or "")

    if not reason:
        return _error("reason is required")

    # --- expires_in ---
    try:
        expires_in = int(arguments.get("expires_in", _DEFAULT_EXPIRES_IN))
    except (TypeError, ValueError):
        return _error("expires_in must be an integer")

    if expires_in <= 0:
        return _error("expires_in must be a positive integer")
    if expires_in < _MIN_EXPIRES_IN:
        return _error(
            f"expires_in must be at least {_MIN_EXPIRES_IN} seconds"
        )
    if expires_in > _MAX_EXPIRES_IN:
        return _error(
            f"expires_in exceeds maximum allowed value of {_MAX_EXPIRES_IN} seconds"
        )

    # --- files array ---
    files_raw = arguments.get("files")
    if not isinstance(files_raw, list) or len(files_raw) == 0:
        return _error("files must be a non-empty array")

    if len(files_raw) > _BATCH_MAX_FILES:
        return _error(
            f"files exceeds maximum of {_BATCH_MAX_FILES} items"
        )

    validated_files: List[dict] = []
    for i, entry in enumerate(files_raw):
        if not isinstance(entry, dict):
            return _error(f"files[{i}] must be an object")

        fn = str(entry.get("filename", "")).strip()
        ct = str(entry.get("content_type", "")).strip()

        if not fn:
            return _error(f"files[{i}].filename is required")
        if not ct:
            return _error(f"files[{i}].content_type is required")

        validated_files.append({"filename": fn, "content_type": ct})

    return PresignedBatchContext(
        req_id=req_id,
        files=validated_files,
        reason=reason,
        source=source,
        account_id=account_id,
        expires_in=expires_in,
    )


def _resolve_batch_target(ctx: PresignedBatchContext) -> None:
    """Assign batch_id, bucket, date_str.  Mutates *ctx* in-place."""
    ctx.batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    ctx.date_str = time.strftime("%Y-%m-%d")
    ctx.bucket = f"bouncer-uploads-{ctx.account_id}"


def _generate_presigned_batch_urls(ctx: PresignedBatchContext) -> dict:
    """Generate presigned URLs for all files and write one audit record."""

    def _error(msg: str) -> dict:
        return mcp_result(
            ctx.req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"status": "error", "error": msg}),
                    }
                ],
                "isError": True,
            },
        )

    s3_client = boto3.client("s3")
    now = int(time.time())
    expires_at_ts = now + ctx.expires_in
    expires_at_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_ts)
    )

    file_results = []
    seen_filenames: set = set()

    for entry in ctx.files:
        raw_filename = entry["filename"]
        content_type = entry["content_type"]

        # Deduplicate: append index suffix if filename already seen
        safe_fn = _sanitize_filename(raw_filename)
        original_safe = safe_fn
        suffix_idx = 1
        while safe_fn in seen_filenames:
            base, _, ext = original_safe.rpartition(".")
            if base:
                safe_fn = f"{base}_{suffix_idx}.{ext}"
            else:
                safe_fn = f"{original_safe}_{suffix_idx}"
            suffix_idx += 1
        seen_filenames.add(safe_fn)

        s3_key = f"{ctx.date_str}/{ctx.batch_id}/{safe_fn}"

        try:
            presigned_url = s3_client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": ctx.bucket,
                    "Key": s3_key,
                    "ContentType": content_type,
                },
                ExpiresIn=ctx.expires_in,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            return _error(f"S3 error [{code}]: {msg}")
        except Exception as exc:
            return _error(f"Failed to generate presigned URL for {raw_filename}: {exc}")

        file_results.append(
            {
                "filename": raw_filename,
                "presigned_url": presigned_url,
                "s3_key": s3_key,
                "s3_uri": f"s3://{ctx.bucket}/{s3_key}",
                "method": "PUT",
                "headers": {"Content-Type": content_type},
            }
        )

    # Write single batch audit record
    audit_item = {
        "request_id": ctx.batch_id,
        "action": "presigned_upload_batch",
        "status": "urls_issued",
        "filenames": [f["filename"] for f in ctx.files],
        "file_count": len(ctx.files),
        "bucket": ctx.bucket,
        "source": ctx.source,
        "reason": ctx.reason,
        "account_id": ctx.account_id,
        "expires_at": expires_at_ts,
        "created_at": now,
        "ttl": expires_at_ts + 60,
    }
    table.put_item(Item=audit_item)

    # Notify (silent) — must not include any presigned URLs
    try:
        send_presigned_batch_notification(
            source=ctx.source,
            count=len(file_results),
            account_id=ctx.account_id,
            expires_at=expires_at_iso,
        )
    except Exception as _notify_exc:
        print(f"[PRESIGNED BATCH] notification error (non-fatal): {_notify_exc}")

    payload = {
        "status": "ready",
        "batch_id": ctx.batch_id,
        "file_count": len(file_results),
        "files": file_results,
        "expires_at": expires_at_iso,
        "bucket": ctx.bucket,
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
# Public MCP tool entry-point (batch)
# =============================================================================


def mcp_tool_request_presigned_batch(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_presigned_batch.

    Issues presigned S3 PUT URLs for up to 50 files in a single call.
    All files share the same batch_id prefix in the staging bucket.
    No human approval is required.
    """
    # Phase 1: parse & validate
    ctx = _parse_presigned_batch_request(req_id, arguments)
    if not isinstance(ctx, PresignedBatchContext):
        return ctx  # validation error dict

    # Phase 2: rate limit check
    if ctx.source:
        try:
            check_rate_limit(ctx.source)
        except (RateLimitExceeded, PendingLimitExceeded) as exc:
            return mcp_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"status": "error", "error": str(exc)}
                            ),
                        }
                    ],
                    "isError": True,
                },
            )

    # Phase 3: resolve batch target (batch_id / bucket / date_str)
    _resolve_batch_target(ctx)

    # Phase 4: generate URLs + write audit record
    return _generate_presigned_batch_urls(ctx)
