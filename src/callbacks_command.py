"""
Bouncer - Command Callback 處理模組 (Phase 2 extract)

handle_command_callback + helpers extracted from callbacks.py
"""

import time
import urllib.error

from botocore.exceptions import ClientError

from aws_lambda_powertools import Logger

# 從其他模組導入
from utils import response, build_info_lines, generate_request_id, log_decision
from commands import execute_command, is_dangerous
from paging import store_paged_output
from trust import create_trust_session, track_command_executed, increment_trust_command_count, TrustRateExceeded
from telegram import escape_markdown, update_message, answer_callback, send_telegram_message_silent, send_chat_action, send_telegram_message_to
from notifications import send_trust_auto_approve_notification
from constants import DEFAULT_ACCOUNT_ID, RESULT_TTL, TRUST_SESSION_MAX_UPLOADS, TRUST_SESSION_MAX_COMMANDS, OTP_RISK_THRESHOLD
from metrics import emit_metric

# DynamoDB tables from db.py (no circular dependency)
import db as _db

logger = Logger(service="bouncer")


def _get_table():
    return _db.table


def _is_execute_failed(output: str) -> bool:
    """判斷 execute_command 輸出是否代表失敗。
    支援：❌ prefix（Bouncer 格式）和 (exit code: N) 格式（AWS CLI 直接輸出）。
    """
    from utils import extract_exit_code
    code = extract_exit_code(output)
    return code is not None and code != 0


def _update_request_status(table, request_id: str, status: str, approver: str, extra_attrs: dict = None) -> None:
    """更新 DynamoDB 請求狀態"""
    now = int(time.time())

    update_expr = 'SET #s = :s, approved_at = :t, approver = :a, #ttl = :ttl'
    expr_names = {'#s': 'status', '#ttl': 'ttl'}
    expr_values = {
        ':s': status,
        ':t': now,
        ':a': approver,
        ':ttl': now + RESULT_TTL
    }

    if extra_attrs:
        for key, value in extra_attrs.items():
            update_expr += f', {key} = :{key}'
            expr_values[f':{key}'] = value

    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values
    )


def _parse_command_callback_request(item: dict) -> dict:
    """解析 command callback 請求的資料

    Args:
        item: DynamoDB item

    Returns:
        dict: 包含 command, assume_role, source, trust_scope, reason, context, account_id, account_name
    """
    return {
        'command': item.get('command', ''),
        'assume_role': item.get('assume_role'),
        'source': item.get('source', ''),
        'trust_scope': item.get('trust_scope', ''),
        'reason': item.get('reason', ''),
        'context': item.get('context', ''),
        'account_id': item.get('account_id', DEFAULT_ACCOUNT_ID),
        'account_name': item.get('account_name', 'Default'),
    }


def _format_command_info(parsed: dict) -> dict:
    """格式化命令顯示資訊

    Args:
        parsed: _parse_command_callback_request 返回的 dict

    Returns:
        dict: 包含 source_line, account_line, safe_reason, cmd_preview
    """
    command = parsed['command']
    source = parsed['source']
    context = parsed['context']
    reason = parsed['reason']
    account_id = parsed['account_id']
    account_name = parsed['account_name']

    # build_info_lines escapes internally; pass raw values from DB
    source_line = build_info_lines(source=source, context=context)
    safe_account_name = escape_markdown(account_name) if account_name else ''
    account_line = f"🏦 *帳號：* `{account_id}` ({safe_account_name})\n"
    safe_reason = escape_markdown(reason)
    cmd_preview = command[:500] + '...' if len(command) > 500 else command

    return {
        'source_line': source_line,
        'account_line': account_line,
        'safe_reason': safe_reason,
        'cmd_preview': cmd_preview,
    }


def _execute_and_store_result(
    command: str,
    assume_role: str,
    request_id: str,
    item: dict,
    user_id: str,
    source_ip: str,
    action: str,
) -> dict:
    """執行命令並存入 DynamoDB

    Args:
        command: 要執行的命令
        assume_role: IAM role
        request_id: 請求 ID
        item: DynamoDB item (需要 created_at)
        user_id: 審批者 ID
        source_ip: 來源 IP (audit trail #74)
        action: approve 或 approve_trust

    Returns:
        dict: 包含 result, paged, decision_latency_ms
    """
    table = _get_table()

    # 執行命令（native boto3 or awscli based on stored action_type）
    if item.get('action_type') == 'native':
        import json as _json
        from commands import execute_boto3_native
        native_service = item.get('native_service', '')
        native_operation = item.get('native_operation', '')
        native_params_str = item.get('native_params', '{}')
        native_region = item.get('native_region', 'us-east-1') or 'us-east-1'
        try:
            native_params = _json.loads(native_params_str) if isinstance(native_params_str, str) else native_params_str
        except Exception:
            native_params = {}
        result = execute_boto3_native(
            service=native_service,
            operation=native_operation,
            params=native_params,
            region=native_region,
            assume_role_arn=assume_role,
        )
    else:
        result = execute_command(command, assume_role)
    cmd_status = 'failed' if _is_execute_failed(result) else 'success'
    emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'manual_approve'})
    paged = store_paged_output(request_id, result)

    # 計算決策延遲
    now = int(time.time())
    created_at = int(item.get('created_at', 0))
    decision_latency_ms = (now - created_at) * 1000 if created_at else 0
    if decision_latency_ms:
        emit_metric('Bouncer', 'DecisionLatency', decision_latency_ms, unit='Milliseconds', dimensions={'Action': 'approve'})

    decision_type = 'manual_approved_trust' if action == 'approve_trust' else 'manual_approved'

    # 存入 DynamoDB（包含分頁資訊 + audit trail #74）
    update_expr = (
        'SET #s = :s, #r = :r, approved_at = :t, approver = :a, '
        'approved_by = :aby, duration_ms = :dms, source_ip = :sip, '
        'decision_type = :dt, decided_at = :da, decision_latency_ms = :dl, '
        'command_status = :cs, #ttl = :ttl'
    )
    expr_names = {'#s': 'status', '#r': 'result', '#ttl': 'ttl'}
    expr_values = {
        ':s': 'approved',
        ':r': paged['result'],
        ':t': now,
        ':a': user_id,
        ':aby': user_id,                         # audit trail: who approved (Telegram user_id)
        ':dms': decision_latency_ms,             # audit trail: approval duration in ms
        ':sip': source_ip,                       # audit trail: Telegram server IP (#74)
        ':dt': decision_type,
        ':da': now,
        ':dl': decision_latency_ms,
        ':cs': cmd_status,
        ':ttl': now + RESULT_TTL
    }

    if paged.get('paged'):
        update_expr += ', paged = :p, total_pages = :tp, output_length = :ol, next_page = :np'
        expr_values[':p'] = True
        expr_values[':tp'] = paged['total_pages']
        expr_values[':ol'] = paged['output_length']
        expr_values[':np'] = paged.get('next_page')

    table.update_item(
        Key={'request_id': request_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values
    )

    # Sprint 58 s58-004: Session lifecycle audit log
    logger.info(
        "session_lifecycle",
        extra={
            "src_module": "callbacks",
            "operation": "trust_approved" if action == 'approve_trust' else "command_approved",
            "request_id": request_id,
            "user_id": user_id,
            "source_ip": source_ip,
            "command": command[:100],
            "command_status": cmd_status,
        }
    )

    return {
        'result': result,
        'paged': paged,
        'decision_latency_ms': decision_latency_ms,
    }


def _auto_execute_pending_requests(trust_scope: str, account_id: str, assume_role: str,
                                    trust_id: str, source: str = ''):
    """信任開啟後，自動執行同 trust_scope + account 的排隊中請求"""
    if not trust_scope:
        return

    table = _db.table

    # 查 pending 請求，用 status-created-index + filter by trust_scope + account
    response_data = table.query(
        IndexName='status-created-index',
        KeyConditionExpression='#status = :status',
        FilterExpression='trust_scope = :scope AND account_id = :account',
        ExpressionAttributeNames={
            '#status': 'status',
        },
        ExpressionAttributeValues={
            ':status': 'pending',
            ':scope': trust_scope,
            ':account': account_id,
        },
        ScanIndexForward=True,
        Limit=20,
    )

    items = response_data.get('Items', [])
    if not items:
        return

    executed = 0
    for item in items:
        req_id = item['request_id']
        cmd = item.get('command', '')
        reason = item.get('reason', '')
        item_source = item.get('source', source)
        item_assume_role = item.get('assume_role', assume_role)

        # SEC-013: 重跑 compliance check，不合規的 pending 命令拒絕執行
        try:
            from compliance_checker import check_compliance
            is_compliant, violation = check_compliance(cmd)
            if not is_compliant:
                logger.warning("Pending request failed compliance", extra={"src_module": "trust", "sec_rule": "SEC-013", "request_id": req_id, "violation_rule": violation.rule_id if violation else "unknown"})
                # 更新狀態為 compliance_rejected
                now_rej = int(time.time())
                table.update_item(
                    Key={'request_id': req_id},
                    UpdateExpression='SET #s = :s, compliance_rejected_at = :t, compliance_rule = :rule',
                    ExpressionAttributeNames={'#s': 'status'},
                    ExpressionAttributeValues={
                        ':s': 'compliance_rejected',
                        ':t': now_rej,
                        ':rule': violation.rule_id if violation else 'unknown',
                    },
                )
                continue
        except ImportError:
            pass  # compliance_checker 不存在時跳過

        # 執行命令
        try:
            send_chat_action('typing')
        except Exception as _e:  # noqa: BLE001 — fire-and-forget
            logger.debug("send_chat_action ignored: %s", _e, extra={"src_module": "callbacks", "operation": "send_chat_action"})
        result = execute_command(cmd, item_assume_role)
        cmd_status = 'failed' if _is_execute_failed(result) else 'success'
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions={'Status': cmd_status, 'Path': 'trust_callback'})
        paged = store_paged_output(req_id, result)

        # 更新 DynamoDB 狀態
        now = int(time.time())
        table.update_item(
            Key={'request_id': req_id},
            UpdateExpression='SET #s = :s, #r = :r, approved_at = :t, decision_type = :dt, decided_at = :da, command_status = :cs',
            ExpressionAttributeNames={'#s': 'status', '#r': 'result'},
            ExpressionAttributeValues={
                ':s': 'approved',
                ':r': paged['result'],
                ':t': now,
                ':dt': 'trust_auto_approved',
                ':da': now,
                ':cs': cmd_status,
            },
        )

        # Trust 計數 (s59-002: catch rate exceeded)
        try:
            new_count = increment_trust_command_count(trust_id)
        except TrustRateExceeded as exc:
            logger.warning("Trust rate exceeded during auto-execute: %s", exc, extra={"src_module": "callbacks_command", "operation": "auto_execute_pending", "trust_id": trust_id, "request_id": req_id})
            emit_metric('Bouncer', 'TrustRateExceeded', 1, dimensions={'Event': 'auto_execute'})
            # Skip this command, continue with others
            # Don't update status - leave as 'pending' for manual approval
            continue

        # 計算剩餘時間
        remaining = "~10:00"  # 剛建的 trust session，約 10 分鐘

        # 靜默通知
        send_trust_auto_approve_notification(
            cmd, trust_id, remaining, new_count, result,
            source=item_source,
            reason=reason,
        )

        # Track command in trust session for summary (sprint9-007-phase-a)
        is_failed = _is_execute_failed(result)
        track_command_executed(trust_id, cmd, not is_failed)

        # Audit log
        log_decision(
            table=table,
            request_id=generate_request_id(cmd),
            command=cmd,
            reason=reason,
            source=item_source,
            account_id=account_id,
            decision_type='trust_approved',
            mode='mcp',
            trust_session_id=trust_id,
        )

        executed += 1


def _handle_trust_session(
    trust_scope: str,
    account_id: str,
    user_id: str,
    source: str,
    assume_role: str,
    source_ip: str = '',
) -> str:
    """處理信任時段的建立與自動執行

    Args:
        trust_scope: 信任範圍
        account_id: 帳號 ID
        user_id: 使用者 ID
        source: 來源
        assume_role: IAM role
        source_ip: Telegram server IP (for IP binding, best-effort)

    Returns:
        str: 信任時段資訊字串 (trust_line)
    """
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

    # 查詢同 trust_scope 的 pending 請求（顯示 display_summary）
    # bouncer-trust-batch-flow (Approach B): show each pending request's
    # display_summary instead of just the count.
    pending_items = []
    try:
        pending_resp = _db.table.query(
            IndexName='status-created-index',
            KeyConditionExpression='#status = :status',
            FilterExpression='trust_scope = :scope AND account_id = :account',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'pending',
                ':scope': trust_scope,
                ':account': account_id,
            },
            ScanIndexForward=True,
            Limit=20,
        )
        pending_items = pending_resp.get('Items', [])
    except Exception:  # noqa: BLE001 — DDB query failure is non-fatal; logged with exc_info
        logger.warning("Failed to query pending items", extra={"src_module": "trust", "operation": "query_pending", "trust_scope": trust_scope}, exc_info=True)  # [TRUST] Failed to query pending items

    pending_count = len(pending_items)
    if pending_count > 0:
        trust_line += f"\n⚡ 自動執行 {pending_count} 個排隊請求："
        for pi in pending_items[:5]:
            summary = pi.get('display_summary') or pi.get('command', '')[:60]
            trust_line += f"\n  • {escape_markdown(str(summary))}"
        if pending_count > 5:
            trust_line += f"\n  _...及其他 {pending_count - 5} 個請求_"

    # 自動執行同 trust_scope + account 的排隊中請求
    try:
        _auto_execute_pending_requests(trust_scope, account_id, assume_role, trust_id, source)
    except (OSError, TimeoutError, ConnectionError, urllib.error.URLError, ClientError) as e:
        logger.error(f"[TRUST] Auto-execute pending error: {e}", extra={"src_module": "trust", "operation": "auto_execute_pending", "error": str(e)})
    return trust_line


def _format_approval_response(
    action: str,
    result: str,
    paged: dict,
    trust_line: str,
    request_id: str,
    info: dict,
    message_id: int,
) -> None:
    """格式化並發送審批通過的回應訊息

    Args:
        action: approve 或 approve_trust
        result: 執行結果
        paged: 分頁資訊
        trust_line: 信任時段資訊
        request_id: 請求 ID
        info: _format_command_info 返回的 dict
        message_id: Telegram message ID
    """
    max_preview = 800 if action == 'approve_trust' else 1000
    result_preview = result[:max_preview] if len(result) > max_preview else result

    if paged.get('paged'):
        truncate_notice = (
            f"\n\n📄 *共 {paged['total_pages']} 頁* ({paged['output_length']} 字元)\n"
            f"用 `bouncer_get_page` 查看更多，或點下方按鈕"
        )
        next_page_button = {
            'inline_keyboard': [[{
                'text': f'➡️ Next Page (2/{paged["total_pages"]})',
                'callback_data': f'show_page:{request_id}:2',
            }]]
        }
    else:
        if len(result) > max_preview:
            # Output was truncated but not paged (between max_preview and 4000 chars)
            truncate_notice = (
                f"\n\n✂️ *輸出已截斷*（顯示前 {max_preview} 字元，共 {len(result)} 字元）\n"
                f"用 `bouncer_get_page` 查看完整輸出"
            )
        else:
            truncate_notice = ""
        next_page_button = None

    failed = _is_execute_failed(result)
    if failed:
        if action == 'approve_trust':
            title = "❌ *已批准但執行失敗* + 🔓 *信任 10 分鐘*"
        else:
            title = "❌ *已批准但執行失敗*"
    else:
        if action == 'approve_trust':
            title = "✅ *已批准並執行* + 🔓 *信任 10 分鐘*"
        else:
            title = "✅ *已批准並執行*"

    result_emoji = "❌" if failed else "✅"

    # Send result message; append inline Next Page button when paged
    send_telegram_message_silent(
        f"{title}\n\n"
        f"🆔 *ID：* `{request_id}`\n"
        f"{info['source_line']}"
        f"{info['account_line']}"
        f"📋 *命令：*\n`{info['cmd_preview']}`\n\n"
        f"💬 *原因：* {info['safe_reason']}\n\n"
        f"📤 *結果：*\n```\n{result_preview}\n```{truncate_notice}{trust_line}",
        reply_markup=next_page_button,
    )
    # Overwrite the approval message (remove buttons from it)
    # s56-003: Add error handling for update_message to avoid 400 errors
    try:
        update_message(message_id, f"{result_emoji} *已執行* — 見下方結果", remove_buttons=True)
    except Exception as _exc:  # noqa: BLE001 — best-effort
        logger.debug("Post-execute update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "post_execute_update", "message_id": message_id})


def _handle_deny_callback(
    request_id: str,
    item: dict,
    callback_id: str,
    user_id: str,
    message_id: int,
    info: dict,
) -> None:
    """處理拒絕操作

    Args:
        request_id: 請求 ID
        item: DynamoDB item (需要 created_at)
        callback_id: Telegram callback ID
        user_id: 拒絕者 ID
        message_id: Telegram message ID
        info: _format_command_info 返回的 dict
    """
    table = _get_table()

    now = int(time.time())
    created_at = int(item.get('created_at', 0))
    decision_latency_ms = (now - created_at) * 1000 if created_at else 0
    if decision_latency_ms:
        emit_metric('Bouncer', 'DecisionLatency', decision_latency_ms, unit='Milliseconds', dimensions={'Action': 'deny'})

    answer_callback(callback_id, '❌ 已拒絕')
    _update_request_status(table, request_id, 'denied', user_id, extra_attrs={
        'decision_type': 'manual_denied',
        'decided_at': now,
        'decision_latency_ms': decision_latency_ms,
    })

    # Sprint 58 s58-004: Session lifecycle audit log
    logger.info(
        "session_lifecycle",
        extra={
            "src_module": "callbacks",
            "operation": "command_denied",
            "request_id": request_id,
            "user_id": user_id,
            "command": item.get('command', '')[:100],
        }
    )

    # s56-003: Add error handling for update_message to avoid 400 errors
    try:
        update_message(
            message_id,
            f"❌ *已拒絕*\n\n"
            f"🆔 *ID：* `{request_id}`\n"
            f"{info['source_line']}"
            f"{info['account_line']}"
            f"📋 *命令：*\n`{info['cmd_preview']}`\n\n"
            f"💬 *原因：* {info['safe_reason']}",
        )
    except Exception as _exc:  # noqa: BLE001 — best-effort
        logger.debug("Deny update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "deny_update", "message_id": message_id})


def handle_command_callback(action: str, request_id: str, item: dict, message_id: int, callback_id: str, user_id: str, *, source_ip: str = '') -> dict:
    """處理命令執行的審批 callback

    Args:
        source_ip: Telegram server IP from API GW event (for audit trail #74).
                   This is NOT the end-user IP — Lambda runs as a webhook.
    """
    # 解析請求資料
    parsed = _parse_command_callback_request(item)
    command = parsed['command']
    assume_role = parsed['assume_role']
    source = parsed['source']
    trust_scope = parsed['trust_scope']
    account_id = parsed['account_id']

    # 格式化顯示資訊
    info = _format_command_info(parsed)

    # SEC: verify approval has not expired
    import time as _time
    _item_ttl = int(item.get('ttl', 0))
    if _item_ttl and int(_time.time()) > _item_ttl and action in ('approve', 'approve_trust'):
        logger.warning("callback rejected: approval expired for %s", request_id, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        answer_callback(callback_id, '❌ 審批已過期，請重新發起請求')
        try:
            update_message(message_id, '❌ *審批已過期*\n\n`' + request_id + '`', remove_buttons=True)
        except Exception as _exc:  # noqa: BLE001 — best-effort
            logger.debug("TTL check update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "ttl_check", "request_id": request_id})
        return response(200, {'ok': True})

    if action in ('approve', 'approve_trust'):
        # Check if OTP required (risk_score >= threshold, non-trust approve only, not already verified)
        if action == 'approve' and not item.get('otp_verified'):
            # s56-001: Recalculate risk_score in callback instead of using DDB value
            # (DDB value may be 0 if smart_decision wasn't calculated during execute)
            from risk_scorer import calculate_risk
            risk_result = calculate_risk(command)
            risk_score = risk_result.score if risk_result else 0
            if risk_score >= OTP_RISK_THRESHOLD:
                from otp import generate_otp, create_otp_record

                otp_code = generate_otp()
                create_otp_record(request_id, user_id, otp_code, message_id)

                try:
                    send_telegram_message_to(
                        user_id,
                        f"🔐 *Bouncer OTP 驗證碼*\n\n"
                        f"命令：`{command[:100]}`\n\n"
                        f"驗證碼：`{otp_code}`\n\n"
                        f"⏰ 有效期：5 分鐘\n"
                        f"輸入：`/otp {otp_code}` 確認執行",
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.warning("Failed to send OTP DM: %s", e)
                    # If can't send DM, abort and inform
                    answer_callback(callback_id, '❌ 無法發送 OTP，請確認 Bot DM 未被封鎖')
                    # s56-003: Add error handling for update_message
                    try:
                        update_message(message_id, f"❌ *OTP 發送失敗*\n\n`{request_id}`\n\n請確認已開啟 Bot DM，然後重新審批", remove_buttons=True)
                    except Exception as _exc:  # noqa: BLE001 — best-effort
                        logger.debug("OTP fail update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "otp_fail_update"})
                    return response(200, {'ok': True})

                answer_callback(callback_id, '🔐 OTP 已發送至 DM，請在 5 分鐘內確認')
                # s56-003: Add error handling for update_message
                try:
                    update_message(
                        message_id,
                        f"⏳ *等待 OTP 驗證*\n\n"
                        f"📋 *請求 ID：* `{request_id}`\n"
                        f"🔐 *OTP 已發送至 DM*\n\n"
                        f"請輸入：`/otp XXXXXX`",
                        remove_buttons=True,
                    )
                except Exception as _exc:  # noqa: BLE001 — best-effort
                    logger.debug("OTP wait update_message failed (non-critical): %s", _exc, extra={"src_module": "callbacks", "operation": "otp_wait_update"})
                return response(200, {'ok': True})

        if is_dangerous(command):
            answer_callback(callback_id, '⚠️ 高危操作確認：正在執行...', show_alert=True)
        elif action == 'approve_trust':
            answer_callback(callback_id, '✅ 執行中 + 🔓 信任啟動')
        else:
            answer_callback(callback_id, '✅ 執行中...')

        # Immediate feedback: remove buttons before execute_command (best-effort)
        try:
            update_message(
                message_id,
                f"⏳ *執行中...*\n\n"
                f"📋 *請求 ID：* `{request_id}`\n"
                f"{info['source_line']}"
                f"{info['account_line']}"
                f"📋 *命令：*\n`{info['cmd_preview']}`\n\n"
                f"💬 *原因：* {info['safe_reason']}",
                remove_buttons=True,
            )
        except Exception as e:  # noqa: BLE001 — s56-003: catch all exceptions including 400 errors
            logger.warning(f"[execute] Immediate feedback update_message failed (non-critical): {e}")

        # 執行命令並存入結果
        try:
            send_chat_action('typing')
        except Exception as _e:  # noqa: BLE001 — fire-and-forget
            logger.debug("send_chat_action ignored: %s", _e, extra={"src_module": "callbacks", "operation": "send_chat_action"})
        exec_result = _execute_and_store_result(
            command, assume_role, request_id, item, user_id, source_ip, action
        )
        result = exec_result['result']
        paged = exec_result['paged']

        # 信任模式
        trust_line = ""
        if action == 'approve_trust':
            trust_line = _handle_trust_session(trust_scope, account_id, user_id, source, assume_role, source_ip=source_ip)

        # 格式化並發送回應訊息
        _format_approval_response(action, result, paged, trust_line, request_id, info, message_id)

    elif action == 'deny':
        _handle_deny_callback(request_id, item, callback_id, user_id, message_id, info)

    return response(200, {'ok': True})
