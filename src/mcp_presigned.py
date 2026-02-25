"""
Bouncer - Presigned Upload Module

mcp_tool_request_presigned(): Generate a presigned S3 PUT URL for direct upload.
No approval required for staging bucket (bouncer-uploads-{DEFAULT_ACCOUNT_ID}).
"""

import json
import re
import time
import boto3

from utils import mcp_result, generate_request_id
from db import table
from constants import DEFAULT_ACCOUNT_ID, AUDIT_TTL_LONG

# Maximum allowed expires_in (seconds)
PRESIGNED_MAX_EXPIRES_IN = 3600
PRESIGNED_DEFAULT_EXPIRES_IN = 900


# =============================================================================
# Filename Sanitization
# =============================================================================

def _sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal attacks.

    Preserves subdirectory structure (e.g. assets/foo.js) but strips
    dangerous components like '..' and absolute path prefixes.
    """
    if not filename:
        return 'unnamed'

    # Remove null bytes
    filename = filename.replace('\x00', '')
    # Normalize backslashes
    filename = filename.replace('\\', '/')
    # Remove path traversal components
    parts = []
    for part in filename.split('/'):
        # Skip empty parts, current dir, and parent dir traversal
        if part in ('', '.', '..'):
            continue
        # Remove special characters except .-_
        safe_part = re.sub(r'[^\w\-.]', '_', part)
        safe_part = safe_part.lstrip('. ')
        if safe_part:
            parts.append(safe_part)

    result = '/'.join(parts)
    return result or 'unnamed'


# =============================================================================
# MCP Tool: bouncer_request_presigned
# =============================================================================

def mcp_tool_request_presigned(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_request_presigned

    Generate a presigned S3 PUT URL for direct client upload.
    No Telegram approval required (staging bucket only).
    """
    # ---- Parse and validate required arguments ----
    filename = str(arguments.get('filename', '')).strip()
    content_type = str(arguments.get('content_type', '')).strip()
    reason = str(arguments.get('reason', '')).strip()
    source = str(arguments.get('source', '')).strip()
    account = arguments.get('account', None)
    expires_in = arguments.get('expires_in', PRESIGNED_DEFAULT_EXPIRES_IN)

    # Validate required fields
    if not filename:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'filename is required',
            })}],
            'isError': True,
        })

    if not content_type:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'content_type is required',
            })}],
            'isError': True,
        })

    if not reason:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'reason is required',
            })}],
            'isError': True,
        })

    if not source:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'source is required',
            })}],
            'isError': True,
        })

    # Validate expires_in
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'expires_in must be an integer',
            })}],
            'isError': True,
        })

    if expires_in > PRESIGNED_MAX_EXPIRES_IN:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'expires_in exceeds maximum of {PRESIGNED_MAX_EXPIRES_IN} seconds',
            })}],
            'isError': True,
        })

    if expires_in <= 0:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'expires_in must be a positive integer',
            })}],
            'isError': True,
        })

    # ---- Sanitize filename ----
    safe_filename = _sanitize_filename(filename)

    # ---- Resolve target account (staging bucket only) ----
    target_account_id = DEFAULT_ACCOUNT_ID
    if account:
        target_account_id = str(account).strip()

    bucket = f"bouncer-uploads-{target_account_id}"

    # ---- Generate request ID and S3 key ----
    request_id = generate_request_id(f"presigned:{safe_filename}")
    date_str = time.strftime('%Y-%m-%d')
    s3_key = f"{date_str}/{request_id}/{safe_filename}"
    s3_uri = f"s3://{bucket}/{s3_key}"

    # ---- Calculate expiry timestamp ----
    now = int(time.time())
    expires_at_ts = now + expires_in
    expires_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires_at_ts))

    # ---- Generate presigned URL ----
    try:
        s3_client = boto3.client('s3')
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': bucket,
                'Key': s3_key,
                'ContentType': content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod='PUT',
        )
    except Exception as e:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'Failed to generate presigned URL: {str(e)}',
            })}],
            'isError': True,
        })

    # ---- Write DynamoDB audit record ----
    try:
        table.put_item(Item={
            'request_id': request_id,
            'action': 'presigned_upload',
            'status': 'url_issued',
            'filename': safe_filename,
            's3_key': s3_key,
            'bucket': bucket,
            'content_type': content_type,
            'source': source,
            'reason': reason,
            'expires_at': expires_at_ts,
            'created_at': now,
            'ttl': now + AUDIT_TTL_LONG,
        })
    except Exception as e:
        # Audit failure should not block the caller — log and continue
        print(f"[PRESIGNED] DynamoDB audit write failed for {request_id}: {e}")

    # ---- Return presigned URL details ----
    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'status': 'ready',
                'presigned_url': presigned_url,
                's3_key': s3_key,
                's3_uri': s3_uri,
                'request_id': request_id,
                'expires_at': expires_at,
                'method': 'PUT',
                'headers': {
                    'Content-Type': content_type,
                },
            }),
        }],
    })
