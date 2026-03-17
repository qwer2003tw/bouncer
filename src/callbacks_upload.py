"""
Bouncer - Upload Callback 處理模組 (Sprint 55 Phase 3)

Upload-related callback handlers extracted from callbacks.py
"""

from botocore.exceptions import ClientError

from aws_clients import get_s3_client
from aws_lambda_powertools import Logger

from utils import response, format_size_human, build_info_lines
from trust import create_trust_session
from telegram import escape_markdown, update_message, answer_callback
from constants import TRUST_SESSION_MAX_UPLOADS, TRUST_SESSION_MAX_COMMANDS
from metrics import emit_metric
from mcp_upload import execute_upload, _verify_upload

logger = Logger()


# Import helper functions from callbacks.py (these remain in callbacks.py)
def _get_table():
    """Import from callbacks.py to avoid circular dependency"""
    from callbacks import _get_table as _gt
    return _gt()


def _update_request_status(table, request_id: str, status: str, approver: str, extra_attrs: dict = None) -> None:
    """Import from callbacks.py to avoid circular dependency"""
    from callbacks import _update_request_status as _urs
    return _urs(table, request_id, status, approver, extra_attrs)


# ============================================================================
# Upload Single File Callback
# ============================================================================

def handle_upload_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理上傳的審批 callback"""
    table = _get_table()

    bucket = item.get('bucket', '')
    key = item.get('key', '')
    content_size = int(item.get('content_size', 0))
    source = item.get('source', '')
    reason = item.get('reason', '')
    context = item.get('context', '')
    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')

    s3_uri = f"s3://{bucket}/{key}"
    info_lines = build_info_lines(
        source=source, context=context,
        account_name=account_name, account_id=account_id,
    )

    size_str = format_size_human(content_size)
    safe_reason = escape_markdown(reason)

    # SEC: verify approval has not expired
    import time as _time
    _item_ttl = int(item.get('ttl', 0))
    if _item_ttl and int(_time.time()) > _item_ttl and action == 'approve':
        logger.warning("upload_callback rejected: approval expired for %s", request_id, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        answer_callback(callback_id, '❌ 審批已過期，請重新上傳')
        try:
            update_message(message_id, '❌ *審批已過期*\n\n`' + request_id + '`', remove_buttons=True)
        except Exception as _exc:  # noqa: BLE001 — best-effort
            logger.debug("TTL check update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        return response(200, {'ok': True})

    if action == 'approve':
        # 執行上傳
        answer_callback(callback_id, '📤 上傳中...')
        result = execute_upload(request_id, user_id)

        if result.get('success'):
            emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'approved', 'Type': 'single'})
            update_message(
                message_id,
                f"✅ *已上傳*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{info_lines}"
                f"📁 *目標：* `{s3_uri}`\n"
                f"📊 *大小：* {size_str}\n"
                f"🔗 *URL：* {result.get('s3_url', '')}\n"
                f"💬 *原因：* {safe_reason}"
            )
        else:
            # 上傳失敗
            error = result.get('error', 'Unknown error')
            update_message(
                message_id,
                f"❌ *上傳失敗*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{info_lines}"
                f"📁 *目標：* `{s3_uri}`\n"
                f"📊 *大小：* {size_str}\n"
                f"❗ *錯誤：* {error}\n"
                f"💬 *原因：* {safe_reason}"
            )

    elif action == 'deny':
        emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'denied', 'Type': 'single'})
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ *已拒絕上傳*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{info_lines}"
            f"📁 *目標：* `{s3_uri}`\n"
            f"📊 *大小：* {size_str}\n"
            f"💬 *原因：* {safe_reason}"
        )

    return response(200, {'ok': True})


# ============================================================================
# Upload Batch Callback
# ============================================================================

def _parse_callback_files_manifest(item: dict, callback_id: str) -> 'list | dict':
    """Parse and validate files manifest from callback item.

    Returns:
        list: Parsed files manifest on success
        dict: Error response on failure
    """
    import json as _json
    try:
        files_manifest = _json.loads(item.get('files', '[]'))
        return files_manifest
    except _json.JSONDecodeError as e:
        logger.error("Failed to parse files manifest: %s", e, extra={"src_module": "callbacks", "operation": "parse_files_manifest", "error": str(e)}, exc_info=True)
        answer_callback(callback_id, '❌ 檔案清單解析失敗')
        return response(500, {'error': 'Failed to parse files manifest'})


def _setup_callback_s3_clients(assume_role, table, request_id: str, user_id: str, message_id: int) -> 'tuple | dict':
    """Setup dual S3 clients for batch upload callback.

    Returns:
        tuple: (s3_staging, s3_target) on success
        dict: Error response on failure
    """
    try:
        s3_staging = get_s3_client(role_arn=None, session_name='bouncer-batch-upload-staging')
        s3_target = get_s3_client(role_arn=assume_role, session_name='bouncer-batch-upload')
        return (s3_staging, s3_target)
    except ClientError as e:
        _update_request_status(table, request_id, 'error', user_id, extra_attrs={'error_message': str(e)})
        update_message(
            message_id,
            f"❌ *批量上傳失敗*（S3 連線錯誤）\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"❗ *錯誤：* {str(e)[:200]}",
        )
        return response(500, {'error': str(e)})


def _execute_callback_upload_batch(
    files_manifest: list,
    s3_staging,
    s3_target,
    bucket: str,
    message_id: int,
    request_id: str,
    file_count: int
) -> tuple:
    """Execute the batch upload loop with progress updates.

    Returns:
        tuple: (uploaded, errors, verification_failed)
    """
    import time as _time
    date_str = _time.strftime('%Y-%m-%d')
    uploaded = []
    errors = []
    verification_failed = []

    for i, fm in enumerate(files_manifest):
        fname = fm.get('filename', 'unknown')
        try:
            s3_key = fm.get('s3_key')  # new format
            content_b64_legacy = fm.get('content_b64')  # old format fallback
            from utils import generate_request_id as _gen_id
            fkey = f"{date_str}/{_gen_id('batch')}/{fname}"
            if s3_key:
                # New path: read from staging (Lambda role), write to target (assumed role).
                # Previously used copy_object with the assumed-role client which fails
                # silently when the assumed role has no read access to staging bucket (#39).
                from constants import DEFAULT_ACCOUNT_ID as _DEFAULT_ACCOUNT_ID
                staging_bucket = f"bouncer-uploads-{_DEFAULT_ACCOUNT_ID}"
                obj = s3_staging.get_object(Bucket=staging_bucket, Key=s3_key)
                body = obj['Body'].read()
                s3_target.put_object(
                    Bucket=bucket,
                    Key=fkey,
                    Body=body,
                    ContentType=fm.get('content_type', 'application/octet-stream'),
                )
                # Cleanup staging object (best effort, non-blocking)
                try:
                    s3_staging.delete_object(Bucket=staging_bucket, Key=s3_key)
                except Exception:  # noqa: BLE001 — S3 staging cleanup is best-effort
                    logger.warning("Staging cleanup failed for key=%s (non-critical)", s3_key, extra={"src_module": "callbacks", "operation": "upload_batch_cleanup", "s3_key": s3_key}, exc_info=True)  # [UPLOAD-BATCH] Staging cleanup failed
            else:
                # Legacy path: decode base64 and upload directly to target
                import base64 as _b64
                content_bytes = _b64.b64decode(content_b64_legacy or '')
                s3_target.put_object(
                    Bucket=bucket,
                    Key=fkey,
                    Body=content_bytes,
                    ContentType=fm.get('content_type', 'application/octet-stream'),
                )

            # Verify file exists after upload (non-blocking)
            vr = _verify_upload(s3_target, bucket, fkey, fname)
            if not vr.verified:
                verification_failed.append(fname)
                # Non-blocking: record in verification_failed but still count as uploaded

            uploaded.append({
                'filename': fname,
                's3_uri': vr.s3_uri,
                'size': fm.get('size', 0),
                'verified': vr.verified,
                's3_size': vr.s3_size,
            })
        except Exception as e:  # noqa: BLE001
            errors.append({'filename': fname, 'reason': str(e)[:120]})

        # Update progress every 5 files
        if (i + 1) % 5 == 0 or i == len(files_manifest) - 1:
            try:
                update_message(
                    message_id,
                    f"⏳ *批量上傳中...*\n\n"
                    f"📋 *請求 ID：* `{request_id}`\n"
                    f"進度: {i + 1}/{file_count}",
                )
            except Exception:  # noqa: BLE001 — progress update is best-effort
                logger.warning("Progress update failed at step %d (non-critical)", i + 1, extra={"src_module": "callbacks", "operation": "upload_batch_progress", "step": i + 1}, exc_info=True)  # [UPLOAD-BATCH] Progress update failed at step

    return (uploaded, errors, verification_failed)


def _finalize_callback_upload(
    table,
    request_id: str,
    user_id: str,
    files_manifest: list,
    uploaded: list,
    errors: list,
    verification_failed: list
) -> str:
    """Determine final upload status and update database.

    Returns:
        str: upload_status ('completed', 'failed', or 'partial')
    """
    import json as _json

    total_files = len(files_manifest)
    success_count = len(uploaded)
    fail_count = len(errors)

    if fail_count == 0:
        upload_status = 'completed'
    elif success_count == 0:
        upload_status = 'failed'
    else:
        upload_status = 'partial'

    _update_request_status(table, request_id, 'approved', user_id, extra_attrs={
        'uploaded_count': success_count,
        'error_count': fail_count,
        'upload_status': upload_status,
        'uploaded_files': _json.dumps([u['filename'] for u in uploaded]),
        'failed_files': _json.dumps([f['filename'] for f in errors]),
        'uploaded_details': _json.dumps(uploaded),
        'failed_details': _json.dumps(errors),
        'total_files': total_files,
        'verification_failed': _json.dumps(verification_failed),
    })
    emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'approved', 'Type': 'batch'})

    return upload_status


def _create_callback_trust_session(
    action: str,
    trust_scope: str,
    account_id: str,
    user_id: str,
    source: str,
    source_ip: str = '',
) -> str:
    """Create trust session if action is approve_trust.

    Returns:
        str: Trust line for message (empty if no trust session created)
    """
    trust_line = ""
    if action == 'approve_trust' and trust_scope:
        trust_id = create_trust_session(
            trust_scope, account_id, user_id, source=source,
            max_uploads=TRUST_SESSION_MAX_UPLOADS,
            creator_ip=source_ip,
        )
        emit_metric('Bouncer', 'TrustSession', 1, dimensions={'Event': 'created'})
        trust_line = (
            f"\n\n🔓 信任時段已啟動：`{trust_id}`"
            f"\n📊 命令: 0/{TRUST_SESSION_MAX_COMMANDS} | 上傳: 0/{TRUST_SESSION_MAX_UPLOADS}"
        )
    return trust_line


def handle_upload_batch_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理批量上傳的審批 callback"""
    table = _get_table()

    bucket = item.get('bucket', '')
    file_count = int(item.get('file_count', 0))
    total_size = int(item.get('total_size', 0))
    source = item.get('source', '')
    reason = item.get('reason', '')
    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    trust_scope = item.get('trust_scope', '')
    assume_role = item.get('assume_role', None)

    size_str = format_size_human(total_size)
    safe_reason = escape_markdown(reason)

    source_line = build_info_lines(
        source=source, account_name=account_name, account_id=account_id,
    )

    # SEC: verify approval has not expired
    import time as _time
    _item_ttl = int(item.get('ttl', 0))
    if _item_ttl and int(_time.time()) > _item_ttl and action == 'approve':
        logger.warning("callback rejected: approval expired for %s", request_id, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        answer_callback(callback_id, '❌ 審批已過期，請重新發起請求')
        try:
            update_message(message_id, '❌ *審批已過期*\n\n`' + request_id + '`', remove_buttons=True)
        except Exception as _exc:  # noqa: BLE001 — best-effort
            logger.debug("TTL check update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        return response(200, {'ok': True})

    if action in ('approve', 'approve_trust'):
        # Parse files manifest
        files_manifest = _parse_callback_files_manifest(item, callback_id)
        if isinstance(files_manifest, dict) and 'statusCode' in files_manifest:
            return files_manifest

        # Update message to show progress
        update_message(
            message_id,
            f"⏳ *批量上傳中...*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📄 {file_count} 個檔案 ({size_str})\n"
            f"💬 *原因：* {safe_reason}\n\n"
            f"進度: 0/{file_count}",
            remove_buttons=True,
        )
        answer_callback(callback_id, '⏳ 上傳中...')

        # Get S3 clients.
        # s3_staging: Lambda execution role — reads from staging bucket (main account).
        # s3_target:  Assumed role (if set) — writes to target bucket (may be cross-account).
        # Using two separate clients avoids cross-account copy_object failures where the
        # assumed role lacks s3:GetObject on the staging bucket (#39).
        s3_clients = _setup_callback_s3_clients(assume_role, table, request_id, user_id, message_id)
        if isinstance(s3_clients, dict) and 'statusCode' in s3_clients:
            return s3_clients
        s3_staging, s3_target = s3_clients

        # Execute batch upload
        uploaded, errors, verification_failed = _execute_callback_upload_batch(
            files_manifest, s3_staging, s3_target, bucket, message_id, request_id, file_count
        )

        # Finalize upload status and update DB
        _finalize_callback_upload(
            table, request_id, user_id, files_manifest, uploaded, errors, verification_failed
        )

        # Create trust session if approve_trust
        trust_line = _create_callback_trust_session(
            action, trust_scope, account_id, user_id, source
        )

        error_line = f"\n❗ 失敗: {len(errors)} 個" if errors else ""

        update_message(
            message_id,
            f"✅ *批量上傳完成*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📄 成功: {len(uploaded)}/{file_count} 個檔案 ({size_str})"
            f"{error_line}"
            f"\n💬 *原因：* {safe_reason}"
            f"{trust_line}",
        )

    elif action == 'deny':
        emit_metric('Bouncer', 'Upload', 1, dimensions={'Status': 'denied', 'Type': 'batch'})
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        update_message(
            message_id,
            f"❌ *已拒絕批量上傳*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📄 {file_count} 個檔案 ({size_str})\n"
            f"💬 *原因：* {safe_reason}",
        )

    return response(200, {'ok': True})
