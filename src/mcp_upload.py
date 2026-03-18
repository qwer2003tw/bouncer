"""
Bouncer - Upload Pipeline

UploadContext + upload step functions + mcp_tool_upload() + mcp_tool_upload_batch()
Also includes _format_size_human().
"""

import binascii
import json
import time
from aws_clients import get_s3_client
from dataclasses import dataclass
from typing import Optional

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from utils import mcp_result, generate_request_id, generate_display_summary, sanitize_filename
from accounts import (
    init_default_account, get_account, list_accounts,
    validate_account_id,
)
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit
from trust import (
    should_trust_approve_upload, increment_trust_upload_count,
)
import telegram as _telegram
from telegram import escape_markdown
from db import table
from notifications import (
    send_trust_upload_notification,
    send_batch_upload_notification,
)
from constants import (

    DEFAULT_ACCOUNT_ID,
    TRUST_SESSION_MAX_UPLOADS,
    TRUST_UPLOAD_MAX_BYTES_PER_FILE, TRUST_UPLOAD_MAX_BYTES_TOTAL,
    UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT, UPLOAD_BATCH_PER_FILE_B64_LIMIT,
    APPROVAL_TTL_BUFFER, UPLOAD_TIMEOUT,
)
from upload_scanner import scan_upload

logger = Logger(service="bouncer")


# =============================================================================
# S3 Upload Verification
# =============================================================================

@dataclass
class UploadVerificationResult:
    """Result of a single S3 upload verification via head_object."""
    filename: str
    s3_uri: str
    verified: bool
    s3_size: Optional[int] = None   # None when verification failed
    error: Optional[str] = None      # populated only on failure


def _verify_upload(s3_client, bucket: str, key: str, filename: str) -> UploadVerificationResult:
    """Call head_object to verify an uploaded file exists in S3.

    Never raises — failures are captured in UploadVerificationResult.verified=False
    and logged as warnings.
    """
    s3_uri = f"s3://{bucket}/{key}"
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        s3_size = int(head.get('ContentLength', 0) or 0)
        return UploadVerificationResult(
            filename=filename,
            s3_uri=s3_uri,
            verified=True,
            s3_size=s3_size,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "head_object failed for %s: %s",
            s3_uri,
            exc,
            extra={"src_module": "upload", "operation": "verify_upload", "s3_uri": s3_uri, "error": str(exc)},
        )
        return UploadVerificationResult(
            filename=filename,
            s3_uri=s3_uri,
            verified=False,
            s3_size=None,
            error=str(exc),
        )


# =============================================================================
# Upload Pipeline — Context + Step Functions
# =============================================================================

@dataclass
class UploadContext:
    """Pipeline context for mcp_tool_upload"""
    req_id: str
    filename: str
    content_b64: str
    content_type: str
    content_size: int
    reason: str
    source: Optional[str]
    sync_mode: bool
    legacy_bucket: Optional[str]
    legacy_key: Optional[str]
    account_id: str
    account_name: str
    assume_role: Optional[str]
    target_account_id: str
    trust_scope: str = ''
    bucket: str = ''
    key: str = ''
    request_id: str = ''


def _parse_upload_request(req_id, arguments: dict) -> 'dict | UploadContext':
    """Parse and validate upload request arguments.

    Returns an UploadContext on success, or an MCP response dict on failure.
    """
    import base64

    filename = str(arguments.get('filename', '')).strip()
    content_b64 = str(arguments.get('content', '')).strip()
    content_type = str(arguments.get('content_type', 'application/octet-stream')).strip()
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    trust_scope = str(arguments.get('trust_scope', '')).strip()
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()
    sync_mode = arguments.get('sync', False)

    legacy_bucket = arguments.get('bucket', None)
    legacy_key = arguments.get('key', None)

    # 驗證必要參數
    if not filename and not legacy_key:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': 'filename is required'})}],
            'isError': True
        })
    if not content_b64:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': 'content is required'})}],
            'isError': True
        })

    # 驗證 base64 padding（截斷檢測）
    if len(content_b64) % 4 != 0:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': (
                'Invalid base64 content: length is not a multiple of 4. '
                'This usually means the content was truncated by OS argument length limits. '
                'Use the HTTP API directly or bouncer_request_presigned for large files.'
            )})}],
            'isError': True
        })

    # 解碼 base64 驗證格式
    try:
        content_bytes = base64.b64decode(content_b64)
        content_size = len(content_bytes)
    except (binascii.Error, ValueError) as e:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': f'Invalid base64 content: {str(e)}'})}],
            'isError': True
        })

    # 檢查大小（4.5 MB 限制）
    max_size = 4.5 * 1024 * 1024
    if content_size > max_size:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'Content too large: {content_size} bytes (max {int(max_size)} bytes)'
            })}],
            'isError': True
        })

    # 解析帳號
    assume_role = None
    account_name = 'Default'
    target_account_id = DEFAULT_ACCOUNT_ID

    if not account_id and DEFAULT_ACCOUNT_ID:
        default_account = get_account(DEFAULT_ACCOUNT_ID)
        if default_account:
            assume_role = default_account.get('role_arn')
            account_name = default_account.get('name', 'Default')

    if account_id:
        valid, error = validate_account_id(account_id)
        if not valid:
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
                'isError': True
            })

        account = get_account(account_id)
        if not account:
            available = [a['account_id'] for a in list_accounts()]
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'帳號 {account_id} 未配置',
                    'available_accounts': available
                })}],
                'isError': True
            })

        if not account.get('enabled', True):
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'帳號 {account_id} 已停用'
                })}],
                'isError': True
            })

        assume_role = account.get('role_arn')
        account_name = account.get('name', account_id)
        target_account_id = account_id

    return UploadContext(
        req_id=req_id,
        filename=filename,
        content_b64=content_b64,
        content_type=content_type,
        content_size=content_size,
        reason=reason,
        source=source,
        sync_mode=sync_mode,
        legacy_bucket=legacy_bucket,
        legacy_key=legacy_key,
        account_id=account_id or DEFAULT_ACCOUNT_ID,
        account_name=account_name,
        assume_role=assume_role,
        target_account_id=target_account_id,
        trust_scope=trust_scope,
    )


def _resolve_upload_target(ctx: UploadContext) -> None:
    """Determine bucket, key, and request_id.  Mutates ctx in-place."""
    if ctx.legacy_bucket and ctx.legacy_key:
        ctx.bucket = ctx.legacy_bucket
        ctx.key = ctx.legacy_key
    else:
        ctx.bucket = f"bouncer-uploads-{ctx.target_account_id}"
        date_str = time.strftime('%Y-%m-%d')
        ctx.request_id = generate_request_id(f"upload:{ctx.filename}")
        ctx.key = f"{date_str}/{ctx.request_id}/{ctx.filename or ctx.legacy_key}"


def _check_upload_rate_limit(ctx: UploadContext) -> Optional[dict]:
    """Rate limit check for uploads."""
    if not ctx.source:
        return None
    try:
        check_rate_limit(ctx.source)
    except RateLimitExceeded as e:
        return mcp_result(ctx.req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
            'isError': True
        })
    except PendingLimitExceeded as e:
        return mcp_result(ctx.req_id, {
            'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': str(e)})}],
            'isError': True
        })
    return None


def _check_upload_trust(ctx: UploadContext) -> Optional[dict]:
    """Trust session auto-approve for uploads — execute if trusted.

    Fallthrough design: returns None on any mismatch/error so the pipeline
    continues to the next layer (human approval).
    """
    import base64
    import hashlib as _hashlib

    # No trust_scope → pass through
    if not ctx.trust_scope:
        return None

    # Custom s3_uri (legacy bucket/key) → don't trust
    if ctx.legacy_bucket or ctx.legacy_key:
        return None

    # Check trust
    should_approve, trust_session, reason = should_trust_approve_upload(
        ctx.trust_scope, ctx.account_id, ctx.filename, ctx.content_size,
    )
    if not should_approve or not trust_session:
        return None

    # Atomic increment (prevent race condition)
    success = increment_trust_upload_count(
        trust_session['request_id'], ctx.content_size,
    )
    if not success:
        return None  # quota race → fall through

    # Execute upload
    try:
        content_bytes = base64.b64decode(ctx.content_b64)
        sha256_hash = _hashlib.sha256(content_bytes).hexdigest()

        # Upload to S3 (Sprint 58 s58-003: use top-level import)
        s3 = get_s3_client(role_arn=ctx.assume_role, session_name='bouncer-trust-upload')

        s3.put_object(
            Bucket=ctx.bucket,
            Key=ctx.key,
            Body=content_bytes,
            ContentType=ctx.content_type,
        )

        s3_uri = f"s3://{ctx.bucket}/{ctx.key}"

        # Compute remaining quota
        upload_count = int(trust_session.get('upload_count', 0)) + 1
        max_uploads = int(trust_session.get('max_uploads', TRUST_SESSION_MAX_UPLOADS))

        # Send silent notification
        send_trust_upload_notification(
            filename=ctx.filename,
            content_size=ctx.content_size,
            sha256_hash=sha256_hash,
            trust_id=trust_session['request_id'],
            upload_count=upload_count,
            max_uploads=max_uploads,
            source=ctx.source,
        )

        return mcp_result(ctx.req_id, {
            'content': [{
                'type': 'text',
                'text': json.dumps({
                    'status': 'trust_auto_approved',
                    's3_uri': s3_uri,
                    'filename': ctx.filename,
                    'size': ctx.content_size,
                    'sha256': sha256_hash,
                    'trust_session': trust_session['request_id'],
                    'upload_quota': f"{upload_count}/{max_uploads}",
                })
            }]
        })

    except ClientError as e:
        logger.error("Trust upload execution error: %s", e, extra={"src_module": "upload", "operation": "trust_upload", "error": str(e)})
        return None  # Fall through to human approval


def _submit_upload_for_approval(ctx: UploadContext) -> dict:
    """Submit upload for human approval — always returns a result."""
    import base64 as _base64

    # 固定桶模式在 _resolve_upload_target 時 request_id 尚未設定
    if ctx.legacy_bucket and ctx.legacy_key:
        ctx.request_id = generate_request_id(f"upload:{ctx.bucket}:{ctx.key}")
    ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER

    # 格式化大小顯示
    if ctx.content_size >= 1024 * 1024:
        size_str = f"{ctx.content_size / 1024 / 1024:.2f} MB"
    elif ctx.content_size >= 1024:
        size_str = f"{ctx.content_size / 1024:.2f} KB"
    else:
        size_str = f"{ctx.content_size} bytes"

    # Upload content to S3 staging (pending/) BEFORE writing to DDB
    # This avoids storing large base64 content directly in DynamoDB (400KB limit).
    # Staging bucket 固定用主帳號 bucket（Lambda IAM policy 只允許此 bucket）
    # target_account_id 是執行命令的帳號，staging 與之無關
    staging_bucket = f"bouncer-uploads-{DEFAULT_ACCOUNT_ID}"
    content_s3_key = f"pending/{ctx.request_id}/{ctx.filename or ctx.legacy_key or 'file'}"
    try:
        content_bytes = _base64.b64decode(ctx.content_b64)

        # Security scan before staging
        scan_result = scan_upload(ctx.filename, content_bytes, ctx.content_type)
        if scan_result.is_blocked:
            return mcp_result(ctx.req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'Upload rejected by security scan: {scan_result.summary}',
                    'scan_findings': scan_result.findings,
                }, ensure_ascii=False)}],
                'isError': True,
            })

        # Use assume_role credentials if available (e.g. BouncerRole has S3 access)
        # The Lambda execution role may not have direct S3 PutObject permissions.
        # Sprint 58 s58-003: use top-level import
        _s3 = get_s3_client(role_arn=ctx.assume_role, session_name='bouncer-upload-staging')
        _s3.put_object(
            Bucket=staging_bucket,
            Key=content_s3_key,
            Body=content_bytes,
            ContentType=ctx.content_type,
        )

        # Add scan warnings to context for approval notification
        if scan_result.risk_level in ('high', 'medium'):
            ctx.reason = f"[⚠️ 安全掃描警告: {scan_result.summary}] {ctx.reason}"

    except Exception as e:  # noqa: BLE001
        return mcp_result(ctx.req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'Failed to stage file to S3: {str(e)}',
            })}],
            'isError': True,
        })

    item = {
        'request_id': ctx.request_id,
        'action': 'upload',
        'bucket': ctx.bucket,
        'key': ctx.key,
        'content_s3_key': content_s3_key,   # S3 reference instead of raw base64
        'content_type': ctx.content_type,
        'content_size': ctx.content_size,
        'reason': ctx.reason,
        'source': ctx.source or '__anonymous__',
        'account_id': ctx.target_account_id,
        'account_name': ctx.account_name,
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp',
        'display_summary': generate_display_summary('upload', filename=ctx.filename, content_size=ctx.content_size),
    }
    if ctx.assume_role:
        item['assume_role'] = ctx.assume_role
    table.put_item(Item=item)

    # 發送 Telegram 審批
    # 若發送失敗，刪除剛寫入的 DynamoDB record，避免產生孤兒審批請求
    s3_uri = f"s3://{ctx.bucket}/{ctx.key}"

    safe_reason = escape_markdown(ctx.reason)
    safe_source = escape_markdown(ctx.source or 'Unknown')
    safe_content_type = escape_markdown(ctx.content_type)
    safe_account = escape_markdown(f"{ctx.target_account_id} ({ctx.account_name})")

    message = (
        f"📤 *上傳檔案請求*\n\n"
        f"🤖 *來源：* {safe_source}\n"
        f"🏦 *帳號：* {safe_account}\n"
        f"📁 *目標：* `{s3_uri}`\n"
        f"📊 *大小：* {size_str}\n"
        f"📝 *類型：* {safe_content_type}\n"
        f"💬 *原因：* {safe_reason}\n\n"
        f"🆔 *ID：* `{ctx.request_id}`\n"
        f"⏰ *{UPLOAD_TIMEOUT // 60} 分鐘後過期*"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准', 'callback_data': f'approve:{ctx.request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{ctx.request_id}'}
        ]]
    }

    try:
        tg_result = _telegram.send_telegram_message(message, keyboard)
        if not (tg_result and tg_result.get('ok')):
            raise RuntimeError("Telegram notification returned failure (ok=False or empty response)")
    except (OSError, TimeoutError, ConnectionError, RuntimeError) as tg_err:
        # Cleanup DDB to prevent orphan pending record
        try:
            table.delete_item(Key={'request_id': ctx.request_id})
        except ClientError as del_err:
            logger.error("Failed to delete DDB record %s: %s", ctx.request_id, del_err, extra={"src_module": "upload", "operation": "orphan_cleanup", "request_id": ctx.request_id, "error": str(del_err)})
        logger.error("Telegram notification failed for upload %s: %s", ctx.request_id, tg_err, extra={"src_module": "upload", "operation": "orphan_cleanup", "request_id": ctx.request_id, "error": str(tg_err)})
        return mcp_result(ctx.req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': 'Telegram notification failed; upload request was not created. Please retry.',
                'detail': str(tg_err),
            })}],
            'isError': True,
        })

    # Post-notification: store telegram_message_id + schedule expiry cleanup
    tg_message_id = tg_result.get('result', {}).get('message_id')
    if tg_message_id:
        from notifications import post_notification_setup
        post_notification_setup(
            request_id=ctx.request_id,
            telegram_message_id=tg_message_id,
            expires_at=ttl,
        )

    # 一律異步返回：讓 client 用 bouncer_status 輪詢結果。
    # sync long-polling 已移除。
    return mcp_result(ctx.req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'status': 'pending_approval',
            'request_id': ctx.request_id,
            's3_uri': s3_uri,
            'size': size_str,
            'message': '請求已發送，用 bouncer_status 查詢結果',
            'expires_in': f'{UPLOAD_TIMEOUT} seconds'
        })}]
    })


def mcp_tool_upload(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_upload（上傳檔案到 S3 桶，支援跨帳號，需要 Telegram 審批）"""
    # Phase 1: Parse & validate request, resolve account
    ctx = _parse_upload_request(req_id, arguments)
    if not isinstance(ctx, UploadContext):
        return ctx  # validation error

    # Phase 2: Determine bucket/key/request_id
    _resolve_upload_target(ctx)

    # Phase 3: Pipeline — first non-None result wins
    result = (
        _check_upload_rate_limit(ctx)
        or _check_upload_trust(ctx)
        or _submit_upload_for_approval(ctx)
    )

    return result


# =============================================================================
# Batch Upload
# =============================================================================

def _format_size_human(size_bytes: int) -> str:
    """格式化檔案大小為人類可讀格式"""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes} bytes"


def _validate_upload_batch_request(req_id: str, files: list) -> Optional[dict]:
    """Validate files array and payload sizes. Returns error response or None if valid."""
    if not files or not isinstance(files, list):
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error', 'error': 'files array is required and must be non-empty',
            })}],
            'isError': True,
        })

    if len(files) > 50:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error', 'error': f'Too many files: {len(files)} (max 50)',
            })}],
            'isError': True,
        })

    # Early payload size validation (before any base64 decode)
    for i, f in enumerate(files):
        file_b64_size = len(str(f.get('content', '')).strip())
        if file_b64_size > UPLOAD_BATCH_PER_FILE_B64_LIMIT:
            fname = str(f.get('filename', f'file #{i+1}')).strip()
            file_mb = file_b64_size / 1024 / 1024
            limit_mb = UPLOAD_BATCH_PER_FILE_B64_LIMIT / 1024 / 1024
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': (
                        f'File #{i+1} ({fname}): base64 payload too large: '
                        f'{file_mb:.2f}MB (per-file safe limit {limit_mb:.1f}MB). '
                        f'Use bouncer_request_presigned_batch for large files.'
                    ),
                    'suggestion': 'bouncer_request_presigned_batch',
                    'file_index': i + 1,
                    'filename': fname,
                    'file_b64_size': file_b64_size,
                    'per_file_limit': UPLOAD_BATCH_PER_FILE_B64_LIMIT,
                })}],
                'isError': True,
            })

    total_b64_size = sum(len(str(f.get('content', '')).strip()) for f in files)
    if total_b64_size > UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT:
        total_mb = total_b64_size / 1024 / 1024
        safe_mb = UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT / 1024 / 1024
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': (
                    f'Batch payload too large: {total_mb:.2f}MB base64 '
                    f'(safe limit {safe_mb:.1f}MB). '
                    f'Use bouncer_request_presigned_batch for large files.'
                ),
                'suggestion': 'bouncer_request_presigned_batch',
                'payload_size': total_b64_size,
                'safe_limit': UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT,
            })}],
            'isError': True,
        })
    return None


def _resolve_upload_account(req_id: str, account_id: Optional[str]) -> tuple:
    """Resolve account details for upload. Returns (assume_role, account_name, target_account_id, error)."""
    init_default_account()
    assume_role = None
    account_name = 'Default'
    target_account_id = DEFAULT_ACCOUNT_ID

    if not account_id and DEFAULT_ACCOUNT_ID:
        default_account = get_account(DEFAULT_ACCOUNT_ID)
        if default_account:
            assume_role = default_account.get('role_arn')
            account_name = default_account.get('name', 'Default')

    if account_id:
        valid, error = validate_account_id(account_id)
        if not valid:
            return None, None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({'status': 'error', 'error': error})}],
                'isError': True,
            })
        account = get_account(account_id)
        if not account:
            return None, None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error', 'error': f'帳號 {account_id} 未配置',
                })}],
                'isError': True,
            })
        if not account.get('enabled', True):
            return None, None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error', 'error': f'帳號 {account_id} 已停用',
                })}],
                'isError': True,
            })
        assume_role = account.get('role_arn')
        account_name = account.get('name', account_id)
        target_account_id = account_id

    return assume_role, account_name, target_account_id, None


def _preprocess_upload_files(req_id: str, files: list) -> tuple:
    """Validate and preprocess files. Returns (processed_files, total_size, error)."""
    import base64
    import hashlib as _hashlib
    from trust import _is_upload_extension_blocked, _is_upload_filename_safe

    processed_files = []
    total_size = 0

    for i, f in enumerate(files):
        fname = str(f.get('filename', '')).strip()
        content_b64 = str(f.get('content', '')).strip()
        ct = str(f.get('content_type', 'application/octet-stream')).strip()

        if not fname:
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error', 'error': f'File #{i+1}: filename is required',
                })}],
                'isError': True,
            })

        if not content_b64:
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error', 'error': f'File #{i+1} ({fname}): content is required',
                })}],
                'isError': True,
            })

        safe_name = sanitize_filename(fname)
        if not _is_upload_filename_safe(safe_name):
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error', 'error': f'File #{i+1} ({fname}): unsafe filename',
                })}],
                'isError': True,
            })

        if _is_upload_extension_blocked(safe_name):
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'File #{i+1} ({safe_name}): blocked extension',
                })}],
                'isError': True,
            })

        if len(content_b64) % 4 != 0:
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': (
                        f'File #{i+1} ({fname}): Invalid base64 content: length is not a multiple of 4. '
                        'This usually means the content was truncated by OS argument length limits. '
                        'Use the HTTP API directly or bouncer_request_presigned for large files.'
                    ),
                })}],
                'isError': True,
            })

        try:
            content_bytes = base64.b64decode(content_b64)
        except (binascii.Error, ValueError):
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'File #{i+1} ({safe_name}): invalid base64',
                })}],
                'isError': True,
            })

        # Security scan for each file in batch
        scan_result = scan_upload(safe_name, content_bytes, ct)
        if scan_result.is_blocked:
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'File #{i+1} ({safe_name}): rejected by security scan: {scan_result.summary}',
                    'scan_findings': scan_result.findings,
                }, ensure_ascii=False)}],
                'isError': True,
            })

        fsize = len(content_bytes)
        if fsize > TRUST_UPLOAD_MAX_BYTES_PER_FILE:
            return None, None, mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'File #{i+1} ({safe_name}): too large ({_format_size_human(fsize)}, max {_format_size_human(TRUST_UPLOAD_MAX_BYTES_PER_FILE)})',
                })}],
                'isError': True,
            })

        total_size += fsize
        file_metadata = {
            'filename': safe_name,
            'original_filename': fname,
            'content_b64': content_b64,
            'content_bytes': content_bytes,
            'content_type': ct,
            'size': fsize,
            'sha256': _hashlib.sha256(content_bytes).hexdigest(),
        }
        # Add scan results for notification purposes
        if scan_result.risk_level in ('high', 'medium'):
            file_metadata['scan_warning'] = scan_result.summary
            file_metadata['scan_findings'] = scan_result.findings
        processed_files.append(file_metadata)

    if total_size > TRUST_UPLOAD_MAX_BYTES_TOTAL:
        return None, None, mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'Total size {_format_size_human(total_size)} exceeds limit ({_format_size_human(TRUST_UPLOAD_MAX_BYTES_TOTAL)})',
            })}],
            'isError': True,
        })

    return processed_files, total_size, None


def _try_trust_auto_approve_batch(
    req_id: str, trust_scope: str, target_account_id: str,
    processed_files: list, total_size: int, bucket: str,
    date_str: str, assume_role: Optional[str], source: Optional[str]
) -> Optional[dict]:
    """Try trust auto-approve for batch upload. Returns MCP result if approved, None otherwise."""
    if not trust_scope:
        return None

    from trust import should_trust_approve_upload, increment_trust_upload_count, get_trust_session

    session = get_trust_session(trust_scope, target_account_id or DEFAULT_ACCOUNT_ID)
    if not session:
        return None

    max_uploads = int(session.get('max_uploads', 0))
    upload_count = int(session.get('upload_count', 0))
    upload_bytes = int(session.get('upload_bytes_total', 0))
    remaining_count = max_uploads - upload_count
    remaining_bytes = TRUST_UPLOAD_MAX_BYTES_TOTAL - upload_bytes

    # Check if all files can fit in trust quota
    if not (remaining_count >= len(processed_files)
            and remaining_bytes >= total_size
            and max_uploads > 0):
        return None

    # Check each file against trust rules
    for pf in processed_files:
        ok, _, _ = should_trust_approve_upload(
            trust_scope, target_account_id or DEFAULT_ACCOUNT_ID,
            pf['filename'], pf['size'],
        )
        if not ok:
            return None

    # Execute all uploads under trust
    uploaded = []
    try:
        # Sprint 58 s58-003: use top-level import
        s3 = get_s3_client(role_arn=assume_role, session_name='bouncer-batch-trust-upload')

        for pf in processed_files:
            inc_ok = increment_trust_upload_count(session['request_id'], pf['size'])
            if not inc_ok:
                break  # quota race, stop

            fkey = f"{date_str}/{generate_request_id('batch-upload')}/{pf['filename']}"
            s3.put_object(
                Bucket=bucket, Key=fkey,
                Body=pf['content_bytes'], ContentType=pf['content_type'],
            )
            vr = _verify_upload(s3, bucket, fkey, pf['filename'])
            uploaded.append({
                'filename': pf['filename'], 's3_uri': vr.s3_uri,
                'size': pf['size'], 'sha256': pf['sha256'],
                'verified': vr.verified, 's3_size': vr.s3_size,
            })

        if uploaded:
            verification_failed = [u['filename'] for u in uploaded if not u['verified']]
            new_count = upload_count + len(uploaded)
            send_trust_upload_notification(
                filename=f"[batch: {len(uploaded)} files]",
                content_size=sum(u['size'] for u in uploaded),
                sha256_hash='batch', trust_id=session['request_id'],
                upload_count=new_count, max_uploads=max_uploads, source=source,
            )

            result_payload = {
                'status': 'trust_auto_approved', 'uploaded': uploaded,
                'total_files': len(uploaded),
                'total_size': sum(u['size'] for u in uploaded),
                'trust_session': session['request_id'],
                'upload_quota': f"{new_count}/{max_uploads}",
            }
            if verification_failed:
                result_payload['verification_failed'] = verification_failed

            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps(result_payload)}],
            })
    except ClientError as e:
        logger.error("Batch trust upload error: %s", e, extra={"src_module": "upload", "operation": "batch_trust_upload", "error": str(e)})

    return None


def _submit_batch_for_approval(
    req_id: str, processed_files: list, total_size: int, bucket: str,
    reason: str, source: Optional[str], trust_scope: str,
    target_account_id: str, account_name: str, assume_role: Optional[str]
) -> dict:
    """Stage files to S3 and submit batch for human approval."""
    batch_id = generate_request_id('upload_batch')
    ttl = int(time.time()) + UPLOAD_TIMEOUT + APPROVAL_TTL_BUFFER

    # Group files by extension for display
    ext_counts = {}
    for pf in processed_files:
        ext = pf['filename'].rsplit('.', 1)[-1].upper() if '.' in pf['filename'] else 'OTHER'
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

    # Stage files to S3 (avoids 400KB DynamoDB item limit)
    staging_bucket = f"bouncer-uploads-{DEFAULT_ACCOUNT_ID}"
    s3_staging = get_s3_client()
    files_manifest = []
    staged_keys = []

    for pf in processed_files:
        s3_key = f"pending/{batch_id}/{pf['filename']}"
        try:
            s3_staging.put_object(
                Bucket=staging_bucket, Key=s3_key,
                Body=pf['content_bytes'], ContentType=pf['content_type'],
            )
            staged_keys.append(s3_key)
        except Exception as e:  # noqa: BLE001
            # Rollback staged objects
            for rk in staged_keys:
                try:
                    s3_staging.delete_object(Bucket=staging_bucket, Key=rk)
                except Exception:  # noqa: BLE001
                    logger.warning("[UPLOAD-BATCH] Rollback cleanup failed for key=%s", rk, extra={"src_module": "upload", "operation": "rollback_cleanup", "s3_key": rk})
            return mcp_result(req_id, {
                'content': [{'type': 'text', 'text': json.dumps({
                    'status': 'error',
                    'error': f'Failed to stage file {pf["filename"]} to S3: {str(e)}',
                })}],
                'isError': True,
            })

        files_manifest.append({
            'filename': pf['filename'], 's3_key': s3_key,
            'content_type': pf['content_type'],
            'size': pf['size'], 'sha256': pf['sha256'],
        })

    # Create DynamoDB item
    item = {
        'request_id': batch_id, 'action': 'upload_batch', 'bucket': bucket,
        'files': json.dumps(files_manifest),
        'file_count': len(processed_files), 'total_size': total_size,
        'reason': reason, 'source': source or '__anonymous__',
        'trust_scope': trust_scope,
        'account_id': target_account_id, 'account_name': account_name,
        'status': 'pending_approval', 'created_at': int(time.time()),
        'ttl': ttl, 'mode': 'mcp',
        'display_summary': generate_display_summary('upload_batch', file_count=len(processed_files), total_size=total_size),
    }
    if assume_role:
        item['assume_role'] = assume_role
    table.put_item(Item=item)

    # Send Telegram notification
    batch_notified = send_batch_upload_notification(
        batch_id=batch_id, file_count=len(processed_files),
        total_size=total_size, ext_counts=ext_counts,
        reason=reason, source=source,
        account_name=account_name, trust_scope=trust_scope,
    )

    if batch_notified.message_id:
        from notifications import post_notification_setup
        post_notification_setup(
            request_id=batch_id,
            telegram_message_id=batch_notified.message_id,
            expires_at=ttl,
        )

    return mcp_result(req_id, {
        'content': [{
            'type': 'text',
            'text': json.dumps({
                'status': 'pending_approval', 'request_id': batch_id,
                'file_count': len(processed_files),
                'total_size': _format_size_human(total_size),
                'message': '批量上傳請求已發送，用 bouncer_status 查詢結果',
                'expires_in': f'{UPLOAD_TIMEOUT} seconds',
            }),
        }],
    })


def mcp_tool_upload_batch(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_upload_batch — 批量上傳多個檔案到 S3"""
    files = arguments.get('files', [])
    reason = str(arguments.get('reason', 'No reason provided'))
    source = arguments.get('source', None)
    trust_scope = str(arguments.get('trust_scope', '')).strip()
    account_id = arguments.get('account', None)
    if account_id:
        account_id = str(account_id).strip()

    # Validate files array and payload sizes
    validation_error = _validate_upload_batch_request(req_id, files)
    if validation_error:
        return validation_error

    # Resolve account details
    assume_role, account_name, target_account_id, account_error = _resolve_upload_account(req_id, account_id)
    if account_error:
        return account_error

    # Preprocess and validate each file
    processed_files, total_size, preprocess_error = _preprocess_upload_files(req_id, files)
    if preprocess_error:
        return preprocess_error

    bucket = f"bouncer-uploads-{target_account_id}"
    date_str = time.strftime('%Y-%m-%d')

    # Try trust auto-approve
    trust_result = _try_trust_auto_approve_batch(
        req_id, trust_scope, target_account_id, processed_files,
        total_size, bucket, date_str, assume_role, source
    )
    if trust_result:
        return trust_result

    # Submit batch for human approval
    return _submit_batch_for_approval(
        req_id, processed_files, total_size, bucket,
        reason, source, trust_scope, target_account_id,
        account_name, assume_role
    )


# ============================================================================
# Upload Execution (moved from app.py to break circular dependency)
# ============================================================================

def execute_upload(request_id: str, approver: str) -> dict:
    """執行已審批的上傳（支援跨帳號）

    Content is read from S3 staging (pending/) and copied to the target bucket.
    The staging object is deleted after a successful copy.
    """
    try:
        result = table.get_item(Key={'request_id': request_id})
        item = result.get('Item')

        if not item:
            return {'success': False, 'error': 'Request not found'}

        bucket = item.get('bucket')
        key = item.get('key')
        content_type = item.get('content_type', 'application/octet-stream')
        assume_role_arn = item.get('assume_role')

        # Support both old (content) and new (content_s3_key) formats
        content_s3_key = item.get('content_s3_key')
        content_b64_legacy = item.get('content')  # backward compat for old items

        # 建立 S3 client（跨帳號時用 assume role）
        s3 = get_s3_client(role_arn=assume_role_arn, session_name='bouncer-upload')

        if content_s3_key:
            # New path: S3-to-S3 copy (no download needed)
            # Staging bucket 固定用主帳號 bucket（與 submit 時一致）
            staging_bucket = f"bouncer-uploads-{DEFAULT_ACCOUNT_ID}"
            s3.copy_object(
                CopySource={'Bucket': staging_bucket, 'Key': content_s3_key},
                Bucket=bucket,
                Key=key,
                ContentType=content_type,
                MetadataDirective='REPLACE',
            )
            # Cleanup staging object
            try:
                s3.delete_object(Bucket=staging_bucket, Key=content_s3_key)
            except ClientError:
                logger.warning("[UPLOAD] Staging cleanup failed for key=%s (non-critical, TTL will handle it)", content_s3_key, extra={"src_module": "upload", "operation": "staging_cleanup", "s3_key": content_s3_key})
        else:
            # Legacy path: base64-decode from DDB item then upload
            import base64
            content_bytes = base64.b64decode(content_b64_legacy)
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=content_bytes,
                ContentType=content_type
            )

        # 產生 S3 URL
        region = s3.meta.region_name or 'us-east-1'
        if region == 'us-east-1':
            s3_url = f"https://{bucket}.s3.amazonaws.com/{key}"
        else:
            s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"

        # 更新 DB
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #status = :status, approver = :approver, s3_url = :url, approved_at = :at',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'approved',
                ':approver': approver,
                ':url': s3_url,
                ':at': int(time.time())
            }
        )

        return {
            'success': True,
            's3_uri': f"s3://{bucket}/{key}",
            's3_url': s3_url
        }

    except ClientError as e:
        # 記錄失敗
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression='SET #status = :status, #error = :error',
            ExpressionAttributeNames={'#status': 'status', '#error': 'error'},
            ExpressionAttributeValues={
                ':status': 'error',
                ':error': str(e)
            }
        )
        return {'success': False, 'error': str(e)}
