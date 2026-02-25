"""
Bouncer - Presigned Upload Pipeline

PresignedContext + PresignedBatchContext + shared pipeline helpers +
mcp_tool_request_presigned() + mcp_tool_request_presigned_batch().

Approach B (Aggressive Abstraction):
- Shared helper ``_parse_common_presigned_params`` extracts account/expires_in
  parsing and validation that both single and batch tools need.
- ``_generate_presigned_url_for_file`` encapsulates the S3 call so single and
  batch tools call the same code path.
- ``PresignedBatchContext`` mirrors ``PresignedContext`` at the batch level.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

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
_MAX_BATCH_FILES = 50


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


@dataclass
class PresignedBatchContext:
    """Pipeline context for mcp_tool_request_presigned_batch.

    Mirrors :class:`PresignedContext` at the batch level — one context
    object covers all files in the batch and records the shared batch_id,
    bucket, and generated per-file results.
    """

    req_id: str
    files: list  # [{"filename": str, "content_type": str}, ...]
    reason: str
    source: str
    account_id: str
    expires_in: int
    # Resolved after validation
    batch_id: str = field(default="")
    bucket: str = field(default="")
    date_str: str = field(default="")
    # Populated during URL generation
    results: list = field(default_factory=list)


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


# =============================================================================
# Shared helpers — used by both single and batch pipelines
# =============================================================================


def _error_result(req_id: str, message: str) -> dict:
    """Return a standard MCP error result dict."""
    return mcp_result(
        req_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"status": "error", "error": message}),
                }
            ],
            "isError": True,
        },
    )


def _parse_common_presigned_params(
    req_id: str, arguments: dict
) -> "tuple[str, int, dict | None]":
    """Parse and validate parameters shared by single and batch presigned tools.

    Returns ``(account_id, expires_in, error_or_None)`` where *error* is a
    ready-to-return MCP error dict when validation fails.
    """
    account_id = str(arguments.get("account", DEFAULT_ACCOUNT_ID or "")).strip()
    account_id = account_id or DEFAULT_ACCOUNT_ID or ""

    try:
        expires_in = int(arguments.get("expires_in", _DEFAULT_EXPIRES_IN))
    except (TypeError, ValueError):
        return "", 0, _error_result(req_id, "expires_in must be an integer")

    if expires_in <= 0:
        return "", 0, _error_result(req_id, "expires_in must be a positive integer")

    if expires_in < _MIN_EXPIRES_IN:
        return (
            "",
            0,
            _error_result(
                req_id,
                f"expires_in must be at least {_MIN_EXPIRES_IN} seconds",
            ),
        )

    if expires_in > _MAX_EXPIRES_IN:
        return (
            "",
            0,
            _error_result(
                req_id,
                f"expires_in exceeds maximum allowed value of {_MAX_EXPIRES_IN} seconds",
            ),
        )

    return account_id, expires_in, None


def _check_rate_limit_for_source(req_id: str, source: str) -> "dict | None":
    """Run rate-limit check for *source*.  Returns an error dict on failure, else None."""
    if not source:
        return None
    try:
        check_rate_limit(source)
    except (RateLimitExceeded, PendingLimitExceeded) as exc:
        return _error_result(req_id, str(exc))
    return None


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
    """Parse and validate single-file request arguments.

    Returns a :class:`PresignedContext` on success, or an MCP error dict
    on validation failure.  Delegates account/expires_in parsing to the
    shared helper :func:`_parse_common_presigned_params`.
    """
    filename = str(arguments.get("filename", "")).strip()
    content_type = str(arguments.get("content_type", "")).strip()
    reason = str(arguments.get("reason", "")).strip()
    source = str(arguments.get("source", "")).strip()

    # Validate required fields first (before expensive common-param parsing)
    for param, value in [
        ("filename", filename),
        ("content_type", content_type),
        ("reason", reason),
        ("source", source),
    ]:
        if not value:
            return _error_result(req_id, f"{param} is required")

    # Shared account + expires_in parsing/validation
    account_id, expires_in, err = _parse_common_presigned_params(req_id, arguments)
    if err is not None:
        return err

    return PresignedContext(
        req_id=req_id,
        filename=filename,
        content_type=content_type,
        reason=reason,
        source=source,
        account_id=account_id,
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

    Delegates the actual S3 call to :func:`_generate_presigned_url_for_file`.
    Returns an MCP result dict on success, or an MCP error dict on failure.
    """
    presigned_url, err_msg = _generate_presigned_url_for_file(
        ctx.bucket, ctx.s3_key, ctx.content_type, ctx.expires_in
    )
    if err_msg is not None:
        return _error_result(ctx.req_id, err_msg)

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
# Public MCP tool entry-point — single file
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
    err = _check_rate_limit_for_source(req_id, ctx.source)
    if err is not None:
        return err

    # Phase 3: resolve target (bucket / key / request_id)
    _resolve_presigned_target(ctx)

    # Phase 4: generate URL + write audit record
    return _generate_presigned_url(ctx)


# =============================================================================
# Batch presigned pipeline
# =============================================================================


def _parse_presigned_batch_request(
    req_id: str, arguments: dict
) -> "PresignedBatchContext | dict":
    """Parse and validate batch-request arguments.

    Returns a :class:`PresignedBatchContext` on success, or an MCP error
    dict on validation failure.
    """
    files = arguments.get("files")
    reason = str(arguments.get("reason", "")).strip()
    source = str(arguments.get("source", "")).strip()

    # Required field checks
    if not reason:
        return _error_result(req_id, "reason is required")
    if source == "":
        # source is optional per spec but we still need to track it
        source = ""

    # files validation
    if files is None or not isinstance(files, list):
        return _error_result(req_id, "files is required and must be an array")
    if len(files) == 0:
        return _error_result(req_id, "files array must not be empty")
    if len(files) > _MAX_BATCH_FILES:
        return _error_result(
            req_id,
            f"files array exceeds maximum of {_MAX_BATCH_FILES} items",
        )

    # Validate each file entry
    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            return _error_result(req_id, f"files[{idx}] must be an object")
        if not str(f.get("filename", "")).strip():
            return _error_result(req_id, f"files[{idx}].filename is required")
        if not str(f.get("content_type", "")).strip():
            return _error_result(req_id, f"files[{idx}].content_type is required")

    # Shared account + expires_in parsing/validation
    account_id, expires_in, err = _parse_common_presigned_params(req_id, arguments)
    if err is not None:
        return err

    return PresignedBatchContext(
        req_id=req_id,
        files=files,
        reason=reason,
        source=source,
        account_id=account_id,
        expires_in=expires_in,
    )


def _resolve_presigned_batch_target(ctx: PresignedBatchContext) -> None:
    """Assign batch_id, bucket, and date_str to *ctx* in-place."""
    ctx.batch_id = generate_request_id("presigned_batch")
    ctx.bucket = f"bouncer-uploads-{ctx.account_id}"
    ctx.date_str = time.strftime("%Y-%m-%d")


def _generate_presigned_urls_for_batch(
    ctx: PresignedBatchContext,
) -> "dict | None":
    """Generate presigned PUT URLs for all files in *ctx*.

    Populates ``ctx.results`` with per-file dicts on success.

    On failure (any file), returns a partial-failure MCP error dict describing
    which files succeeded and which failed (rollback semantics: no DynamoDB
    record is written if any URL generation fails).

    Returns ``None`` on full success so the caller can proceed to write the
    audit record.
    """
    now = int(time.time())
    expires_at_ts = now + ctx.expires_in
    expires_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_ts))

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for file_entry in ctx.files:
        raw_filename = str(file_entry.get("filename", "")).strip()
        content_type = str(file_entry.get("content_type", "")).strip()
        safe_filename = _sanitize_filename(raw_filename)
        s3_key = f"{ctx.date_str}/{ctx.batch_id}/{safe_filename}"
        s3_uri = f"s3://{ctx.bucket}/{s3_key}"

        url, err_msg = _generate_presigned_url_for_file(
            ctx.bucket, s3_key, content_type, ctx.expires_in
        )
        if err_msg is not None:
            failed.append({"filename": raw_filename, "error": err_msg})
        else:
            succeeded.append(
                {
                    "filename": raw_filename,
                    "presigned_url": url,
                    "s3_key": s3_key,
                    "s3_uri": s3_uri,
                    "method": "PUT",
                    "headers": {"Content-Type": content_type},
                    "expires_at": expires_at_iso,
                }
            )

    if failed:
        # Rollback: report partial failure, do NOT write audit record
        return mcp_result(
            ctx.req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "error": "One or more presigned URLs could not be generated",
                                "succeeded": succeeded,
                                "failed": failed,
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )

    ctx.results = succeeded
    return None  # signal success


def _write_batch_audit_record(ctx: PresignedBatchContext) -> None:
    """Write a single DynamoDB audit record for the entire batch."""
    now = int(time.time())
    expires_at_ts = now + ctx.expires_in
    filenames = [f.get("filename", "") for f in ctx.files]
    audit_item = {
        "request_id": ctx.batch_id,
        "action": "presigned_upload_batch",
        "status": "urls_issued",
        "batch_id": ctx.batch_id,
        "file_count": len(ctx.files),
        "filenames": filenames,
        "bucket": ctx.bucket,
        "source": ctx.source,
        "reason": ctx.reason,
        "account_id": ctx.account_id,
        "expires_at": expires_at_ts,
        "created_at": now,
        "ttl": expires_at_ts + 60,
    }
    table.put_item(Item=audit_item)


# =============================================================================
# Public MCP tool entry-point — batch
# =============================================================================


def mcp_tool_request_presigned_batch(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_presigned_batch.

    Issues presigned S3 PUT URLs for multiple files in a single call.
    No human approval is required — files go to the staging bucket only.
    If any URL generation fails the entire operation is rolled back and
    a partial-failure response is returned.
    """
    # Phase 1: parse & validate
    ctx = _parse_presigned_batch_request(req_id, arguments)
    if not isinstance(ctx, PresignedBatchContext):
        return ctx  # validation error dict

    # Phase 2: rate limit check (uses shared helper)
    err = _check_rate_limit_for_source(req_id, ctx.source)
    if err is not None:
        return err

    # Phase 3: resolve batch target (batch_id, bucket, date)
    _resolve_presigned_batch_target(ctx)

    # Phase 4: generate all presigned URLs (with rollback on partial failure)
    err = _generate_presigned_urls_for_batch(ctx)
    if err is not None:
        return err  # partial failure

    # Phase 5: write batch audit record
    _write_batch_audit_record(ctx)

    # Phase 6: build and return response
    now = int(time.time())
    expires_at_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + ctx.expires_in)
    )
    payload = {
        "status": "ready",
        "batch_id": ctx.batch_id,
        "file_count": len(ctx.results),
        "files": ctx.results,
        "expires_at": expires_at_iso,
        "bucket": ctx.bucket,
    }
    return mcp_result(
        req_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload),
                }
            ]
        },
    )
