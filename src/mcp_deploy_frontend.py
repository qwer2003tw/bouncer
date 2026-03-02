"""
Bouncer - Frontend Deployment Tool (Phase A)

Implements bouncer_deploy_frontend:
  - Input validation (index.html required, extension blocklist, size limits)
  - Content-Type + Cache-Control header calculation per file
  - Stage all files to S3 staging bucket (pending/ prefix)
  - Write DDB pending record (action=deploy_frontend, status=pending_approval)
  - Send Telegram approval notification

Phase B (callback / actual S3 deploy / CloudFront invalidation) is NOT included here.
"""

import base64
import json
import logging
import time
from typing import Optional

import boto3

from constants import DEFAULT_ACCOUNT_ID, APPROVAL_TTL_BUFFER, UPLOAD_TIMEOUT
from db import table
from notifications import send_deploy_frontend_notification
from utils import generate_request_id, mcp_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project Config (hardcoded; could be moved to DDB in the future)
# ---------------------------------------------------------------------------

_PROJECT_CONFIG = {
    "ztp-files": {
        "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
        "distribution_id": "E176PW0SA5JF29",
        "region": "us-east-1",
    }
}

# ---------------------------------------------------------------------------
# Security: Blocked Extensions
# ---------------------------------------------------------------------------

_BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".wsf",
    ".msi", ".msp", ".com", ".scr", ".jar", ".py", ".rb", ".pl",
    ".php", ".asp", ".aspx", ".jsp", ".cgi", ".bin", ".elf",
}

# ---------------------------------------------------------------------------
# Size Limits
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB per file
MAX_TOTAL_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB total
MAX_FILE_COUNT = 200


# ---------------------------------------------------------------------------
# Cache-Control helpers
# ---------------------------------------------------------------------------

def _get_cache_control(filename: str) -> str:
    """Return the correct Cache-Control header value for a given filename."""
    name = filename.lstrip("/")
    if name == "index.html":
        return "no-cache, no-store, must-revalidate"
    if name.startswith("assets/"):
        return "max-age=31536000, immutable"
    return "no-cache"


def _get_content_type(filename: str, provided_ct: Optional[str]) -> str:
    """Return content_type, preferring caller-supplied value, then guessing from extension."""
    if provided_ct and provided_ct.strip():
        return provided_ct.strip()

    import mimetypes
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _has_blocked_extension(filename: str) -> bool:
    lower = filename.lower()
    for ext in _BLOCKED_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def _validate_files(files: list) -> Optional[str]:
    """Validate the files array.  Returns an error string, or None on success."""
    if not files or not isinstance(files, list):
        return "files array is required and must be non-empty"

    if len(files) > MAX_FILE_COUNT:
        return f"Too many files: {len(files)} (max {MAX_FILE_COUNT})"

    filenames = set()
    has_index_html = False
    total_decoded_size = 0

    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return f"File #{i + 1}: must be an object"

        fname = str(f.get("filename", "")).strip()
        content_b64 = str(f.get("content", "")).strip()

        if not fname:
            return f"File #{i + 1}: filename is required"

        if fname in filenames:
            return f"Duplicate filename: {fname}"
        filenames.add(fname)

        if _has_blocked_extension(fname):
            return f"File #{i + 1} ({fname}): blocked extension"

        if not content_b64:
            return f"File #{i + 1} ({fname}): content is required"

        if len(content_b64) % 4 != 0:
            return (
                f"File #{i + 1} ({fname}): invalid base64 content "
                "(length not a multiple of 4 - likely truncated)"
            )

        try:
            content_bytes = base64.b64decode(content_b64)
        except Exception as exc:
            return f"File #{i + 1} ({fname}): invalid base64 - {exc}"

        file_size = len(content_bytes)
        if file_size > MAX_FILE_SIZE_BYTES:
            return (
                f"File #{i + 1} ({fname}): too large "
                f"({file_size} bytes, max {MAX_FILE_SIZE_BYTES})"
            )

        total_decoded_size += file_size

        if fname.lower() == "index.html":
            has_index_html = True

    if not has_index_html:
        return "index.html is required in files"

    if total_decoded_size > MAX_TOTAL_SIZE_BYTES:
        return (
            f"Total size {total_decoded_size} bytes exceeds limit "
            f"({MAX_TOTAL_SIZE_BYTES} bytes)"
        )

    return None  # all good


# ---------------------------------------------------------------------------
# Main tool entry point
# ---------------------------------------------------------------------------

def mcp_tool_deploy_frontend(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_frontend

    Phase A: validate -> stage to S3 -> write DDB -> send Telegram approval.
    """

    # 1. Extract arguments
    files = arguments.get("files", [])
    project = str(arguments.get("project", "")).strip()
    reason = str(arguments.get("reason", "No reason provided")).strip()
    source = arguments.get("source", None)
    trust_scope = str(arguments.get("trust_scope", "")).strip()

    # 2. Validate project
    if not project:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "project is required",
            })}],
            "isError": True,
        })

    project_config = _PROJECT_CONFIG.get(project)
    if not project_config:
        available = list(_PROJECT_CONFIG.keys())
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": f"Unknown project: {project}",
                "available_projects": available,
            })}],
            "isError": True,
        })

    # 3. Validate files
    error = _validate_files(files)
    if error:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": error,
            })}],
            "isError": True,
        })

    # 4. Pre-process files (decode + compute metadata)
    request_id = generate_request_id(f"deploy_frontend:{project}")
    staging_bucket = f"bouncer-uploads-{DEFAULT_ACCOUNT_ID}"

    processed_files = []
    total_size = 0

    for f in files:
        fname = str(f["filename"]).strip()
        content_b64 = str(f["content"]).strip()
        content_bytes = base64.b64decode(content_b64)
        file_size = len(content_bytes)
        ct = _get_content_type(fname, f.get("content_type"))
        cc = _get_cache_control(fname)
        s3_key = f"pending/{request_id}/{fname}"

        processed_files.append({
            "filename": fname,
            "content_bytes": content_bytes,
            "content_type": ct,
            "cache_control": cc,
            "size": file_size,
            "s3_key": s3_key,
        })
        total_size += file_size

    # 5. Stage files to S3
    s3 = boto3.client("s3")
    staged_keys = []

    for pf in processed_files:
        try:
            s3.put_object(
                Bucket=staging_bucket,
                Key=pf["s3_key"],
                Body=pf["content_bytes"],
                ContentType=pf["content_type"],
            )
            staged_keys.append(pf["s3_key"])
        except Exception as exc:
            # Rollback: delete already-staged objects
            for rk in staged_keys:
                try:
                    s3.delete_object(Bucket=staging_bucket, Key=rk)
                except Exception:
                    pass
            return mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "error",
                    "error": f"Failed to stage {pf['filename']} to S3: {exc}",
                })}],
                "isError": True,
            })

    # 6. Build files manifest (without raw content_bytes)
    files_manifest = [
        {
            "filename": pf["filename"],
            "s3_key": pf["s3_key"],
            "content_type": pf["content_type"],
            "cache_control": pf["cache_control"],
            "size": pf["size"],
        }
        for pf in processed_files
    ]

    # 7. Write DDB pending record
    ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER

    item = {
        "request_id": request_id,
        "action": "deploy_frontend",
        "status": "pending_approval",
        "project": project,
        "frontend_bucket": project_config["frontend_bucket"],
        "distribution_id": project_config["distribution_id"],
        "region": project_config.get("region", "us-east-1"),
        "staging_bucket": staging_bucket,
        "files": json.dumps(files_manifest),
        "file_count": len(processed_files),
        "total_size": total_size,
        "reason": reason,
        "source": source or "__anonymous__",
        "trust_scope": trust_scope,
        "created_at": int(time.time()),
        "ttl": ttl,
        "mode": "mcp",
    }
    table.put_item(Item=item)

    # 8. Send Telegram approval notification
    target_info = {
        "frontend_bucket": project_config["frontend_bucket"],
        "distribution_id": project_config["distribution_id"],
        "region": project_config.get("region", "us-east-1"),
    }

    notif_result = send_deploy_frontend_notification(
        request_id=request_id,
        files_summary=files_manifest,
        target_info=target_info,
        project=project,
        reason=reason,
        source=source,
    )

    if not notif_result.ok:
        # Cleanup DDB and staged objects to avoid orphan records
        try:
            table.delete_item(Key={"request_id": request_id})
        except Exception as del_err:
            logger.error("[DEPLOY-FRONTEND] DDB cleanup failed for %s: %s", request_id, del_err)
        for rk in staged_keys:
            try:
                s3.delete_object(Bucket=staging_bucket, Key=rk)
            except Exception:
                pass
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "Telegram notification failed; deploy request was not created. Please retry.",
            })}],
            "isError": True,
        })

    # Store telegram_message_id in DDB for later keyboard removal
    if notif_result.message_id:
        from notifications import post_notification_setup
        post_notification_setup(
            request_id=request_id,
            telegram_message_id=notif_result.message_id,
            expires_at=ttl,
        )

    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps({
            "status": "pending_approval",
            "request_id": request_id,
            "file_count": len(processed_files),
            "message": "Frontend deploy request sent. Use bouncer_status to poll.",
            "expires_in": f"{UPLOAD_TIMEOUT} seconds",
        })}],
    })
