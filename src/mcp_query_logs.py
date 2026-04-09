"""
Bouncer - CloudWatch Logs 查詢工具

bouncer_query_logs:      查詢 CloudWatch Log Insights（需在允許名單中）
bouncer_logs_allowlist:  管理允許查詢的 log group 名單（add/remove/list/add_batch）
"""
from __future__ import annotations

import json
import os
import re
import time

import boto3
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from utils import mcp_result, mcp_error, generate_request_id
from accounts import get_account
from db import table
from telegram import send_telegram_message, escape_markdown
from notifications import post_notification_setup
from constants import DEFAULT_ACCOUNT_ID

logger = Logger(service="bouncer")

# ============================================================================
# Constants
# ============================================================================

# Maximum time range: 30 days
MAX_TIME_RANGE_SECONDS = 30 * 24 * 60 * 60

# Result limits
MAX_RESULTS_LIMIT = 1000
DEFAULT_RESULTS_LIMIT = 100

# Lambda response size limit (~5.5 MB safety margin within 6 MB)
RESPONSE_SIZE_LIMIT = 5_500_000

# Allowed log group prefixes (security: only these prefixes are permitted)
ALLOWED_LOG_GROUP_PREFIXES = (
    '/aws/lambda/',
    '/aws/ecs/',
    '/aws/apigateway/',
    '/aws/rds/',
    '/aws/eks/',
    '/aws/codebuild/',
    '/aws/elasticbeanstalk/',
    '/ecs/',
    'bouncer',
    'API-Gateway-Execution-Logs',
    'aws-waf-logs',
)

# Log group name validation regex (alphanum, slash, dash, dot, underscore)
LOG_GROUP_NAME_RE = re.compile(r'^[\w.\/\-]+$')

# filter_pattern sanitization: only allow safe chars (#1 injection fix)
FILTER_PATTERN_SAFE_RE = re.compile(r'[^a-zA-Z0-9_.\-\s:@/]')

# Query polling
QUERY_POLL_INTERVAL = 1   # seconds
QUERY_MAX_POLL_TIME = 20  # seconds (#4: reduced from 25 to leave margin for Lambda 30s limit)

# Approval timeout for query_logs fallback
QUERY_LOGS_APPROVAL_TIMEOUT = 1800  # 30 minutes

# Default allowlist entries (auto-initialized on first use)
DEFAULT_ALLOWLIST = (
    '/aws/lambda/bouncer-prod-BouncerFunction',
    '/aws/lambda/bouncer-deployer-prod-NotifierFunction',
)

# Lazy flag: default allowlist init
_default_allowlist_initialized = False

# Module-level TTL cache for allowlist checks (#13: avoid redundant DDB reads)
_allowlist_cache: dict[str, tuple[bool, float]] = {}
_ALLOWLIST_CACHE_TTL = 300  # 5 minutes


# ============================================================================
# Validation helpers
# ============================================================================

def _validate_log_group_name(log_group: str) -> tuple[bool, str]:
    """Validate log group name format and prefix."""
    if not log_group:
        return False, 'log_group 不能為空'

    if len(log_group) > 512:
        return False, 'log_group 長度不能超過 512 字元'

    if not LOG_GROUP_NAME_RE.match(log_group):
        return False, f'log_group 包含不允許的字元: {log_group}'

    # Check against allowed prefixes
    if not any(log_group.startswith(p) for p in ALLOWED_LOG_GROUP_PREFIXES):
        allowed_str = ', '.join(ALLOWED_LOG_GROUP_PREFIXES)
        return False, f'log_group 必須以下列前綴開頭: {allowed_str}'

    return True, ''


def _resolve_account(account: str) -> tuple[str, str | None, str | None]:
    """Resolve account ID and assume_role_arn.

    Returns:
        (account_id, assume_role_arn, error_message)
    """
    account_id = account or DEFAULT_ACCOUNT_ID
    assume_role_arn = None

    if account_id and account_id != DEFAULT_ACCOUNT_ID:
        account_info = get_account(account_id)
        if not account_info:
            return account_id, None, f'帳號 {account_id} 未設定'
        assume_role_arn = account_info.get('role_arn')
        if not assume_role_arn:
            return account_id, None, f'帳號 {account_id} 未設定 role_arn'

    return account_id, assume_role_arn, None


# ============================================================================
# CloudWatch Logs client
# ============================================================================

def _get_logs_client(region: str = None, assume_role_arn: str = None):
    """Get CloudWatch Logs boto3 client, optionally with assumed role."""
    region = region or os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')

    if assume_role_arn:
        sts = boto3.client('sts')
        assumed = sts.assume_role(
            RoleArn=assume_role_arn,
            RoleSessionName='bouncer-query-logs',
            DurationSeconds=900,
        )
        creds = assumed['Credentials']
        return boto3.client(
            'logs',
            region_name=region,
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
        )

    return boto3.client('logs', region_name=region)


def _verify_log_group_exists(log_group: str, region: str = '', assume_role_arn: str = None) -> tuple[bool, str]:
    """Verify that a CloudWatch log group exists.

    Returns:
        (exists, error_message) — exists=True if found, else error_message explains why.
    """
    try:
        logs_client = _get_logs_client(region=region, assume_role_arn=assume_role_arn)
        resp = logs_client.describe_log_groups(logGroupNamePrefix=log_group, limit=1)
        for lg in resp.get('logGroups', []):
            if lg.get('logGroupName') == log_group:
                return True, ''
        return False, f'Log group "{log_group}" 不存在'
    except ClientError as e:
        code = e.response['Error']['Code']
        msg = e.response['Error']['Message']
        logger.warning("Failed to verify log group existence: %s: %s", code, msg,
                       extra={"src_module": "mcp_query_logs", "operation": "verify_log_group",
                              "log_group": log_group, "error": f'{code}: {msg}'})
        return False, f'無法驗證 log group 是否存在: {code}: {msg}'


# ============================================================================
# Allowlist helpers (DDB-backed)
# ============================================================================

def _allowlist_key(account_id: str, log_group: str) -> str:
    """Generate DDB key for allowlist entry."""
    return f'LOGS_ALLOWLIST#{account_id}#{log_group}'


def _check_allowlist(account_id: str, log_group: str) -> bool:
    """Check if log_group is in the allowlist for this account.

    Uses module-level TTL cache to avoid redundant DDB reads (#13).
    """
    key = _allowlist_key(account_id, log_group)

    cached = _allowlist_cache.get(key)
    if cached:
        found, ts = cached
        if time.time() - ts < _ALLOWLIST_CACHE_TTL:
            return found

    try:
        result = table.get_item(Key={'request_id': key})
        found = 'Item' in result
    except ClientError:
        found = False

    _allowlist_cache[key] = (found, time.time())
    return found


def _add_to_allowlist(account_id: str, log_group: str, added_by: str = '') -> None:
    """Add log_group to allowlist."""
    table.put_item(Item={
        'request_id': _allowlist_key(account_id, log_group),
        'type': 'logs_allowlist',
        'account_id': account_id,
        'log_group': log_group,
        'added_by': added_by,
        'created_at': int(time.time()),
        # expires_at=0 → never expires; field present for type-expires-at GSI compat
        'expires_at': 0,
    })


def _remove_from_allowlist(account_id: str, log_group: str) -> bool:
    """Remove log_group from allowlist. Returns True if existed."""
    key = _allowlist_key(account_id, log_group)
    try:
        table.delete_item(
            Key={'request_id': key},
            ConditionExpression='attribute_exists(request_id)',
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise


def _list_allowlist(account_id: str) -> list:
    """List all allowlist entries for an account.

    TODO(#5): FilterExpression scans entire GSI partition — consider adding
    a GSI with account_id as PK if allowlist grows large.
    """
    try:
        items: list = []
        kwargs: dict = {
            'IndexName': 'type-expires-at-index',
            'KeyConditionExpression': '#t = :t',
            'FilterExpression': 'account_id = :aid',
            'ExpressionAttributeNames': {'#t': 'type'},
            'ExpressionAttributeValues': {
                ':t': 'logs_allowlist',
                ':aid': account_id,
            },
        }
        while True:
            result = table.query(**kwargs)
            items.extend(result.get('Items', []))
            last_key = result.get('LastEvaluatedKey')
            if not last_key:
                break
            kwargs['ExclusiveStartKey'] = last_key
        return items
    except ClientError as e:
        logger.error("Failed to list allowlist: %s", e,
                     extra={"src_module": "mcp_query_logs", "operation": "list_allowlist",
                            "account_id": account_id, "error": str(e)})
        return []


def initialize_default_allowlist(account_id: str = None) -> None:
    """Initialize default allowlist entries if they don't exist (idempotent)."""
    global _default_allowlist_initialized  # noqa: PLW0603
    if _default_allowlist_initialized:
        return

    aid = account_id or DEFAULT_ACCOUNT_ID
    if not aid:
        return

    for lg in DEFAULT_ALLOWLIST:
        if not _check_allowlist(aid, lg):
            try:
                _add_to_allowlist(aid, lg, added_by='system_init')
                logger.info("Initialized default allowlist entry: %s", lg,
                            extra={"src_module": "mcp_query_logs",
                                   "operation": "initialize_default_allowlist",
                                   "account_id": aid, "log_group": lg})
            except ClientError as e:
                logger.warning("Failed to init default allowlist entry %s: %s", lg, e,
                               extra={"src_module": "mcp_query_logs",
                                      "operation": "initialize_default_allowlist",
                                      "error": str(e)})

    _default_allowlist_initialized = True


# ============================================================================
# MCP Tool: bouncer_query_logs
# ============================================================================

def _parse_time(value, now: int) -> int:
    """Parse time value: unix timestamp (int/str) or relative (-1h, -30m, -7d, now)."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip().lower()
    if s == 'now':
        return now
    # Relative time: -1h, -30m, -7d, -3600s
    m = re.match(r'^-(\d+)([smhd])$', s)
    if m:
        num = int(m.group(1))
        if num == 0:
            return now
        unit = m.group(2)
        multiplier = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
        return now - num * multiplier[unit]
    # Try as int string
    try:
        return int(s)
    except ValueError:
        raise ValueError(f'Invalid time format: {value}. Use unix timestamp, "now", or relative (-1h, -30m, -7d)')


def _format_time_range(start_time: int, end_time: int) -> str:
    """Format time range as human-readable string for Telegram display."""
    import datetime
    start_dt = datetime.datetime.fromtimestamp(start_time, tz=datetime.timezone.utc)
    end_dt = datetime.datetime.fromtimestamp(end_time, tz=datetime.timezone.utc)
    if start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%H:%M')} UTC"
    return f"{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')} UTC"


def execute_log_insights(log_group: str, query_with_limit: str, start_time: int, end_time: int,
                         region: str = '', assume_role_arn: str = None, account_id: str = '') -> dict:
    """Execute CloudWatch Log Insights query and return results.

    Returns:
        dict with keys:
            status: 'complete', 'running', 'error', or lowercase CloudWatch status
            query_id: str (if query was started)
            log_group, account_id: echo back
            records_matched: int (if complete)
            statistics: dict (if complete)
            results: list of dicts (if complete)
            error: str (if status == 'error')
    """
    try:
        logs_client = _get_logs_client(region=region, assume_role_arn=assume_role_arn)

        start_resp = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_time,
            endTime=end_time,
            queryString=query_with_limit,
        )
        query_id = start_resp['queryId']

        logger.info("Log query started: query_id=%s, log_group=%s",
                     query_id, log_group,
                     extra={"src_module": "mcp_query_logs", "operation": "start_query",
                            "query_id": query_id, "log_group": log_group,
                            "account_id": account_id})

        # Poll for results
        poll_start = time.time()
        status = 'Running'
        results = []
        stats = {}

        while status in ('Scheduled', 'Running') and (time.time() - poll_start) < QUERY_MAX_POLL_TIME:
            time.sleep(QUERY_POLL_INTERVAL)
            get_resp = logs_client.get_query_results(queryId=query_id)
            status = get_resp.get('status', '')
            results = get_resp.get('results', [])
            stats = get_resp.get('statistics', {})

        if status in ('Scheduled', 'Running'):
            logger.info("Log query still running after poll timeout: query_id=%s", query_id,
                         extra={"src_module": "mcp_query_logs", "operation": "query_timeout",
                                "query_id": query_id})
            return {
                'status': 'running',
                'query_id': query_id,
                'poll_seconds': round(time.time() - poll_start, 1),
            }

        # Format results
        formatted_results = []
        for row in results:
            entry = {field['field']: field['value'] for field in row}
            formatted_results.append(entry)

        logger.info("Log query complete: query_id=%s, records=%d",
                     query_id, len(formatted_results),
                     extra={"src_module": "mcp_query_logs", "operation": "query_complete",
                            "query_id": query_id, "records": len(formatted_results)})

        return {
            'status': 'complete' if status == 'Complete' else status.lower(),
            'query_id': query_id,
            'log_group': log_group,
            'account_id': account_id,
            'records_matched': len(formatted_results),
            'statistics': {
                'records_matched': stats.get('recordsMatched', 0),
                'records_scanned': stats.get('recordsScanned', 0),
                'bytes_scanned': stats.get('bytesScanned', 0),
            },
            'results': formatted_results,
        }

    except ClientError as e:
        code = e.response['Error']['Code']
        msg = e.response['Error']['Message']
        logger.error("Log query error: %s: %s", code, msg,
                     extra={"src_module": "mcp_query_logs", "operation": "query_error",
                            "log_group": log_group, "error": f'{code}: {msg}'})
        return {'status': 'error', 'error': f'CloudWatch Logs 錯誤: {code}: {msg}'}
    except Exception as e:  # noqa: BLE001 — query execution entry point
        logger.error("Log query unexpected error: %s", e,
                     extra={"src_module": "mcp_query_logs", "operation": "query_error",
                            "log_group": log_group, "error": str(e)})
        return {'status': 'error', 'error': f'查詢錯誤: {str(e)}'}


def mcp_tool_query_logs(req_id: str, arguments: dict) -> dict:
    """MCP tool handler: CloudWatch Log Insights query."""
    logger.info("Tool called", extra={
        "src_module": "mcp_query_logs", "operation": "tool_called",
        "tool": "bouncer_query_logs"})

    # --- Extract parameters ---
    log_group = arguments.get('log_group', '').strip()
    query = arguments.get('query', '').strip()
    filter_pattern = arguments.get('filter_pattern', '').strip()
    start_time = arguments.get('start_time')
    end_time = arguments.get('end_time')
    limit = arguments.get('limit', DEFAULT_RESULTS_LIMIT)
    account = arguments.get('account', '')
    region = arguments.get('region', '')

    # --- Validate log_group ---
    if not log_group:
        return mcp_error(req_id, -32602, 'Missing required parameter: log_group')

    valid, err_msg = _validate_log_group_name(log_group)
    if not valid:
        return mcp_error(req_id, -32602, err_msg)

    # --- Sanitize filter_pattern (#1: prevent Log Insights injection) ---
    if filter_pattern:
        filter_pattern = FILTER_PATTERN_SAFE_RE.sub('', filter_pattern).strip()
        if not filter_pattern:
            return mcp_error(req_id, -32602,
                             'filter_pattern 包含不允許的字元（僅允許英數字、底線、點、減號、空白、冒號、@、/）')

    # --- Build query string ---
    if not query:
        if filter_pattern:
            query = (
                f'fields @timestamp, @message'
                f' | filter @message like /{filter_pattern}/'
                f' | sort @timestamp desc'
            )
        else:
            query = 'fields @timestamp, @message | sort @timestamp desc'

    # --- Validate & inject limit ---
    try:
        limit = min(max(1, int(limit)), MAX_RESULTS_LIMIT)
    except (ValueError, TypeError):
        return mcp_error(req_id, -32602, f'limit 必須是有效整數，收到: {limit}')
    if 'limit' not in query.lower():
        query_with_limit = f'{query} | limit {limit}'
    else:
        query_with_limit = query

    # --- Validate time range ---
    now = int(time.time())
    start_time = _parse_time(start_time, now) if start_time else now - 3600
    end_time = _parse_time(end_time, now) if end_time else now

    if end_time - start_time > MAX_TIME_RANGE_SECONDS:
        return mcp_error(req_id, -32602,
                         f'時間範圍不能超過 30 天（{MAX_TIME_RANGE_SECONDS} 秒）')
    if start_time >= end_time:
        return mcp_error(req_id, -32602, 'start_time 必須小於 end_time')

    # --- Resolve account ---
    account_id, assume_role_arn, err = _resolve_account(account)
    if err:
        return mcp_error(req_id, -32602, err)

    # --- Lazy init default allowlist ---
    initialize_default_allowlist(account_id)

    # --- Check allowlist ---
    # NOTE(#11): self-approval is by design — the operator who queries is
    # the same person who approves via Telegram.  Accepted risk for this tool.
    if not _check_allowlist(account_id, log_group):
        # Verify log group exists before sending approval request
        exists, verify_err = _verify_log_group_exists(log_group, region=region, assume_role_arn=assume_role_arn)
        if not exists:
            logger.info("Log group does not exist, skipping approval: %s (account=%s)",
                        log_group, account_id,
                        extra={"src_module": "mcp_query_logs", "operation": "log_group_not_found",
                               "log_group": log_group, "account_id": account_id})
            return mcp_error(req_id, -32602, verify_err)

        logger.info("Log group not in allowlist, sending approval request: %s (account=%s)",
                     log_group, account_id,
                     extra={"src_module": "mcp_query_logs", "operation": "allowlist_check",
                            "log_group": log_group, "account_id": account_id})

        # Create DDB pending request
        approval_req_id = generate_request_id(f'query_logs:{log_group}')
        now = int(time.time())
        expires_at = now + QUERY_LOGS_APPROVAL_TIMEOUT
        time_range_str = _format_time_range(start_time, end_time)

        table.put_item(Item={
            'request_id': approval_req_id,
            'type': 'query_logs_approval',
            'action': 'query_logs',
            'status': 'pending_approval',
            'log_group': log_group,
            'account_id': account_id,
            'assume_role_arn': assume_role_arn or '',
            'query': query_with_limit,
            'start_time': start_time,
            'end_time': end_time,
            'region': region or '',
            'created_at': now,
            'ttl': expires_at,
            'expires_at': expires_at,
        })

        # Send Telegram notification with approval buttons
        tg_text = (
            f"📋 *Log 查詢請求*\n\n"
            f"📁 *Log Group：* `{escape_markdown(log_group)}`\n"
            f"🏦 *Account：* `{account_id}`\n"
            f"⏰ *時間範圍：* {escape_markdown(time_range_str)}\n\n"
            f"🆔 `{approval_req_id}`\n"
            f"⏰ *30 分鐘後過期*"
        )
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '✅ 一次性允許', 'callback_data': f'approve_query_logs:{approval_req_id}'},
                    {'text': '📋 加入允許名單', 'callback_data': f'approve_add_allowlist:{approval_req_id}'},
                ],
                [
                    {'text': '❌ 拒絕', 'callback_data': f'deny_query_logs:{approval_req_id}'},
                ],
            ]
        }

        tg_result = send_telegram_message(tg_text, keyboard)
        tg_message_id = (tg_result or {}).get('result', {}).get('message_id')
        if tg_message_id:
            post_notification_setup(approval_req_id, tg_message_id, expires_at)

        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': approval_req_id,
                'log_group': log_group,
                'account_id': account_id,
                'message': (
                    f'log_group "{log_group}" 不在帳號 {account_id} 的允許名單中。'
                    f'已發送審批請求到 Telegram，等待審批後將自動執行查詢。'
                ),
            }, ensure_ascii=False)}]
        })

    # --- Execute Log Insights query ---
    result_data = execute_log_insights(
        log_group=log_group, query_with_limit=query_with_limit,
        start_time=start_time, end_time=end_time,
        region=region, assume_role_arn=assume_role_arn, account_id=account_id,
    )

    if result_data['status'] == 'running':
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'running',
                'query_id': result_data['query_id'],
                'message': (
                    '查詢仍在執行中。可用 bouncer_execute_native 呼叫 '
                    'logs.get_query_results(queryId=query_id) 取得結果'
                ),
                'poll_seconds': result_data.get('poll_seconds', 0),
            }, ensure_ascii=False)}]
        })

    if result_data['status'] == 'error':
        return mcp_error(req_id, -32603, result_data.get('error', 'Unknown error'))

    # Truncate if response exceeds 6 MB Lambda limit (#2/#6: estimate-based)
    result_json = json.dumps(result_data, ensure_ascii=False)
    result_bytes = len(result_json.encode('utf-8'))
    if result_bytes > RESPONSE_SIZE_LIMIT:
        results = result_data['results']
        # Estimate overhead (everything except the results array)
        overhead_data = {**result_data, 'results': [], 'truncated': True, 'records_returned': 0}
        overhead_size = len(json.dumps(overhead_data, ensure_ascii=False).encode('utf-8'))
        available = RESPONSE_SIZE_LIMIT - overhead_size

        # Estimate per-record size from sample
        sample_n = min(10, len(results))
        if sample_n > 0:
            sample_bytes = len(json.dumps(results[:sample_n], ensure_ascii=False).encode('utf-8'))
            avg_size = sample_bytes / sample_n
            keep = max(1, int(available / avg_size * 0.9))  # 10% safety margin
            keep = min(keep, len(results))
        else:
            keep = 0

        result_data['results'] = results[:keep]
        result_data['truncated'] = True
        result_data['records_returned'] = len(result_data['results'])

        # Single fallback: if estimate was wrong, halve until it fits
        while (result_data['results']
               and len(json.dumps(result_data, ensure_ascii=False).encode('utf-8')) > RESPONSE_SIZE_LIMIT):
            result_data['results'] = result_data['results'][:len(result_data['results']) // 2]
            result_data['records_returned'] = len(result_data['results'])

        result_json = json.dumps(result_data, ensure_ascii=False)

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': result_json}]
    })


# ============================================================================
# MCP Tool: bouncer_logs_allowlist
# ============================================================================

def mcp_tool_logs_allowlist(req_id: str, arguments: dict) -> dict:
    """MCP tool handler: manage logs query allowlist."""
    logger.info("Tool called", extra={
        "src_module": "mcp_query_logs", "operation": "tool_called",
        "tool": "bouncer_logs_allowlist"})

    action = arguments.get('action', '').strip().lower()
    account = arguments.get('account', '')
    account_id = account or DEFAULT_ACCOUNT_ID

    if not account_id:
        return mcp_error(req_id, -32602, '無法確定帳號 ID（DEFAULT_ACCOUNT_ID 未設定）')

    # ---- list ----
    if action == 'list':
        items = _list_allowlist(account_id)
        entries = [
            {
                'log_group': item.get('log_group', ''),
                'added_by': item.get('added_by', ''),
                'created_at': int(item.get('created_at', 0)),
            }
            for item in items
        ]
        logger.info("Allowlist listed: account=%s, count=%d", account_id, len(entries),
                     extra={"src_module": "mcp_query_logs", "operation": "allowlist_list",
                            "account_id": account_id, "count": len(entries)})
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'account_id': account_id,
                'count': len(entries),
                'entries': entries,
            }, ensure_ascii=False)}]
        })

    # ---- add ----
    if action == 'add':
        log_group = arguments.get('log_group', '').strip()
        valid, err_msg = _validate_log_group_name(log_group)
        if not valid:
            return mcp_error(req_id, -32602, err_msg)

        # Verify log group exists before adding to allowlist
        region = arguments.get('region', '')
        account_id_resolved, assume_role_arn, acct_err = _resolve_account(account)
        exists, verify_err = _verify_log_group_exists(
            log_group, region=region,
            assume_role_arn=assume_role_arn if not acct_err else None,
        )
        if not exists:
            logger.info("Log group does not exist, rejecting allowlist add: %s (account=%s)",
                        log_group, account_id,
                        extra={"src_module": "mcp_query_logs", "operation": "allowlist_add_rejected",
                               "log_group": log_group, "account_id": account_id})
            return mcp_error(req_id, -32602, verify_err)

        source = arguments.get('source', '')
        _add_to_allowlist(account_id, log_group, added_by=source)

        logger.info("Allowlist entry added: %s for account %s", log_group, account_id,
                     extra={"src_module": "mcp_query_logs", "operation": "allowlist_add",
                            "account_id": account_id, "log_group": log_group})
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'added',
                'account_id': account_id,
                'log_group': log_group,
            }, ensure_ascii=False)}]
        })

    # ---- remove ----
    if action == 'remove':
        log_group = arguments.get('log_group', '').strip()
        if not log_group:
            return mcp_error(req_id, -32602, 'Missing required parameter: log_group')

        existed = _remove_from_allowlist(account_id, log_group)
        status = 'removed' if existed else 'not_found'

        logger.info("Allowlist entry %s: %s for account %s", status, log_group, account_id,
                     extra={"src_module": "mcp_query_logs", "operation": "allowlist_remove",
                            "account_id": account_id, "log_group": log_group})
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': status,
                'account_id': account_id,
                'log_group': log_group,
            }, ensure_ascii=False)}]
        })

    # ---- add_batch ----
    if action == 'add_batch':
        log_groups = arguments.get('log_groups', [])
        if not log_groups:
            return mcp_error(req_id, -32602, 'Missing required parameter: log_groups')
        if len(log_groups) > 50:
            return mcp_error(req_id, -32602, 'log_groups 最多 50 個')

        source = arguments.get('source', '')
        region = arguments.get('region', '')
        _, assume_role_arn_batch, _ = _resolve_account(account)
        added = []
        errors = []
        for lg in log_groups:
            lg_str = lg.strip() if isinstance(lg, str) else str(lg)
            valid, err_msg = _validate_log_group_name(lg_str)
            if not valid:
                errors.append({'log_group': lg_str, 'error': err_msg})
                continue
            exists, verify_err = _verify_log_group_exists(
                lg_str, region=region, assume_role_arn=assume_role_arn_batch,
            )
            if not exists:
                errors.append({'log_group': lg_str, 'error': verify_err})
                continue
            _add_to_allowlist(account_id, lg_str, added_by=source)
            added.append(lg_str)

        logger.info("Allowlist batch add: %d added, %d errors for account %s",
                     len(added), len(errors), account_id,
                     extra={"src_module": "mcp_query_logs", "operation": "allowlist_add_batch",
                            "account_id": account_id, "added_count": len(added),
                            "error_count": len(errors)})
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'batch_complete',
                'account_id': account_id,
                'added': added,
                'errors': errors,
            }, ensure_ascii=False)}]
        })

    # ---- unknown action ----
    return mcp_error(req_id, -32602,
                     f'Unknown action: {action}. 支援的 action: add, remove, list, add_batch')
