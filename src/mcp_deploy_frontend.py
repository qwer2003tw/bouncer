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
import binascii
import json
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger
from aws_clients import get_s3_client

from constants import DEFAULT_ACCOUNT_ID, APPROVAL_TIMEOUT_DEFAULT, APPROVAL_TTL_BUFFER, UPLOAD_TIMEOUT, DEFAULT_REGION, TTL_30_DAYS, PROJECTS_TABLE
from db import table, deployer_projects_table, deployer_history_table
from notifications import send_deploy_frontend_notification
from utils import generate_request_id, mcp_result, mcp_error, format_size_human
from aws_clients import get_cloudfront_client  # noqa: E402
from metrics import emit_metric  # noqa: E402
from notifications import post_notification_setup, send_trust_auto_approve_notification  # noqa: E402
from trust import TrustRateExceeded, increment_trust_command_count, should_trust_approve  # noqa: E402
from utils import log_decision  # noqa: E402
from telegram import send_telegram_message_silent  # noqa: E402

logger = Logger(service="bouncer")

# ---------------------------------------------------------------------------
# Project Config (DynamoDB-only; no hardcoded fallback)
# ---------------------------------------------------------------------------
# All project configs must be stored in the DynamoDB 'bouncer-projects' table.
# To add a new frontend project, run:
#   python3 scripts/seed_frontend_configs.py
# See SKILL.md > "Adding a New Frontend Project" for full instructions.
# ---------------------------------------------------------------------------

_REGION = DEFAULT_REGION

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
# DynamoDB config lookup
# ---------------------------------------------------------------------------

def _get_frontend_config(project_id: str) -> Optional[dict]:
    """Get frontend project config from DynamoDB bouncer-projects table.

    The project record is expected to contain frontend-specific fields:
      - frontend_bucket
      - frontend_distribution_id  (CloudFront distribution ID)
      - frontend_region            (optional, defaults to us-east-1)
      - frontend_deploy_role_arn   (IAM role for S3/CF deploy)

    Returns a normalised config dict with keys: frontend_bucket, distribution_id, region, deploy_role_arn;
    or None if the project record does not exist or has no frontend fields.
    """
    try:
        dynamodb = boto3.resource('dynamodb', region_name=_REGION)
        projects_table = dynamodb.Table(PROJECTS_TABLE)
        resp = projects_table.get_item(Key={'project_id': project_id})
        item = resp.get('Item')

        if not item:
            return None

        # Map DDB item fields -> canonical config keys
        frontend_bucket = item.get('frontend_bucket')
        distribution_id = item.get('frontend_distribution_id')
        region = item.get('frontend_region', DEFAULT_REGION)
        deploy_role_arn = item.get('frontend_deploy_role_arn')

        if not frontend_bucket or not distribution_id:
            # Project record exists but has no frontend config
            return None

        return {
            'frontend_bucket': frontend_bucket,
            'distribution_id': distribution_id,
            'region': region,
            'deploy_role_arn': deploy_role_arn,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "DDB config lookup failed for %s: %s",
            project_id, exc,
            extra={"src_module": "deploy_frontend", "operation": "get_project_config_ddb", "project_id": project_id, "error": str(exc)},
        )
        return None


def _get_project_config(project_id: str) -> Optional[dict]:
    """Return frontend project config from DynamoDB bouncer-projects table.

    If the project is not found in DynamoDB, returns None.
    The caller is responsible for returning a meaningful error to the user.

    To add a new frontend project, seed the DynamoDB table:
        python3 scripts/seed_frontend_configs.py
    """
    return _get_frontend_config(project_id)


def _list_known_projects() -> list:
    """Return list of known frontend project IDs from DynamoDB."""
    known = set()
    try:
        dynamodb = boto3.resource('dynamodb', region_name=_REGION)
        projects_table = dynamodb.Table(PROJECTS_TABLE)
        result = projects_table.scan(
            ProjectionExpression='project_id, frontend_bucket',
        )
        for item in result.get('Items', []):
            if item.get('frontend_bucket'):
                known.add(item['project_id'])
    except Exception:  # noqa: BLE001 — fallback to empty list
        logger.warning("[DEPLOY-FRONTEND] Failed to list known projects from DDB, returning empty list", extra={"src_module": "deploy_frontend", "operation": "list_known_projects"})
    return sorted(known)


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

        # Path traversal prevention
        if '..' in fname or fname.startswith('/') or fname.startswith('\\'):
            return f"Invalid filename '{fname}': path traversal not allowed"

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
        except (binascii.Error, ValueError) as exc:
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
# Helper: Trust session check
# ---------------------------------------------------------------------------

def _cleanup_stale_assets(s3_target, frontend_bucket: str, deployed_keys: set, request_id: str, project: str) -> dict:
    """Remove stale assets from frontend bucket after deploy.

    Lists all objects under assets/ prefix, deletes any not in deployed_keys.
    Only cleans assets/ — root files (index.html, favicon.ico) are NOT touched.

    Args:
        s3_target: S3 client (may be assumed-role client)
        frontend_bucket: Target bucket name
        deployed_keys: Set of S3 keys that were just deployed
        request_id: For logging
        project: For logging

    Returns:
        dict: {deleted_count: int, deleted_bytes: int, errors: list}
    """
    deleted_count = 0
    deleted_bytes = 0
    errors = []

    try:
        # List all objects under assets/ prefix
        paginator = s3_target.get_paginator('list_objects_v2')
        stale_keys = []

        for page in paginator.paginate(Bucket=frontend_bucket, Prefix='assets/'):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key not in deployed_keys:
                    stale_keys.append({'Key': key})
                    deleted_bytes += obj.get('Size', 0)

        if not stale_keys:
            logger.info("No stale assets to clean up", extra={
                "src_module": "deploy_frontend",
                "operation": "cleanup_stale_assets",
                "request_id": request_id,
                "project": project,
            })
            return {'deleted_count': 0, 'deleted_bytes': 0, 'errors': []}

        # Delete in batches of 1000 (S3 limit)
        for i in range(0, len(stale_keys), 1000):
            batch = stale_keys[i:i+1000]
            s3_target.delete_objects(
                Bucket=frontend_bucket,
                Delete={'Objects': batch}
            )
            deleted_count += len(batch)

        logger.info("Cleaned up %d stale assets (%d bytes)", deleted_count, deleted_bytes, extra={
            "src_module": "deploy_frontend",
            "operation": "cleanup_stale_assets",
            "request_id": request_id,
            "project": project,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
        })
    except Exception as e:
        logger.warning("Stale asset cleanup failed (non-critical): %s", e, extra={
            "src_module": "deploy_frontend",
            "operation": "cleanup_stale_assets",
            "request_id": request_id,
            "project": project,
            "error": str(e),
        })
        errors.append(str(e))

    return {'deleted_count': deleted_count, 'deleted_bytes': deleted_bytes, 'errors': errors}


def _check_deploy_trust(trust_scope: str, project: str, account_id: str, source: str) -> tuple:
    """Check if trust_scope allows auto-approval for frontend deployment.

    Returns (should_trust: bool, trust_session: dict or None, reason: str)
    """

    # 1. Check trust session
    synthetic_command = f"bouncer_deploy_frontend project={project}"
    should_trust, trust_session, reason = should_trust_approve(
        command=synthetic_command,
        trust_scope=trust_scope,
        account_id=account_id,
        source=source,
    )

    if not should_trust:
        logger.info("Trust denied: %s", reason, extra={"src_module": "deploy_frontend", "operation": "check_trust_deploy", "project": project, "reason": reason})
        return False, None, reason

    # 2. Validate project has deploy_role_arn (frontend projects only)
    try:
        resp = deployer_projects_table.get_item(Key={"project_id": project})
        item = resp.get("Item")
        if not item or not item.get("frontend_deploy_role_arn"):
            logger.warning("Trust denied: no deploy_role_arn, project=%s", project, extra={"src_module": "deploy_frontend", "operation": "check_trust_deploy", "project": project})
            return False, None, "project not configured for frontend deploy"
    except ClientError:
        logger.warning("Trust denied: project verification failed", extra={"src_module": "deploy_frontend", "operation": "check_trust_deploy", "project": project}, exc_info=True)
        return False, None, "project verification failed"

    logger.info("Trust approved, project=%s, scope=%s", project, trust_scope, extra={"src_module": "deploy_frontend", "operation": "check_trust_deploy", "project": project, "trust_scope": trust_scope})
    return True, trust_session, reason


# ---------------------------------------------------------------------------
# Helper: Submit for approval
# ---------------------------------------------------------------------------

def _stage_files_to_s3(processed_files: list, staging_bucket: str, req_id: str):
    """Stage processed files to S3.

    Returns (staged_keys, error_result). If error_result is not None, staging failed.
    """
    s3 = get_s3_client()
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
        except ClientError as exc:
            # Rollback: delete already-staged objects
            for rk in staged_keys:
                try:
                    s3.delete_object(Bucket=staging_bucket, Key=rk)
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    logger.warning("Rollback cleanup failed (non-critical)",
                                 extra={"src_module": "deploy_frontend",
                                        "operation": "rollback_cleanup", "s3_key": rk})
            error_result = mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "error",
                    "error": f"Failed to stage {pf['filename']} to S3: {exc}",
                })}],
                "isError": True,
            })
            return staged_keys, error_result

    return staged_keys, None


def _submit_deploy_frontend_approval(
    req_id: str,
    files: list,
    project: str,
    project_config: dict,
    reason: str,
    source: Optional[str],
    trust_scope: str,
) -> dict:
    """Stage files to S3, write DDB record, and send Telegram notification.

    Returns an mcp_result dict with either success (pending_approval) or error.
    """
    # 1. Pre-process files (decode + compute metadata)
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

    # 2. Stage files to S3
    staged_keys, stage_error = _stage_files_to_s3(processed_files, staging_bucket, req_id)
    if stage_error is not None:
        return stage_error

    # 3. Build files manifest (without raw content_bytes)
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

    # 4. Write DDB pending record
    ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER

    item = {
        "request_id": request_id,
        "action": "deploy_frontend",
        "status": "pending_approval",
        "project": project,
        "frontend_bucket": project_config["frontend_bucket"],
        "distribution_id": project_config["distribution_id"],
        "region": project_config.get("region", DEFAULT_REGION),
        "deploy_role_arn": project_config.get("deploy_role_arn"),
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

    # 5. Send Telegram approval notification
    target_info = {
        "frontend_bucket": project_config["frontend_bucket"],
        "distribution_id": project_config["distribution_id"],
        "region": project_config.get("region", DEFAULT_REGION),
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
        except ClientError as del_err:
            logger.exception("DDB cleanup failed for %s: %s", request_id, del_err, extra={"src_module": "deploy_frontend", "operation": "submit_deploy_frontend", "request_id": request_id, "error": str(del_err)})
        s3 = get_s3_client()
        for rk in staged_keys:
            try:
                s3.delete_object(Bucket=staging_bucket, Key=rk)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning("Notification-failure cleanup skipped (non-critical)", extra={"src_module": "deploy_frontend", "operation": "notification_failure_cleanup", "s3_key": rk})
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "Telegram notification failed; deploy request was not created. Please retry.",
            })}],
            "isError": True,
        })

    # 6. Store telegram_message_id in DDB for later keyboard removal
    if notif_result.message_id:
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


# ---------------------------------------------------------------------------
# Helper: Execute approved deployment (trust sessions)
# ---------------------------------------------------------------------------

def _execute_deploy_frontend_approved(
    req_id: str,
    files: list,
    project: str,
    project_config: dict,
    reason: str,
    source: Optional[str],
    trust_session: dict,
    account_id: str,
    trust_scope: str,
) -> dict:
    """Execute frontend deployment directly (for trusted sessions).

    Mirrors the callback approval flow but executes immediately without manual approval.
    """

    # 1. Pre-process files (decode + compute metadata)
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

    # 2. Stage files to S3
    s3_staging = get_s3_client()
    staged_keys = []

    for pf in processed_files:
        try:
            s3_staging.put_object(
                Bucket=staging_bucket,
                Key=pf["s3_key"],
                Body=pf["content_bytes"],
                ContentType=pf["content_type"],
            )
            staged_keys.append(pf["s3_key"])
        except ClientError as exc:
            # Rollback: delete already-staged objects
            for rk in staged_keys:
                try:
                    s3_staging.delete_object(Bucket=staging_bucket, Key=rk)
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    logger.warning("Trust deploy rollback cleanup failed", extra={"src_module": "deploy_frontend", "operation": "trust_deploy_rollback_cleanup", "s3_key": rk}, exc_info=True)
            return mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "error",
                    "error": f"Failed to stage {pf['filename']} to S3: {exc}",
                })}],
                "isError": True,
            })

    # 3. Assume deploy role
    deploy_role_arn = project_config.get("deploy_role_arn")
    try:
        if deploy_role_arn:
            s3_target = get_s3_client(role_arn=deploy_role_arn, session_name=f"bouncer-deploy-{request_id[:16]}")
        else:
            s3_target = get_s3_client()
    except ClientError as exc:
        logger.exception("[DEPLOY-FRONTEND] Trust deploy AssumeRole failed for %s: %s", deploy_role_arn, exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_assume_role", "role_arn": deploy_role_arn, "error": str(exc)})
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": f"AssumeRole failed: {exc}",
            })}],
            "isError": True,
        })

    # 4. Deploy files to frontend bucket
    frontend_bucket = project_config["frontend_bucket"]
    deployed = []
    failed = []

    for pf in processed_files:
        filename = pf["filename"]
        staged_key = pf["s3_key"]
        content_type = pf["content_type"]
        cache_control = pf["cache_control"]

        try:
            # Copy from staging to frontend
            s3_target.copy_object(
                Bucket=frontend_bucket,
                Key=filename,
                CopySource={"Bucket": staging_bucket, "Key": staged_key},
                ContentType=content_type,
                CacheControl=cache_control,
                MetadataDirective="REPLACE",
            )
            deployed.append(filename)
            logger.info("Trust deploy file %s -> %s", filename, frontend_bucket, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_file", "file_name": filename, "bucket": frontend_bucket})
        except ClientError as exc:
            logger.exception("Trust deploy failed for %s: %s", filename, exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_file", "file_name": filename, "error": str(exc)})
            failed.append({"filename": filename, "reason": str(exc)})

    # 4.5. Clean up stale assets (best-effort, don't fail deploy if cleanup fails)
    deployed_keys = set(deployed)
    cleanup_result = _cleanup_stale_assets(s3_target, frontend_bucket, deployed_keys, request_id, project)

    # 5. CloudFront invalidation
    cf_invalidation_failed = False
    distribution_id = project_config["distribution_id"]
    if deployed:
        try:
            cf = get_cloudfront_client(role_arn=deploy_role_arn)
            cf.create_invalidation(
                DistributionId=distribution_id,
                InvalidationBatch={
                    "Paths": {"Quantity": 1, "Items": ["/*"]},
                    "CallerReference": request_id,
                },
            )
            logger.info("[DEPLOY-FRONTEND] Trust deploy CloudFront invalidation created for %s", distribution_id, extra={"src_module": "deploy_frontend", "operation": "cloudfront_invalidation", "distribution_id": distribution_id})
        except ClientError as exc:
            logger.exception("[DEPLOY-FRONTEND] Trust deploy CloudFront invalidation failed: %s", exc, extra={"src_module": "deploy_frontend", "operation": "cloudfront_invalidation", "distribution_id": distribution_id})
            cf_invalidation_failed = True

    # 6. Increment trust command count (s59-002: catch rate exceeded)
    trust_id = trust_session.get("request_id", "")
    try:
        new_count = increment_trust_command_count(trust_id)
    except TrustRateExceeded as exc:
        logger.warning("Trust rate exceeded for frontend deploy: %s", exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy", "trust_id": trust_id})
        emit_metric('Bouncer', 'TrustRateExceeded', 1, dimensions={'Event': 'frontend_deploy'})
        return mcp_error(
            req_id,
            'TRUST_RATE_EXCEEDED',
            f'信任時段命令速率過高，請稍候再試。{str(exc)}',
        )

    # 7. Log decision to DDB
    synthetic_command = f"bouncer_deploy_frontend project={project}"
    log_decision(
        table=table,
        request_id=request_id,
        command=synthetic_command,
        reason=reason,
        source=source or "__anonymous__",
        account_id=account_id,
        decision_type="trust_approved",
        trust_bypass=True,
        trust_scope=trust_scope,
        project=project,
    )

    # 8. Write deploy history
    now = int(time.time())
    history_status = "failed" if failed or cf_invalidation_failed else "completed"
    try:
        history_item = {
            "deploy_id": f"frontend-{request_id}",
            "project": project,
            "source": source or "__anonymous__",
            "reason": reason,
            "deployed_files": deployed,
            "failed_files": [f["filename"] for f in failed],
            "status": history_status,
            "deployed_at": now,
            "frontend_bucket": frontend_bucket,
            "distribution_id": distribution_id,
            "cf_invalidation_failed": cf_invalidation_failed,
            "request_id": request_id,
            "trust_bypass": True,
            "trust_scope": trust_scope,
            "ttl": now + TTL_30_DAYS,  # 30 days
        }
        deployer_history_table.put_item(Item=history_item)
    except ClientError as exc:
        logger.exception("[DEPLOY-FRONTEND] Trust deploy history write failed for %s: %s", request_id, exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_history_write", "request_id": request_id, "error": str(exc)})
    # 9. Send silent trust notification
    remaining = int(trust_session.get("expires_at", 0)) - now
    remaining_str = f"{remaining // 60}:{remaining % 60:02d}" if remaining > 0 else "0:00"

    cleanup_summary = ""
    if cleanup_result.get('deleted_count', 0) > 0:
        from utils import format_size_human
        deleted_count = cleanup_result['deleted_count']
        deleted_bytes = cleanup_result['deleted_bytes']
        deleted_size_human = format_size_human(deleted_bytes)
        cleanup_summary = f"\n🧹 Cleaned up {deleted_count} stale files ({deleted_size_human})"

    result_summary = (
        f"✅ Deployed {len(deployed)} files to {frontend_bucket}\n"
        f"CloudFront: {'✅' if not cf_invalidation_failed else '⚠️ invalidation failed'}\n"
        + (f"⚠️ Failed: {len(failed)} files" if failed else "")
        + cleanup_summary
    )

    send_trust_auto_approve_notification(
        command=synthetic_command,
        trust_id=trust_id,
        remaining=remaining_str,
        count=new_count,
        result=result_summary,
        source=source,
        reason=reason,
    )

    # 10. Cleanup staging files
    for staged_key in staged_keys:
        try:
            s3_staging.delete_object(Bucket=staging_bucket, Key=staged_key)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("Trust deploy cleanup skipped", extra={"src_module": "deploy_frontend", "operation": "trust_deploy_cleanup", "s3_key": staged_key}, exc_info=True)

    # 11. Return result
    status = "success" if not failed and not cf_invalidation_failed else "partial_success" if deployed else "error"

    return mcp_result(req_id, {
        "content": [{"type": "text", "text": json.dumps({
            "status": status,
            "request_id": request_id,
            "deployed": len(deployed),
            "failed": len(failed),
            "frontend_bucket": frontend_bucket,
            "distribution_id": distribution_id,
            "cloudfront_invalidation": "success" if not cf_invalidation_failed else "failed",
            "trust_session": trust_id,
            "message": f"Trust session: deployed {len(deployed)}/{len(processed_files)} files",
        })}],
        "isError": bool(failed and not deployed),
    })


# ---------------------------------------------------------------------------
# Main tool entry point
# ---------------------------------------------------------------------------

def mcp_tool_deploy_frontend(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_frontend

    ⚠️ DEPRECATED since Sprint 38 — Use bouncer_request_frontend_presigned + bouncer_confirm_frontend_deploy instead.
    This function is retained for backward compatibility with existing DDB callback records (action=deploy_frontend).

    Phase A: validate -> check trust -> route to approval or execution.
    """

    # 1. Extract and validate arguments
    files = arguments.get("files", [])
    project = str(arguments.get("project", "")).strip()
    reason = str(arguments.get("reason", "No reason provided")).strip()
    source = arguments.get("source", None)
    trust_scope = str(arguments.get("trust_scope", "")).strip()
    account_id = str(arguments.get("account_id", DEFAULT_ACCOUNT_ID)).strip()

    # 2. Validate project
    if not project:
        return mcp_result(req_id, {
            "content": [{"type": "text", "text": json.dumps({
                "status": "error",
                "error": "project is required",
            })}],
            "isError": True,
        })

    project_config = _get_project_config(project)
    if not project_config:
        available = _list_known_projects()
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

    # 4. Check trust session
    trust_ok, trust_session, trust_reason = _check_deploy_trust(
        trust_scope, project, account_id, source or ""
    )
    if trust_ok:
        # Trust allows auto-approval: execute directly
        return _execute_deploy_frontend_approved(
            req_id, files, project, project_config, reason, source,
            trust_session, account_id, trust_scope
        )

    # 5. No trust: submit for manual approval
    return _submit_deploy_frontend_approval(
        req_id, files, project, project_config, reason, source, trust_scope
    )


# ---------------------------------------------------------------------------
# Presigned URL Flow - Step 1: Request presigned URLs
# ---------------------------------------------------------------------------

def mcp_tool_request_frontend_presigned(req_id: str, arguments: dict) -> dict:
    """Step 1 of presigned deploy: validate metadata, return presigned PUT URLs.
    No DDB write, no Telegram notification, no approval needed.
    Agent uploads files directly to S3, then calls bouncer_confirm_frontend_deploy.
    """
    files = arguments.get("files", [])  # [{filename, content_type}] — NO content
    project = str(arguments.get("project", "")).strip()
    _source = arguments.get("source", None)  # noqa: F841 — reserved for future use
    _trust_scope = str(arguments.get("trust_scope", "")).strip()  # noqa: F841 — reserved for future use
    account_id = str(arguments.get("account_id", DEFAULT_ACCOUNT_ID)).strip()

    if not project:
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": "project is required"})}], "isError": True})

    project_config = _get_project_config(project)
    if not project_config:
        available = _list_known_projects()
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Unknown project: {project}", "available_projects": available})}], "isError": True})

    if not files:
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": "files is required"})}], "isError": True})

    # Validate file metadata only (no base64 content to decode)
    filenames = set()
    for fm in files:
        fname = fm.get("filename", "")
        if not fname:
            return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": "filename is required for each file"})}], "isError": True})
        if _has_blocked_extension(fname):
            return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Blocked file extension: {fname}"})}], "isError": True})
        if fname in filenames:
            return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Duplicate filename: {fname}"})}], "isError": True})
        filenames.add(fname)

    # Generate request_id for staging key grouping
    request_id = generate_request_id(f"fepre:{project}")
    staging_bucket = f"bouncer-uploads-{account_id}"
    expires_in = 300  # 5 minutes

    presigned_urls = []
    s3 = get_s3_client()

    for fm in files:
        fname = fm.get("filename", "")
        content_type = _get_content_type(fname, fm.get("content_type"))
        s3_key = f"frontend/{project}/{request_id}/{fname}"

        try:
            url = s3.generate_presigned_url(
                "put_object",
                Params={"Bucket": staging_bucket, "Key": s3_key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )
        except Exception as exc:  # noqa: BLE001
            return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Failed to generate presigned URL for {fname}: {exc}"})}], "isError": True})

        presigned_urls.append({
            "filename": fname,
            "presigned_url": url,
            "s3_key": s3_key,
            "content_type": content_type,
        })

    import datetime
    expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({
        "status": "ready",
        "request_id": request_id,
        "presigned_urls": presigned_urls,
        "expires_at": expires_at,
        "staging_bucket": staging_bucket,
        "instructions": "Upload each file via HTTP PUT to its presigned_url, then call bouncer_confirm_frontend_deploy with request_id and files metadata.",
    })}]})


# ---------------------------------------------------------------------------
# Presigned URL Flow - Step 2: Confirm deployment
# ---------------------------------------------------------------------------

def _finalize_auto_approved_deploy(table, request_id: str, project: str, reason: str, source: str,
                                    staging_bucket: str, frontend_bucket: str, distribution_id: str,
                                    files_manifest: list, deployed: list, failed: list, total_bytes: int,
                                    cf_invalidation_failed: bool, deploy_status: str):
    """Finalize auto-approved deploy: update DDB, emit metrics, write history, send silent notification."""
    success_count = len(deployed)
    fail_count = len(failed)

    # Update DDB with deploy results
    extra_attrs = {
        'deploy_status': deploy_status,
        'deployed_count': success_count,
        'failed_count': fail_count,
        'deployed_files': json.dumps([d['filename'] for d in deployed]),
        'failed_files': json.dumps([f['filename'] for f in failed]),
        'deployed_details': json.dumps(deployed),
        'failed_details': json.dumps(failed),
        'cf_invalidation_failed': cf_invalidation_failed,
        'total_size': total_bytes,
    }

    try:
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #st = :st, deploy_status = :ds, deployed_count = :dc, failed_count = :fc, '
                           'deployed_files = :df, failed_files = :ff, deployed_details = :dd, '
                           'failed_details = :fd, cf_invalidation_failed = :cf, total_size = :ts',
            ExpressionAttributeNames={'#st': 'status'},
            ExpressionAttributeValues={
                ':st': 'auto_approved',
                ':ds': deploy_status,
                ':dc': success_count,
                ':fc': fail_count,
                ':df': extra_attrs['deployed_files'],
                ':ff': extra_attrs['failed_files'],
                ':dd': extra_attrs['deployed_details'],
                ':fd': extra_attrs['failed_details'],
                ':cf': cf_invalidation_failed,
                ':ts': total_bytes,
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to update DDB for auto-approved deploy %s: %s", request_id, e, extra={
            "src_module": "deploy_frontend", "operation": "auto_approve_finalize",
            "request_id": request_id, "error": str(e)
        })

    # Emit metrics
    emit_metric('Bouncer', 'DeployFrontend', 1, dimensions={'Status': deploy_status, 'Project': project})

    # Write to deploy_history table
    try:
        history_item = {
            'request_id': request_id,
            'timestamp': int(time.time()),
            'action': 'deploy_frontend',
            'project': project,
            'deploy_status': deploy_status,
            'source': source or 'unknown',
            'reason': reason,
            'file_count': len(files_manifest),
            'success_count': success_count,
            'fail_count': fail_count,
            'frontend_bucket': frontend_bucket,
            'distribution_id': distribution_id,
            'cf_invalidation_failed': cf_invalidation_failed,
            'ttl': int(time.time()) + TTL_30_DAYS,
        }
        deployer_history_table.put_item(Item=history_item)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to write deploy history for %s: %s", request_id, e, extra={
            "src_module": "deploy_frontend", "operation": "auto_approve_history",
            "request_id": request_id, "error": str(e)
        })

    # Send silent notification (informational, not approval request)
    size_str = format_size_human(total_bytes)
    cf_warn = "\n⚠️ *CloudFront Invalidation 失敗* (S3 已完成)" if cf_invalidation_failed else ""
    fail_line = f"\n❗ 失敗: {fail_count} 個" if fail_count > 0 else ""

    if deploy_status == 'deploy_failed':
        status_emoji = '❌'
        title = '前端部署自動批准並執行（失敗）'
    elif deploy_status == 'partial_deploy':
        status_emoji = '⚠️'
        title = '前端部署自動批准並執行（部分成功）'
    else:
        status_emoji = '✅'
        title = '前端部署自動批准並執行'

    notification_text = (
        f"{status_emoji} *{title}*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"📦 *專案：* {project}\n"
        f"📄 成功: {success_count}/{len(files_manifest)} 個檔案 ({size_str})"
        f"{fail_line}\n"
        f"💬 *原因：* {reason}\n"
        f"📡 *來源：* {source or 'unknown'}"
        f"{cf_warn}"
    )

    try:
        send_telegram_message_silent(notification_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to send silent notification for auto-approved deploy %s: %s", request_id, e, extra={
            "src_module": "deploy_frontend", "operation": "auto_approve_notify",
            "request_id": request_id, "error": str(e)
        })


def mcp_tool_confirm_frontend_deploy(req_id: str, arguments: dict) -> dict:
    """Step 2 of presigned deploy: verify uploads, create approval request.
    Verifies all files exist in staging via head_object, then creates DDB approval record.
    """
    request_id = str(arguments.get("request_id", "")).strip()
    files = arguments.get("files", [])  # [{filename, content_type, cache_control}]
    project = str(arguments.get("project", "")).strip()
    reason = str(arguments.get("reason", "No reason provided")).strip()
    source = arguments.get("source", None)
    _trust_scope = str(arguments.get("trust_scope", "")).strip()  # noqa: F841 — reserved for future use
    account_id = str(arguments.get("account_id", DEFAULT_ACCOUNT_ID)).strip()

    if not request_id or not project or not files:
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": "request_id, project, and files are required"})}], "isError": True})

    project_config = _get_project_config(project)
    if not project_config:
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Unknown project: {project}"})}], "isError": True})

    frontend_config = _get_frontend_config(project)
    if not frontend_config:
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"No frontend config for project: {project}"})}], "isError": True})

    staging_bucket = f"bouncer-uploads-{account_id}"
    s3 = get_s3_client()

    # Verify all files exist in staging
    missing = []
    files_manifest = []
    for fm in files:
        fname = fm.get("filename", "")
        s3_key = f"frontend/{project}/{request_id}/{fname}"
        try:
            head = s3.head_object(Bucket=staging_bucket, Key=s3_key)
            size = head.get("ContentLength", 0)
        except Exception:  # noqa: BLE001
            missing.append(fname)
            continue

        content_type = _get_content_type(fname, fm.get("content_type"))
        cache_control = _get_cache_control(fname)
        files_manifest.append({
            "filename": fname,
            "s3_key": s3_key,
            "content_type": content_type,
            "cache_control": cache_control,
            "size": size,
        })

    if missing:
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Missing files in staging: {missing}. Upload them first using presigned URLs."})}], "isError": True})

    # Auto-approve frontend deploy when conditions are met:
    # 1. All files verified in staging (already checked above)
    # 2. Source is from a known bot (not None/unknown)
    # 3. File count is reasonable (< 500 files)
    FRONTEND_AUTO_APPROVE_ENABLED = os.environ.get('FRONTEND_AUTO_APPROVE', 'true').lower() == 'true'

    should_auto_approve = (
        FRONTEND_AUTO_APPROVE_ENABLED
        and source  # source must be provided (not None or empty)
        and source != 'unknown'
        and len(files_manifest) < 500  # sanity check
    )

    # Build approval record (same structure as _submit_deploy_frontend_approval)
    now = int(time.time())
    confirm_request_id = generate_request_id(f"deploy_frontend:{project}")

    deploy_role_arn = frontend_config.get("deploy_role_arn") or project_config.get("deploy_role_arn")

    item = {
        "request_id": confirm_request_id,
        "action": "deploy_frontend",
        "status": "pending_approval",
        "project": project,
        "reason": reason,
        "source": source or "unknown",
        "mode": "mcp",
        "files": json.dumps(files_manifest),
        "frontend_bucket": frontend_config["frontend_bucket"],
        "distribution_id": frontend_config["distribution_id"],
        "region": frontend_config.get("region", DEFAULT_REGION),
        "deploy_role_arn": deploy_role_arn,
        "staging_bucket": staging_bucket,
        "file_count": len(files_manifest),
        "created_at": now,
        "ttl": now + APPROVAL_TIMEOUT_DEFAULT + APPROVAL_TTL_BUFFER,
    }

    # Execute auto-approve flow if conditions are met
    if should_auto_approve:
        logger.info("Frontend deploy auto-approve triggered", extra={
            "src_module": "deploy_frontend", "operation": "auto_approve",
            "request_id": confirm_request_id, "project": project,
            "file_count": len(files_manifest), "source": source
        })

        # Update item status to auto_approved
        item["status"] = "auto_approved"

        try:
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Failed to create approval record: {exc}"})}], "isError": True})

        # Execute deploy inline (replicate callback approve logic)
        deployed = []
        failed = []
        total_bytes = 0
        cf_invalidation_failed = False

        # 1. Assume deploy role
        s3_staging = get_s3_client()
        if deploy_role_arn:
            try:
                s3_target = get_s3_client(role_arn=deploy_role_arn, session_name=f"bouncer-deploy-{confirm_request_id[:16]}")
            except ClientError as e:
                logger.exception("AssumeRole failed for %s: %s", deploy_role_arn, e, extra={
                    "src_module": "deploy_frontend", "operation": "auto_approve_assume_role",
                    "deploy_role_arn": deploy_role_arn, "error": str(e)
                })
                failed = [{'filename': fm.get('filename', 'unknown'), 'reason': f'AssumeRole failed: {e}'} for fm in files_manifest]
                deploy_status = 'deploy_failed'
                _finalize_auto_approved_deploy(
                    table, confirm_request_id, project, reason, source, staging_bucket,
                    frontend_config["frontend_bucket"], frontend_config["distribution_id"],
                    files_manifest, deployed, failed, total_bytes, cf_invalidation_failed, deploy_status
                )
                return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({
                    "status": "auto_approved",
                    "request_id": confirm_request_id,
                    "deploy_status": deploy_status,
                    "error": f"AssumeRole failed: {e}"
                })}], "isError": True})
        else:
            s3_target = s3_staging

        # 2. Deploy files to frontend bucket
        for fm in files_manifest:
            filename = fm.get('filename', 'unknown')
            staged_key = fm.get('s3_key', '')
            content_type = fm.get('content_type', 'application/octet-stream')
            cache_control = fm.get('cache_control', 'no-cache')

            try:
                # Read from staging
                obj = s3_staging.get_object(Bucket=staging_bucket, Key=staged_key)
                body = obj['Body'].read()

                # Write to frontend
                s3_target.put_object(
                    Bucket=frontend_config["frontend_bucket"],
                    Key=filename,
                    Body=body,
                    ContentType=content_type,
                    CacheControl=cache_control,
                )
                total_bytes += len(body)
                deployed.append({'filename': filename, 's3_key': filename})
                logger.info("uploaded file=%s size=%d content_type=%s request_id=%s project=%s", filename, len(body), content_type, confirm_request_id, project, extra={
                    "src_module": "deploy_frontend", "operation": "auto_approve_upload",
                    "file_name": filename, "request_id": confirm_request_id, "project": project
                })
            except Exception as e:  # noqa: BLE001
                logger.exception("upload_failed file=%s error=%s request_id=%s project=%s", filename, str(e)[:200], confirm_request_id, project, extra={
                    "src_module": "deploy_frontend", "operation": "auto_approve_upload",
                    "file_name": filename, "request_id": confirm_request_id, "project": project, "error": str(e)[:200]
                })
                failed.append({'filename': filename, 'reason': str(e)[:200]})

        # 3. CloudFront invalidation
        if len(deployed) > 0:
            try:
                cf = get_cloudfront_client(role_arn=deploy_role_arn)
                cf.create_invalidation(
                    DistributionId=frontend_config["distribution_id"],
                    InvalidationBatch={
                        'Paths': {'Quantity': 1, 'Items': ['/*']},
                        'CallerReference': confirm_request_id,
                    },
                )
            except ClientError as e:
                logger.exception("CloudFront invalidation failed for dist=%s: %s", frontend_config["distribution_id"], e, extra={
                    "src_module": "deploy_frontend", "operation": "auto_approve_cloudfront",
                    "distribution_id": frontend_config["distribution_id"], "error": str(e)
                })
                cf_invalidation_failed = True

        # 4. Determine deploy status
        success_count = len(deployed)
        fail_count = len(failed)
        if success_count == 0:
            deploy_status = 'deploy_failed'
        elif fail_count == 0:
            deploy_status = 'deployed'
        else:
            deploy_status = 'partial_deploy'

        # 5. Finalize: update DDB, metrics, history, notification
        _finalize_auto_approved_deploy(
            table, confirm_request_id, project, reason, source, staging_bucket,
            frontend_config["frontend_bucket"], frontend_config["distribution_id"],
            files_manifest, deployed, failed, total_bytes, cf_invalidation_failed, deploy_status
        )

        # Return success response
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({
            "status": "auto_approved",
            "request_id": confirm_request_id,
            "deploy_status": deploy_status,
            "file_count": len(files_manifest),
            "deployed_count": success_count,
            "failed_count": fail_count,
            "message": f"Frontend deploy auto-approved and executed. Status: {deploy_status}"
        })}]})

    # Manual approval path (original flow)
    try:
        table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Failed to create approval record: {exc}"})}], "isError": True})

    # Send Telegram notification (reuse existing function)
    try:
        send_deploy_frontend_notification(
            request_id=confirm_request_id,
            files_summary=files_manifest,
            target_info={
                "frontend_bucket": frontend_config["frontend_bucket"],
                "distribution_id": frontend_config["distribution_id"],
                "region": frontend_config.get("region", DEFAULT_REGION),
            },
            project=project,
            reason=reason,
            source=source or "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: approval record already exists, user can still approve
        logger.warning("Failed to send notification for %s: %s", confirm_request_id, exc,
                       extra={"src_module": "deploy_frontend", "operation": "confirm_notify", "request_id": confirm_request_id})
        table.delete_item(Key={"request_id": confirm_request_id})
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Failed to send notification: {exc}"})}], "isError": True})

    return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({
        "status": "pending_approval",
        "request_id": confirm_request_id,
        "file_count": len(files_manifest),
        "message": "Deploy approval request sent. Use bouncer_status to check.",
    })}]})
