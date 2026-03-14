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

from constants import DEFAULT_ACCOUNT_ID, APPROVAL_TTL_BUFFER, UPLOAD_TIMEOUT
from db import table, deployer_projects_table, deployer_history_table
from notifications import send_deploy_frontend_notification
from utils import generate_request_id, mcp_result

logger = Logger(service="bouncer")

# ---------------------------------------------------------------------------
# Project Config (DynamoDB-only; no hardcoded fallback)
# ---------------------------------------------------------------------------
# All project configs must be stored in the DynamoDB 'bouncer-projects' table.
# To add a new frontend project, run:
#   python3 scripts/seed_frontend_configs.py
# See SKILL.md > "Adding a New Frontend Project" for full instructions.
# ---------------------------------------------------------------------------

# Environment variable for projects table (same as deployer.py)
_PROJECTS_TABLE = os.environ.get('PROJECTS_TABLE', 'bouncer-projects')
_REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')

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
        projects_table = dynamodb.Table(_PROJECTS_TABLE)
        resp = projects_table.get_item(Key={'project_id': project_id})
        item = resp.get('Item')

        if not item:
            return None

        # Map DDB item fields -> canonical config keys
        frontend_bucket = item.get('frontend_bucket')
        distribution_id = item.get('frontend_distribution_id')
        region = item.get('frontend_region', 'us-east-1')
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
        projects_table = dynamodb.Table(_PROJECTS_TABLE)
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

def _check_deploy_trust(trust_scope: str, project: str, account_id: str, source: str) -> tuple:
    """Check if trust_scope allows auto-approval for frontend deployment.

    Returns (should_trust: bool, trust_session: dict or None, reason: str)
    """
    from trust import should_trust_approve

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
                    logger.warning("Rollback cleanup failed (non-critical)", extra={"src_module": "deploy_frontend", "operation": "rollback_cleanup", "s3_key": rk})
            return mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "error",
                    "error": f"Failed to stage {pf['filename']} to S3: {exc}",
                })}],
                "isError": True,
            })

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
        "region": project_config.get("region", "us-east-1"),
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
        except ClientError as del_err:
            logger.error("DDB cleanup failed for %s: %s", request_id, del_err, extra={"src_module": "deploy_frontend", "operation": "submit_deploy_frontend", "request_id": request_id, "error": str(del_err)})
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
    from trust import increment_trust_command_count
    from notifications import send_trust_auto_approve_notification
    from utils import log_decision
    from aws_clients import get_cloudfront_client

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
        logger.error("[DEPLOY-FRONTEND] Trust deploy AssumeRole failed for %s: %s", deploy_role_arn, exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_assume_role", "role_arn": deploy_role_arn, "error": str(exc)})
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
            logger.error("Trust deploy failed for %s: %s", filename, exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_file", "file_name": filename, "error": str(exc)})
            failed.append({"filename": filename, "reason": str(exc)})

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
            logger.error("[DEPLOY-FRONTEND] Trust deploy CloudFront invalidation failed: %s", exc, extra={"src_module": "deploy_frontend", "operation": "cloudfront_invalidation", "distribution_id": distribution_id})
            cf_invalidation_failed = True

    # 6. Increment trust command count
    trust_id = trust_session.get("request_id", "")
    new_count = increment_trust_command_count(trust_id)

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
            "ttl": now + 30 * 24 * 3600,  # 30 days
        }
        deployer_history_table.put_item(Item=history_item)
    except ClientError as exc:
        logger.error("[DEPLOY-FRONTEND] Trust deploy history write failed for %s: %s", request_id, exc, extra={"src_module": "deploy_frontend", "operation": "trust_deploy_history_write", "request_id": request_id, "error": str(exc)})
    # 9. Send silent trust notification
    remaining = int(trust_session.get("expires_at", 0)) - now
    remaining_str = f"{remaining // 60}:{remaining % 60:02d}" if remaining > 0 else "0:00"

    result_summary = (
        f"✅ Deployed {len(deployed)} files to {frontend_bucket}\n"
        f"CloudFront: {'✅' if not cf_invalidation_failed else '⚠️ invalidation failed'}\n"
        + (f"⚠️ Failed: {len(failed)} files" if failed else "")
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

    # Build approval record (same structure as _submit_deploy_frontend_approval)
    now = int(time.time())
    confirm_request_id = generate_request_id(f"deploy_frontend:{project}")

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
        "region": frontend_config.get("region", "us-east-1"),
        "deploy_role_arn": frontend_config.get("deploy_role_arn") or project_config.get("deploy_role_arn"),
        "staging_bucket": staging_bucket,
        "file_count": len(files_manifest),
        "created_at": now,
        "ttl": now + APPROVAL_TTL_BUFFER,
    }

    try:
        table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        return mcp_result(req_id, {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": f"Failed to create approval record: {exc}"})}], "isError": True})

    # Send Telegram notification (reuse existing function)
    try:
        send_deploy_frontend_notification(
            request_id=confirm_request_id,
            project=project,
            file_count=len(files_manifest),
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
