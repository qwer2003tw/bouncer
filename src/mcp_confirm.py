"""
Bouncer - Confirm Upload Module (Approach C)

Verifies that files uploaded via presigned URLs actually made it to the
staging bucket.  Uses a single list_objects_v2 call for efficiency, then
writes the confirm result to DynamoDB for audit / bouncer_status queries.

Design notes (Approach C):
- One list_objects_v2 per call (cheaper than N HeadObject calls)
- DynamoDB audit record: pk=CONFIRM#{batch_id}, TTL=7 days
- Max 50 files per call (anti-abuse)
- No Telegram notification (read-only verification, no approval needed)
"""

import json
import re
import time

import boto3
from botocore.exceptions import ClientError

from constants import STAGING_BUCKET
from db import table
from utils import mcp_result


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIRM_MAX_FILES = 50
_BATCH_ID_RE = re.compile(r"^batch-[0-9a-f]{12}$")
_CONFIRM_TTL_DAYS = 7
_LIST_MAX_KEYS = 1000  # list_objects_v2 page size (AWS default max)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _error_result(req_id: str, message: str) -> dict:
    """Return an MCP isError result dict."""
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


def _list_batch_keys(bucket: str, prefix: str) -> "tuple[set[str], str | None]":
    """List all object keys under *prefix* using paginated list_objects_v2.

    Returns ``(key_set, error_message)``.  On success ``error_message`` is
    ``None``; on failure ``key_set`` is empty.
    """
    s3_client = boto3.client("s3")
    found_keys: set[str] = set()
    continuation_token = None

    try:
        while True:
            kwargs: dict = {
                "Bucket": bucket,
                "Prefix": prefix,
                "MaxKeys": _LIST_MAX_KEYS,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = s3_client.list_objects_v2(**kwargs)

            for obj in response.get("Contents", []):
                found_keys.add(obj["Key"])

            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        return set(), f"S3 error [{code}]: {msg}"
    except Exception as exc:
        return set(), f"Failed to list objects: {exc}"

    return found_keys, None


def _write_confirm_record(
    batch_id: str,
    verified: bool,
    missing: list,
    results: list,
) -> None:
    """Write confirm result to DynamoDB (best-effort; errors are logged)."""
    now = int(time.time())
    ttl = now + _CONFIRM_TTL_DAYS * 86400

    item = {
        "request_id": f"CONFIRM#{batch_id}",
        "request_type": "CONFIRM",
        "batch_id": batch_id,
        "action": "confirm_upload",
        "status": "verified" if verified else "incomplete",
        "verified": verified,
        "missing": missing,
        "file_count": len(results),
        "missing_count": len(missing),
        "checked_at": now,
        "created_at": now,
        "ttl": ttl,
    }

    try:
        table.put_item(Item=item)
    except Exception as exc:
        # Non-fatal: log and continue; verification result is still returned.
        print(f"[confirm_upload] DynamoDB write failed: {exc}")


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def handle_confirm_upload(params: dict) -> dict:
    """MCP tool handler for ``bouncer_confirm_upload``.

    Validates *params*, lists the staging bucket for the batch prefix,
    matches the requested files, writes an audit record, and returns the
    verification result.
    """
    req_id = params.get("_req_id", "confirm")

    # ---- Validate batch_id ----
    batch_id = str(params.get("batch_id", "")).strip()
    if not batch_id:
        return _error_result(req_id, "batch_id is required")
    if not _BATCH_ID_RE.match(batch_id):
        return _error_result(req_id, "batch_id must match format batch-{12 hex chars}")

    # ---- Validate files ----
    files_raw = params.get("files")
    if not isinstance(files_raw, list) or len(files_raw) == 0:
        return _error_result(req_id, "files must be a non-empty array")

    if len(files_raw) > _CONFIRM_MAX_FILES:
        return _error_result(
            req_id,
            f"files exceeds maximum of {_CONFIRM_MAX_FILES} items",
        )

    # Extract s3_key strings
    requested_keys: list[str] = []
    for i, entry in enumerate(files_raw):
        if not isinstance(entry, dict):
            return _error_result(req_id, f"files[{i}] must be an object")
        s3_key = str(entry.get("s3_key", "")).strip()
        if not s3_key:
            return _error_result(req_id, f"files[{i}].s3_key is required")
        requested_keys.append(s3_key)

    # ---- Determine bucket + prefix ----
    bucket = STAGING_BUCKET
    # The batch prefix is the common parent of all batch keys.
    # Keys are expected to follow: {date}/{batch_id}/{filename}
    # We derive the prefix from the batch_id portion.
    # Use the batch_id segment to find the prefix that covers all files.
    batch_prefix = _derive_batch_prefix(batch_id, requested_keys)

    # ---- List objects in staging bucket ----
    found_keys, list_error = _list_batch_keys(bucket, batch_prefix)
    if list_error:
        return _error_result(req_id, list_error)

    # ---- Match requested files ----
    results = []
    missing = []

    for s3_key in requested_keys:
        exists = s3_key in found_keys
        result_entry: dict = {"s3_key": s3_key, "exists": exists}
        if not exists:
            missing.append(s3_key)
        results.append(result_entry)

    verified = len(missing) == 0

    # ---- Write audit record to DynamoDB ----
    _write_confirm_record(batch_id, verified, missing, results)

    # ---- Return result ----
    payload = {
        "batch_id": batch_id,
        "verified": verified,
        "results": results,
        "missing": missing,
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


def _derive_batch_prefix(batch_id: str, keys: list) -> str:
    """Derive the S3 prefix that covers the given batch.

    Strategy: find the longest common path prefix that contains
    the batch_id segment.  Falls back to just the batch_id itself
    if heuristics fail (e.g. custom key layouts).

    Expected key format: ``{date}/{batch_id}/{filename}``
    """
    for key in keys:
        # Find the segment ending at batch_id
        idx = key.find(batch_id)
        if idx != -1:
            # prefix is everything up to and including the batch_id + "/"
            end = idx + len(batch_id)
            return key[:end] + "/"

    # Fallback: use batch_id directly
    return batch_id + "/"
