"""
Bouncer - Telegram Callback 處理模組

所有 handle_*_callback 函數
"""

import time
import urllib.error

from botocore.exceptions import ClientError

from aws_clients import get_s3_client
from aws_lambda_powertools import Logger


# 從其他模組導入
from utils import response, format_size_human, build_info_lines
from paging import get_paged_output
from telegram import escape_markdown, update_message, answer_callback, send_telegram_message_silent, pin_message
from constants import RESULT_TTL, TTL_30_DAYS
from metrics import emit_metric
# Grant callbacks extracted to separate module (Sprint 53 Phase 1)
from callbacks_grant import (  # noqa: F401
    handle_grant_approve,
    handle_grant_approve_all,
    handle_grant_approve_safe,
    handle_grant_deny,
)

# Command callbacks extracted to separate module (Sprint 54 Phase 2)
from callbacks_command import (  # noqa: F401,F811
    handle_command_callback,
    _parse_command_callback_request,
    _format_command_info,
    _execute_and_store_result,
    _auto_execute_pending_requests,
    _handle_trust_session,
    _format_approval_response,
    _handle_deny_callback,
)
# Re-export for backward compat with tests patching callbacks.X
from commands import execute_command  # noqa: F401
from paging import store_paged_output  # noqa: F401

# Upload callbacks extracted to separate module (Sprint 55 Phase 3)
from callbacks_upload import (  # noqa: F401,F811
    handle_upload_callback,
    handle_upload_batch_callback,
    _parse_callback_files_manifest,
    _setup_callback_s3_clients,
    _execute_callback_upload_batch,
    _finalize_callback_upload,
    _create_callback_trust_session,
)

# DynamoDB tables from db.py (no circular dependency)
import db as _db
from aws_clients import get_cloudfront_client  # noqa: E402
from deployer import _get_history_table, start_deploy, update_deploy_record  # noqa: E402
from notifications import _send_message_silent  # noqa: E402
from scheduler_service import get_scheduler_service  # noqa: E402
from utils import extract_exit_code  # noqa: E402


def _is_execute_failed(output: str) -> bool:
    """判斷 execute_command 輸出是否代表失敗。
    支援：❌ prefix（Bouncer 格式）和 (exit code: N) 格式（AWS CLI 直接輸出）。
    """
    code = extract_exit_code(output)
    return code is not None and code != 0


logger = Logger(service="bouncer")


# DynamoDB - via db.py (lazy init)
# Tests may inject directly: callbacks._table = mock_table
_table = None


def _get_table():
    """Get table, with test override support. Unified fallback via db.table."""
    if _table is not None:
        return _table
    return _db.table


def _get_accounts_table():
    """取得 accounts DynamoDB table"""
    return _db.accounts_table


# ============================================================================
# 共用函數
# ============================================================================

def _update_request_status(table, request_id: str, status: str, approver: str, extra_attrs: dict = None) -> None:
    """更新 DynamoDB 請求狀態

    Args:
        table: DynamoDB table resource
        request_id: 請求 ID
        status: 新狀態 (approved/denied)
        approver: 審批者 user_id
        extra_attrs: 額外要更新的屬性 dict
    """
    now = int(time.time())
    update_expr = 'SET #s = :s, approved_at = :t, approver = :a, #ttl = :ttl'
    expr_names = {'#s': 'status', '#ttl': 'ttl'}
    expr_values = {
        ':s': status,
        ':t': now,
        ':a': approver,
        ':ttl': now + RESULT_TTL,
    }

    if extra_attrs:
        for key, value in extra_attrs.items():
            placeholder = f':{key}'
            # 處理保留字
            if key in ('status', 'result'):
                expr_names[f'#{key}'] = key
                update_expr += f', #{key} = {placeholder}'
            else:
                update_expr += f', {key} = {placeholder}'
            expr_values[placeholder] = value

    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )

    # S35-003: Delete both cleanup and warning schedules (best-effort cleanup)
    # S59-001: Also delete reminder schedule
    # S60-004: Also delete escalation schedule
    try:
        svc = get_scheduler_service()
        svc.delete_schedule(request_id)  # cleanup schedule
        svc.delete_warning_schedule(request_id)  # warning schedule
        svc.delete_reminder_schedule(request_id)  # reminder schedule (s59-001)
        svc.delete_escalation_schedule(request_id)  # escalation schedule (s60-004)
    except Exception as _e:  # noqa: BLE001 — best-effort cleanup
        logger.debug("schedule cleanup ignored error: %s", _e, extra={"src_module": "callbacks", "operation": "cleanup_schedule"})


def _send_status_update(message_id: int, status_emoji: str, title: str, item: dict, extra_lines: str = '') -> None:
    """更新 Telegram 訊息

    Args:
        message_id: Telegram 訊息 ID
        status_emoji: 狀態 emoji (✅/❌)
        title: 標題文字
        item: 包含 request_id, source, context 等的 dict
        extra_lines: 額外要加在訊息中的行
    """
    request_id = item.get('request_id', '')
    info = build_info_lines(source=item.get('source', ''), context=item.get('context', ''))

    update_message(
        message_id,
        f"{status_emoji} *{title}*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{info}"
        f"{extra_lines}"
    )


# ============================================================================
# Command Callback (extracted to callbacks_command.py in Sprint 54 Phase 2)
# ============================================================================
# All command callback functions are now in callbacks_command.py


# ============================================================================
# Account Add Callback
# ============================================================================

def handle_account_add_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理新增帳號的審批 callback"""
    table = _get_table()
    accounts_table = _get_accounts_table()

    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    role_arn = item.get('role_arn', '')
    source = item.get('source', '')
    context = item.get('context', '')

    detail_lines = (
        f"🆔 *帳號 ID：* `{account_id}`\n"
        f"📛 *名稱：* {account_name}"
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

    if action == 'approve':
        # 寫入帳號配置
        answer_callback(callback_id, '✅ 處理中...')
        try:
            accounts_table.put_item(Item={
                'account_id': account_id,
                'name': account_name,
                'role_arn': role_arn if role_arn else None,
                'is_default': False,
                'enabled': True,
                'created_at': int(time.time()),
                'created_by': user_id
            })

            # Audit log: account modified
            logger.info("Account modified", extra={
                "src_module": "mcp_admin", "operation": "add_account",
                "account_id": account_id,
                "account_name": account_name,
                "source": source,
                "bot_id": "telegram_callback",
            })

            _update_request_status(table, request_id, 'approved', user_id)

            logger.info("Approval action", extra={
                "src_module": "callbacks", "operation": "approval_action",
                "action": "approve",
                "request_id": request_id,
                "request_type": "add_account",
                "user_id": str(user_id),
            })

            _send_status_update(
                message_id, '✅', '已新增帳號',
                {'request_id': request_id, 'source': source, 'context': context},
                extra_lines=f"{detail_lines}\n🔗 *Role：* `{role_arn}`"
            )

        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError):
            logger.exception("Internal error", extra={"src_module": "callbacks", "operation": "add_account"})
            answer_callback(callback_id, '❌ 新增失敗')
            return response(500, {'error': 'Internal server error'})

    elif action == 'deny':
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        logger.info("Approval action", extra={
            "src_module": "callbacks", "operation": "approval_action",
            "action": "deny",
            "request_id": request_id,
            "request_type": "add_account",
            "user_id": str(user_id),
            "source": source,
        })

        _send_status_update(
            message_id, '❌', '已拒絕新增帳號',
            {'request_id': request_id, 'source': source, 'context': context},
            extra_lines=detail_lines
        )

    return response(200, {'ok': True})


# ============================================================================
# Account Remove Callback
# ============================================================================

def handle_account_remove_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理移除帳號的審批 callback"""
    table = _get_table()
    accounts_table = _get_accounts_table()

    account_id = item.get('account_id', '')
    account_name = item.get('account_name', '')
    source = item.get('source', '')
    context = item.get('context', '')

    detail_lines = (
        f"🆔 *帳號 ID：* `{account_id}`\n"
        f"📛 *名稱：* {account_name}"
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

    if action == 'approve':
        answer_callback(callback_id, '✅ 處理中...')
        try:
            accounts_table.delete_item(Key={'account_id': account_id})

            # Audit log: account modified
            logger.info("Account modified", extra={
                "src_module": "mcp_admin", "operation": "remove_account",
                "account_id": account_id,
                "account_name": account_name,
                "source": source,
                "bot_id": "telegram_callback",
            })

            _update_request_status(table, request_id, 'approved', user_id)

            logger.info("Approval action", extra={
                "src_module": "callbacks", "operation": "approval_action",
                "action": "approve",
                "request_id": request_id,
                "request_type": "remove_account",
                "user_id": str(user_id),
            })

            _send_status_update(
                message_id, '✅', '已移除帳號',
                {'request_id': request_id, 'source': source, 'context': context},
                extra_lines=detail_lines
            )

        except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError):
            logger.exception("Internal error", extra={"src_module": "callbacks", "operation": "remove_account"})
            answer_callback(callback_id, '❌ 移除失敗')
            return response(500, {'error': 'Internal server error'})

    elif action == 'deny':
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        logger.info("Approval action", extra={
            "src_module": "callbacks", "operation": "approval_action",
            "action": "deny",
            "request_id": request_id,
            "request_type": "remove_account",
            "user_id": str(user_id),
            "source": source,
        })

        _send_status_update(
            message_id, '❌', '已拒絕移除帳號',
            {'request_id': request_id, 'source': source, 'context': context},
            extra_lines=detail_lines
        )

    return response(200, {'ok': True})


# ============================================================================
# Deploy Callback
# ============================================================================

def _handle_deploy_approve(request_id: str, item: dict, message_id: int,
                           callback_id: str, user_id: str, project_name: str,
                           branch: str, stack_name: str, source_line: str) -> None:
    """Handle approve action for deploy callback."""
    table = _get_table()

    project_id = item.get('project_id', '')
    reason = item.get('reason', '')

    answer_callback(callback_id, '🚀 啟動部署中...')
    _update_request_status(table, request_id, 'approved', user_id)

    logger.info("Approval action", extra={
        "src_module": "callbacks", "operation": "approval_action",
        "action": "approve",
        "request_id": request_id,
        "request_type": "deploy",
        "user_id": str(user_id),
    })

    # Immediate feedback: remove buttons before start_deploy (best-effort)
    try:
        update_message(
            message_id,
            f"⏳ *部署排隊中...*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}",
            remove_buttons=True,
        )
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        logger.warning(f"[deploy] Immediate feedback update_message failed (non-critical): {e}")

    # 啟動部署
    result = start_deploy(project_id, branch, user_id, reason)

    if 'error' in result or result.get('status') == 'conflict':
        emit_metric('Bouncer', 'Deploy', 1, dimensions={'Status': 'failed', 'Project': project_id})
        error_msg = result.get('error') or result.get('message', '啟動失敗')
        update_message(
            message_id,
            f"❌ *部署啟動失敗*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n\n"
            f"❗ *錯誤：* {escape_markdown(error_msg)}"
        )
    else:
        emit_metric('Bouncer', 'Deploy', 1, dimensions={'Status': 'started', 'Project': project_id})
        deploy_id = result.get('deploy_id', '')
        reason_line = f"📝 *原因：* {escape_markdown(reason)}\n" if reason else ""
        commit_short = result.get('commit_short')
        commit_message = result.get('commit_message', '')
        commit_line = ""
        if commit_short:
            commit_display = f"`{commit_short}`"
            if commit_message:
                commit_display += f" {escape_markdown(commit_message)}"
            commit_line = f"🔖 {commit_display}\n"
        update_message(
            message_id,
            f"🚀 *部署已啟動*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n"
            f"{reason_line}"
            f"📋 *Stack：* {stack_name}\n"
            f"{commit_line}"
            f"\n🆔 *部署 ID：* `{deploy_id}`\n\n"
            f"⏳ 部署進行中..."
        )

        # Pin the approval message so progress is visible (best-effort)
        try:
            pin_message(message_id)
        except Exception as pin_err:
            logger.warning(f"[deploy] Failed to pin message (ignored): {pin_err}")

        # Store telegram_message_id in deploy record for unpinning later
        if deploy_id:
            try:
                update_deploy_record(deploy_id, {'telegram_message_id': message_id})
            except ClientError as e:
                logger.warning("Failed to store telegram_message_id (ignored): %s", e,
                             extra={"src_module": "callbacks", "operation": "handle_deploy_callback",
                                    "error": str(e)})


def handle_deploy_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理部署的審批 callback"""
    table = _get_table()

    project_id = item.get('project_id', '')
    project_name = item.get('project_name', project_id)
    branch = item.get('branch', 'master')
    stack_name = item.get('stack_name', '')
    source = item.get('source', '')
    reason = item.get('reason', '')
    context = item.get('context', '')

    source_line = build_info_lines(source=source, context=context)

    # SEC: verify approval has not expired (use approval_expiry, fallback to ttl)
    import time as _time
    _expiry = int(item.get('approval_expiry', 0)) or int(item.get('ttl', 0))
    if _expiry and int(_time.time()) > _expiry and action == 'approve':
        logger.warning("deploy_callback rejected: approval expired for %s", request_id, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        answer_callback(callback_id, '❌ 審批已過期，請重新發起部署')
        try:
            update_message(message_id, '❌ *審批已過期*\n\n`' + request_id + '`', remove_buttons=True)
        except Exception as _exc:  # noqa: BLE001 — best-effort
            logger.debug("TTL check update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        return response(200, {'ok': True})

    if action == 'approve':
        _handle_deploy_approve(request_id, item, message_id, callback_id, user_id,
                               project_name, branch, stack_name, source_line)

    elif action == 'deny':
        answer_callback(callback_id, '❌ 已拒絕')
        _update_request_status(table, request_id, 'denied', user_id)

        logger.info("Approval action", extra={
            "src_module": "callbacks", "operation": "approval_action",
            "action": "deny",
            "request_id": request_id,
            "request_type": "deploy",
            "user_id": str(user_id),
            "source": item.get('source', ''),
        })

        update_message(
            message_id,
            f"❌ *已拒絕部署*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{source_line}"
            f"📦 *專案：* {project_name}\n"
            f"🌿 *分支：* {branch}\n"
            f"📋 *Stack：* {stack_name}\n\n"
            f"💬 *原因：* {escape_markdown(reason)}"
        )

    return response(200, {'ok': True})


# ============================================================================
# Deploy Frontend Callback (sprint9-003 Phase B)
# ============================================================================

def _write_frontend_deploy_history(
    request_id: str,
    project: str,
    deploy_status: str,
    user_id: str,
    file_count: int,
    success_count: int,
    fail_count: int,
    reason: str,
    source: str,
    frontend_bucket: str,
    distribution_id: str,
    cf_invalidation_failed: bool,
) -> None:
    """Write frontend deploy outcome to the deploy_history DynamoDB table.

    Uses the same table as SAM deploys (bouncer-deploy-history) so that
    bouncer_deploy_history can surface frontend deploys alongside SAM deploys.
    The project_id GSI (project-time-index) is keyed on project_id, so we
    map the frontend project name to project_id for consistent querying.
    """
    try:
        now = int(time.time())
        # Map deploy_status -> uppercase STATUS used by SAM deploys
        status_map = {
            'deployed': 'SUCCEEDED',
            'partial_deploy': 'PARTIAL',
            'deploy_failed': 'FAILED',
        }
        history_status = status_map.get(deploy_status, deploy_status.upper())

        history_item = {
            'deploy_id': f'frontend-{request_id}',
            'project_id': project,
            'deploy_type': 'frontend',
            'status': history_status,
            'started_at': now,
            'completed_at': now,
            'triggered_by': user_id,
            'reason': reason or '',
            'source': source or '',
            'files_count': file_count,
            'files_deployed': success_count,
            'files_failed': fail_count,
            'frontend_bucket': frontend_bucket,
            'distribution_id': distribution_id,
            'cf_invalidation_failed': cf_invalidation_failed,
            'request_id': request_id,
            'ttl': now + TTL_30_DAYS,  # 30 days
        }
        # DynamoDB does not allow None values
        history_item = {k: v for k, v in history_item.items() if v is not None}
        _get_history_table().put_item(Item=history_item)
        logger.info("deploy_history written deploy_id=frontend-%s project=%s status=%s", request_id, project, history_status, extra={"src_module": "callbacks", "operation": "write_deploy_history", "request_id": request_id, "project": project, "status": history_status})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to write deploy_history for %s: %s", request_id, exc, extra={"src_module": "callbacks", "operation": "write_deploy_history", "request_id": request_id, "error": str(exc)})


def _parse_deploy_frontend_params(item: dict) -> dict:
    """Parse and prepare parameters from deploy frontend request item.

    Returns dict with all necessary fields for deploy frontend processing.
    """
    project = item.get('project', '')
    staging_bucket = item.get('staging_bucket', '')
    frontend_bucket = item.get('frontend_bucket', '')
    distribution_id = item.get('distribution_id', '')
    source = item.get('source', '')
    reason = item.get('reason', '')
    files_json = item.get('files', '[]')
    file_count = int(item.get('file_count', 0))
    total_size = int(item.get('total_size', 0))
    deploy_role_arn = item.get('deploy_role_arn')

    safe_reason = escape_markdown(reason)
    size_str = format_size_human(total_size)
    source_line = build_info_lines(source=source)

    return {
        'project': project,
        'staging_bucket': staging_bucket,
        'frontend_bucket': frontend_bucket,
        'distribution_id': distribution_id,
        'source': source,
        'reason': reason,
        'files_json': files_json,
        'file_count': file_count,
        'total_size': total_size,
        'deploy_role_arn': deploy_role_arn,
        'safe_reason': safe_reason,
        'size_str': size_str,
        'source_line': source_line,
    }


def _handle_deploy_frontend_deny(table, request_id: str, callback_id: str, message_id: int, user_id: str, params: dict) -> dict:
    """Handle deny action for frontend deploy request.

    Updates status to rejected and sends notification message.
    Returns response dict.
    """
    answer_callback(callback_id, '❌ 已拒絕')
    _update_request_status(table, request_id, 'rejected', user_id)

    logger.info("Approval action", extra={
        "src_module": "callbacks", "operation": "approval_action",
        "action": "deny",
        "request_id": request_id,
        "request_type": "deploy_frontend",
        "user_id": str(user_id),
    })

    update_message(
        message_id,
        f"❌ *已拒絕前端部署*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{params['source_line']}"
        f"📦 *專案：* {escape_markdown(params['project'])}\n"
        f"📄 {params['file_count']} 個檔案 ({params['size_str']})\n"
        f"💬 *原因：* {params['safe_reason']}",
    )
    return response(200, {'ok': True})


def _assume_deploy_role(deploy_role_arn: str, request_id: str, files_manifest: list, table, message_id: int, user_id: str, params: dict, item: dict):
    """Assume deploy role for S3 operations.

    Returns:
        tuple: (s3_client, error_response_or_none)
        - If successful: (s3_client, None)
        - If failed: (None, response_dict)
    """
    import json as _json

    if not deploy_role_arn:
        return get_s3_client(), None

    try:
        s3_target = get_s3_client(role_arn=deploy_role_arn, session_name=f"bouncer-deploy-{request_id[:16]}")
        return s3_target, None
    except ClientError as e:
        logger.exception("AssumeRole failed for %s: %s", deploy_role_arn, e, extra={"src_module": "callbacks", "operation": "assume_role", "deploy_role_arn": deploy_role_arn, "error": str(e)})
        failed = [
            {'filename': fm.get('filename', 'unknown'), 'reason': f'AssumeRole failed: {e}'}
            for fm in files_manifest
        ]
        deploy_status = 'deploy_failed'
        extra_attrs = {
            'deploy_status': deploy_status,
            'deployed_count': 0,
            'failed_count': len(failed),
            'deployed_files': _json.dumps([]),
            'failed_files': _json.dumps([f['filename'] for f in failed]),
            'deployed_details': _json.dumps([]),
            'failed_details': _json.dumps(failed),
            'cf_invalidation_failed': False,
        }
        _update_request_status(table, request_id, 'approved', user_id, extra_attrs=extra_attrs)
        emit_metric('Bouncer', 'DeployFrontend', 1, dimensions={'Status': deploy_status, 'Project': params['project']})
        _write_frontend_deploy_history(
            request_id=request_id,
            project=params['project'],
            deploy_status=deploy_status,
            user_id=user_id,
            file_count=params['file_count'],
            success_count=0,
            fail_count=len(failed),
            reason=item.get('reason', ''),
            source=params['source'],
            frontend_bucket=params['frontend_bucket'],
            distribution_id=params['distribution_id'],
            cf_invalidation_failed=False,
        )
        update_message(
            message_id,
            f"❌ *前端部署失敗*\n\n"
            f"📋 *請求 ID：* `{request_id}`\n"
            f"{params['source_line']}"
            f"📦 *專案：* {escape_markdown(params['project'])}\n"
            f"❗ AssumeRole 失敗，全部 {params['file_count']} 個檔案無法部署\n"
            f"💬 *原因：* {params['safe_reason']}",
        )
        return None, response(200, {
            'ok': True,
            'deploy_status': deploy_status,
            'deployed_count': 0,
            'failed_count': len(failed),
            'cf_invalidation_failed': False,
        })


def _deploy_files_to_frontend(files_manifest: list, s3_staging, s3_target, request_id: str, message_id: int, params: dict, user_id: str) -> tuple:
    """Deploy files from staging bucket to frontend bucket.

    Returns:
        tuple: (deployed_list, failed_list)
    """
    deployed = []
    failed = []
    total_bytes = 0
    staging_bucket = params['staging_bucket']
    frontend_bucket = params['frontend_bucket']
    file_count = params['file_count']
    project = params['project']

    for i, fm in enumerate(files_manifest):
        filename = fm.get('filename', 'unknown')
        staged_key = fm.get('s3_key', '')
        content_type = fm.get('content_type', 'application/octet-stream')
        cache_control = fm.get('cache_control', 'no-cache')

        try:
            # Read from staging (Lambda role)
            obj = s3_staging.get_object(Bucket=staging_bucket, Key=staged_key)
            body = obj['Body'].read()

            # Write to frontend (assumed role or Lambda role)
            s3_target.put_object(
                Bucket=frontend_bucket,
                Key=filename,
                Body=body,
                ContentType=content_type,
                CacheControl=cache_control,
            )
            total_bytes += len(body)
            deployed.append({'filename': filename, 's3_key': filename})
            logger.info("uploaded file=%s size=%d content_type=%s request_id=%s project=%s", filename, len(body), content_type, request_id, project, extra={"src_module": "callbacks", "operation": "deploy_frontend_upload", "file_name": filename, "request_id": request_id, "project": project})
        except Exception as e:  # noqa: BLE001
            logger.exception("upload_failed file=%s error=%s request_id=%s project=%s", filename, str(e)[:200], request_id, project, extra={"src_module": "callbacks", "operation": "deploy_frontend_upload", "file_name": filename, "request_id": request_id, "project": project, "error": str(e)[:200]})
            failed.append({'filename': filename, 'reason': str(e)[:200]})

        # Progress update every 5 files
        if (i + 1) % 5 == 0 or i == len(files_manifest) - 1:
            try:
                update_message(
                    message_id,
                    f"⏳ *前端部署中...*\n\n"
                    f"📋 *請求 ID：* `{request_id}`\n"
                    f"進度: {i + 1}/{file_count}",
                )
            except Exception:  # noqa: BLE001 — progress update is best-effort
                logger.warning("Progress update failed at step %d (non-critical)", i + 1, extra={"src_module": "callbacks", "operation": "deploy_frontend_progress", "step": i + 1}, exc_info=True)  # [DEPLOY-FRONTEND] Progress update failed at step

    return deployed, failed, total_bytes


def _invalidate_cloudfront(success_count: int, deploy_role_arn: str, distribution_id: str, request_id: str) -> bool:
    """Invalidate CloudFront distribution if files were successfully deployed.

    Returns:
        bool: True if invalidation failed, False if succeeded or skipped (no files deployed)
    """
    if success_count == 0:
        return False

    try:
        cf = get_cloudfront_client(role_arn=deploy_role_arn)
        cf.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                'Paths': {'Quantity': 1, 'Items': ['/*']},
                'CallerReference': request_id,
            },
        )
        return False
    except ClientError as e:
        logger.exception("CloudFront invalidation failed for dist=%s: %s", distribution_id, e, extra={"src_module": "callbacks", "operation": "cloudfront_invalidation", "distribution_id": distribution_id, "error": str(e)})
        return True


def _finalize_deploy_frontend(deployed: list, failed: list, cf_invalidation_failed: bool, table, request_id: str,
                               user_id: str, message_id: int, params: dict, item: dict) -> dict:
    """Finalize deploy frontend: update DDB, emit metrics, write history, send notifications.

    Returns:
        dict: API Gateway response
    """
    import json as _json

    success_count = len(deployed)
    fail_count = len(failed)

    if success_count == 0:
        deploy_status = 'deploy_failed'
    elif fail_count == 0:
        deploy_status = 'deployed'
    else:
        deploy_status = 'partial_deploy'

    # Update DDB
    extra_attrs = {
        'deploy_status': deploy_status,
        'deployed_count': success_count,
        'failed_count': fail_count,
        'deployed_files': _json.dumps([d['filename'] for d in deployed]),
        'failed_files': _json.dumps([f['filename'] for f in failed]),
        'deployed_details': _json.dumps(deployed),
        'failed_details': _json.dumps(failed),
        'cf_invalidation_failed': cf_invalidation_failed,
    }
    _update_request_status(table, request_id, 'approved', user_id, extra_attrs=extra_attrs)

    emit_metric('Bouncer', 'DeployFrontend', 1, dimensions={'Status': deploy_status, 'Project': params['project']})

    # Write to deploy_history table (mirrors SAM deploy format)
    _write_frontend_deploy_history(
        request_id=request_id,
        project=params['project'],
        deploy_status=deploy_status,
        user_id=user_id,
        file_count=params['file_count'],
        success_count=success_count,
        fail_count=fail_count,
        reason=item.get('reason', ''),
        source=params['source'],
        frontend_bucket=params['frontend_bucket'],
        distribution_id=params['distribution_id'],
        cf_invalidation_failed=cf_invalidation_failed,
    )

    # Build result message
    cf_warn = "\n⚠️ *CloudFront Invalidation 失敗* (S3 已完成)" if cf_invalidation_failed else ""
    fail_line = f"\n❗ 失敗: {fail_count} 個" if fail_count > 0 else ""

    if deploy_status == 'deploy_failed':
        status_emoji = '❌'
        title = '前端部署失敗'
    elif deploy_status == 'partial_deploy':
        status_emoji = '⚠️'
        title = '前端部署部分成功'
    else:
        status_emoji = '✅'
        title = '前端部署完成'

    update_message(
        message_id,
        f"{status_emoji} *{title}*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{params['source_line']}"
        f"📦 *專案：* {escape_markdown(params['project'])}\n"
        f"📄 成功: {success_count}/{params['file_count']} 個檔案 ({params['size_str']})"
        f"{fail_line}\n"
        f"🌐 *目標 Bucket：* `{escape_markdown(params['frontend_bucket'])}`\n"
        f"☁️ *CloudFront：* `{escape_markdown(params['distribution_id'])}`\n"
        f"💬 *原因：* {params['safe_reason']}"
        f"{cf_warn}",
    )

    # Send Telegram result notification (silent)
    try:
        if deploy_status == 'deployed':
            notif_text = (
                f"✅ 前端部署成功\n"
                f"📦 {params['project']} — {success_count} 個檔案\n"
                f"🆔 `{request_id}`"
            )
        elif deploy_status == 'partial_deploy':
            notif_text = (
                f"⚠️ 前端部署部分成功\n"
                f"📦 {params['project']} — {success_count}/{params['file_count']} 成功，{fail_count} 失敗\n"
                f"🆔 `{request_id}`"
            )
        else:
            notif_text = (
                f"❌ 前端部署失敗\n"
                f"📦 {params['project']} — 全部 {params['file_count']} 個檔案失敗\n"
                f"🆔 `{request_id}`"
            )
        if cf_invalidation_failed:
            notif_text += "\n⚠️ CloudFront Invalidation 失敗"
        _send_message_silent(notif_text)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError) as notif_exc:
        logger.warning("Result notification failed: %s", notif_exc, extra={"src_module": "callbacks", "operation": "deploy_frontend_notify", "request_id": request_id, "error": str(notif_exc)})

    return response(200, {
        'ok': True,
        'deploy_status': deploy_status,
        'deployed_count': success_count,
        'failed_count': fail_count,
        'cf_invalidation_failed': cf_invalidation_failed,
    })


def handle_deploy_frontend_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str) -> dict:
    """處理前端部署的審批 callback

    action=approve: 從 DDB 讀 staged_files + target_info → S3 copy → CloudFront invalidation
    action=deny:    更新 DDB status=rejected，不執行任何部署
    """
    import json as _json

    table = _get_table()
    params = _parse_deploy_frontend_params(item)

    logger.info("deploy_frontend_callback received", extra={"src_module": "callbacks", "operation": "handle_deploy_frontend_callback", "request_id": request_id, "action": action, "user_id": user_id})

    if action == 'deny':
        logger.info("deploy_frontend_callback denied", extra={"src_module": "callbacks", "operation": "handle_deploy_frontend_callback", "request_id": request_id, "action": "deny", "user_id": user_id})
        return _handle_deploy_frontend_deny(table, request_id, callback_id, message_id, user_id, params)

    # action == 'approve'
    logger.info("deploy_frontend_callback approve started", extra={"src_module": "callbacks", "operation": "handle_deploy_frontend_callback", "request_id": request_id, "action": "approve", "user_id": user_id})
    # SEC: verify approval has not expired
    import time as _time
    item_ttl = int(item.get('ttl', 0))
    if item_ttl and int(_time.time()) > item_ttl:
        logger.warning("deploy_frontend_callback rejected: approval expired for %s", request_id, extra={"src_module": "callbacks", "operation": "handle_deploy_frontend_callback", "request_id": request_id})
        answer_callback(callback_id, '❌ 審批已過期，請重新發起前端部署')
        update_message(message_id, '❌ *審批已過期*\n\n`' + request_id + '`\n\n請重新呼叫 bouncer_confirm_frontend_deploy。', remove_buttons=True)
        return response(200, {'ok': True})

    logger.info("Approval action", extra={
        "src_module": "callbacks", "operation": "approval_action",
        "action": "approve",
        "request_id": request_id,
        "request_type": "deploy_frontend",
        "user_id": str(user_id),
    })

    try:
        files_manifest = _json.loads(params['files_json'])
    except _json.JSONDecodeError as e:
        logger.exception("Failed to parse files manifest for deploy-frontend: %s", e, extra={"src_module": "callbacks", "operation": "handle_deploy_frontend_callback", "error": str(e)}, exc_info=True)
        answer_callback(callback_id, '❌ 檔案清單解析失敗')
        return response(500, {'error': 'Failed to parse files manifest'})

    answer_callback(callback_id, '🚀 部署中...')
    update_message(
        message_id,
        f"⏳ *前端部署中...*\n\n"
        f"📋 *請求 ID：* `{request_id}`\n"
        f"{params['source_line']}"
        f"📦 *專案：* {escape_markdown(params['project'])}\n"
        f"📄 {params['file_count']} 個檔案 ({params['size_str']})\n"
        f"💬 *原因：* {params['safe_reason']}\n\n"
        f"進度: 0/{params['file_count']}",
        remove_buttons=True,
    )

    # 1. Assume deploy role
    s3_target, error_response = _assume_deploy_role(
        params['deploy_role_arn'], request_id, files_manifest, table, message_id, user_id, params, item
    )
    if error_response:
        return error_response

    # 2. Deploy files to frontend bucket
    s3_staging = get_s3_client()
    deployed, failed, actual_total_bytes = _deploy_files_to_frontend(
        files_manifest, s3_staging, s3_target, request_id, message_id, params, user_id
    )

    # 3. CloudFront invalidation
    cf_invalidation_failed = _invalidate_cloudfront(
        len(deployed), params['deploy_role_arn'], params['distribution_id'], request_id
    )

    # Update total_size from actual bytes transferred (fixes #421: presigned path has 0)
    if actual_total_bytes > 0:
        params['total_size'] = actual_total_bytes
        params['size_str'] = format_size_human(actual_total_bytes)

    # 4. Finalize: update DDB, metrics, history, and send notifications
    return _finalize_deploy_frontend(
        deployed, failed, cf_invalidation_failed, table, request_id, user_id, message_id, params, item
    )



# ============================================================================
# Show Page Callback (sprint13-003 on-demand pagination)
# ============================================================================

def handle_show_page_callback(query: dict, request_id: str, page_num: int) -> dict:
    """處理 show_page callback — 從 DynamoDB 拉取指定頁面並發送到 Telegram

    callback_data 格式：show_page:{request_id}:{page_num}

    All pages (including page 1) are stored in paging DDB by _write_all_pages().

    Args:
        query: Telegram callback query dict
        request_id: 原始命令的 request_id
        page_num: 要顯示的頁碼 (1-based)
    """
    callback_id = query.get('id', '')

    page_id = f"{request_id}:page:{page_num}"
    page_data = get_paged_output(page_id)

    if 'error' in page_data:
        answer_callback(callback_id, '❌ 頁面不存在或已過期')
        return response(200, {'ok': True})

    total_pages = page_data.get('total_pages', page_num)
    has_more = page_num < total_pages
    content_text = page_data.get('result', '')

    # Build Next Page button if more pages remain
    if has_more:
        next_page_num = page_num + 1
        next_btn = {
            'inline_keyboard': [[{
                'text': f'➡️ Next Page ({next_page_num}/{total_pages})',
                'callback_data': f'show_page:{request_id}:{next_page_num}',
            }]]
        }
    else:
        next_btn = None

    answer_callback(callback_id, f'📄 第 {page_num}/{total_pages} 頁')
    send_telegram_message_silent(
        f"📄 *第 {page_num}/{total_pages} 頁*\n\n```\n{content_text}\n```",
        reply_markup=next_btn,
    )

    return response(200, {'ok': True})
