"""
Bouncer - 工具函數模組
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger
from constants import AUDIT_TTL_SHORT, AUDIT_TTL_LONG
import telegram as telegram_module

logger = Logger(service="bouncer")


# ============================================================================
# Data Classes (shared across modules to avoid circular imports)
# ============================================================================

@dataclass
class RiskFactor:
    """
    單一風險因素，用於追蹤評分來源

    Attributes:
        name: 因素名稱（人類可讀）
        category: 因素類別 (verb/parameter/context/account)
        raw_score: 原始分數（0-100）
        weighted_score: 加權後的分數
        weight: 權重（0-1）
        details: 額外資訊（如：哪個參數、哪個規則）
    """
    name: str
    category: str
    raw_score: int
    weighted_score: float
    weight: float
    details: Optional[str] = None

    def __post_init__(self):
        """確保分數在有效範圍內"""
        self.raw_score = max(0, min(100, self.raw_score))
        self.weighted_score = max(0.0, min(100.0, self.weighted_score))


def sanitize_filename(filename: str, keep_path: bool = False) -> str:
    """消毒檔名，移除危險字元（防 path traversal）。

    Args:
        filename:  原始檔名（可包含路徑）
        keep_path: True 時保留 sub-directory 結構（用於 presigned URL）；
                   False 時只保留 basename（用於一般上傳）

    Returns:
        安全的檔名字串，不含危險字元。若結果為空則回傳 'unnamed'。
    """
    # Remove null bytes
    filename = filename.replace('\x00', '')
    # Normalise separators
    filename = filename.replace('\\', '/')

    if keep_path:
        # Resolve path-traversal components segment by segment
        clean_parts = []
        for part in filename.split('/'):
            part = part.replace('..', '')
            part = part.lstrip('. ')
            part = re.sub(r'[^\w\-.]', '_', part)
            if part:
                clean_parts.append(part)
        return '/'.join(clean_parts) or 'unnamed'
    else:
        # Only keep basename (strip directory separators)
        filename = filename.rsplit('/', 1)[-1]
        filename = filename.replace('..', '')
        filename = filename.lstrip('. ')
        filename = re.sub(r'[^\w\-.]', '_', filename)
        return filename or 'unnamed'


def format_size_human(size_bytes: int) -> str:
    """格式化檔案大小為人類可讀格式"""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes} bytes"


def build_info_lines(
    source: str = None,
    context: str = None,
    account_name: str = None,
    account_id: str = None,
    reason: str = None,
    *,
    bold: bool = True,
) -> str:
    """Build common Telegram message info lines.

    Automatically escapes user-input fields (source, context,
    account_name, reason) for Telegram Markdown V1.
    Callers should **not** pre-escape these values.

    Args:
        source: 請求來源（user input — will be escaped）
        context: 任務描述（user input — will be escaped）
        account_name: 帳號名稱（user input — will be escaped）
        account_id: 帳號 ID（placed in inline code — not escaped）
        reason: 原因（user input — will be escaped）
        bold: True 使用 markdown *粗體*，False 使用純文字

    Returns:
        多行字串（每行結尾含 ``\\n``），可直接嵌入 f-string
    """
    # Escape user-input fields (inline code fields like account_id are not escaped)
    if source:
        source = telegram_module.escape_markdown(source)
    if context:
        context = telegram_module.escape_markdown(context)
    if account_name:
        account_name = telegram_module.escape_markdown(account_name)
    if reason:
        reason = telegram_module.escape_markdown(reason)

    lines: list[str] = []
    if bold:
        if source:
            lines.append(f"🤖 *來源：* {source}")
        if context:
            lines.append(f"📝 *任務：* {context}")
        if account_name and account_id:
            lines.append(f"🏦 *帳號：* `{account_id}` ({account_name})")
        if reason:
            lines.append(f"💬 *原因：* {reason}")
    else:
        if source:
            lines.append(f"🤖 來源： {source}")
        if context:
            lines.append(f"📝 任務： {context}")
        if account_name and account_id:
            lines.append(f"🏦 帳號： `{account_id}` ({account_name})")
        if reason:
            lines.append(f"💬 原因： {reason}")
    return "\n".join(lines) + "\n" if lines else ""


def generate_display_summary(action: str, **kwargs) -> str:
    """Generate a human-readable display_summary for a DynamoDB approval request item.

    Args:
        action: Request type ('execute', 'upload', 'upload_batch',
                'add_account', 'remove_account', 'deploy')
        **kwargs: Action-specific fields:
            - command: str (for execute)
            - filename: str (for upload)
            - content_size: int (for upload)
            - file_count: int (for upload_batch)
            - total_size: int (for upload_batch)
            - account_name: str (for add/remove_account)
            - account_id: str (for add/remove_account)
            - project_id: str (for deploy)

    Returns:
        Human-readable summary string, ≤100 chars
    """
    if action == 'execute' or not action:
        command = kwargs.get('command', '')
        return command[:100] if command else '(empty command)'

    if action == 'upload':
        filename = kwargs.get('filename', 'unknown')
        content_size = kwargs.get('content_size')
        if content_size is not None:
            size_str = format_size_human(int(content_size))
            return f"upload: {filename} ({size_str})"
        return f"upload: {filename}"

    if action == 'upload_batch':
        file_count = kwargs.get('file_count')
        total_size = kwargs.get('total_size')
        count_str = str(file_count) if file_count else 'unknown'
        if total_size is not None:
            size_str = format_size_human(int(total_size))
            return f"upload_batch ({count_str} 個檔案, {size_str})"
        return f"upload_batch ({count_str} 個檔案)"

    if action == 'add_account':
        account_name = kwargs.get('account_name', '')
        account_id = kwargs.get('account_id', '')
        if account_name and account_id:
            return f"add_account: {account_name} ({account_id})"
        return f"add_account: {account_id or account_name or 'unknown'}"

    if action == 'remove_account':
        account_name = kwargs.get('account_name', '')
        account_id = kwargs.get('account_id', '')
        if account_name and account_id:
            return f"remove_account: {account_name} ({account_id})"
        return f"remove_account: {account_id or account_name or 'unknown'}"

    if action == 'deploy':
        project_id = kwargs.get('project_id', 'unknown project')
        return f"deploy: {project_id}"

    # Fallback for unknown action types
    return action or '(unknown)'


def get_header(headers: dict, key: str) -> Optional[str]:
    """Case-insensitive header lookup for API Gateway compatibility"""
    if headers is None:
        return None
    if key in headers:
        return headers[key]
    lower_key = key.lower()
    if lower_key in headers:
        return headers[lower_key]
    for k, v in headers.items():
        if k.lower() == lower_key:
            return v
    return None


def generate_request_id(command: str) -> str:
    """生成唯一請求 ID"""
    import time
    hash_input = f"{command}:{time.time()}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:12]


def decimal_to_native(obj):
    """將 DynamoDB Decimal 轉換為 Python 原生類型"""
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def response(status_code: int, body: dict) -> dict:
    """標準 API 回應格式"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps(body, ensure_ascii=False)
    }


def mcp_result(req_id, result: dict) -> dict:
    """MCP JSON-RPC 成功回應"""
    return response(200, {
        'jsonrpc': '2.0',
        'id': req_id,
        'result': result
    })


def mcp_error(req_id, code: int, message: str) -> dict:
    """MCP JSON-RPC 錯誤回應"""
    return response(200, {
        'jsonrpc': '2.0',
        'id': req_id,
        'error': {
            'code': code,
            'message': message
        }
    })


def log_decision(table, request_id, command, reason, source, account_id,
                 decision_type, risk_score=None, risk_factors=None,
                 sequence_modifier=None, exit_code: Optional[int] = None,
                 error_output: Optional[str] = None, result: Optional[str] = None,
                 notification_suppressed: bool = False,
                 agent_id: Optional[str] = None,
                 verified_identity: bool = False,
                 **kwargs):
    """統一的決策記錄函數 — 記錄所有審批決策到 requests 表"""
    now = int(time.time())
    item = {
        'request_id': request_id,
        'command': command[:2000],
        'reason': reason[:500],
        'source': source or '__anonymous__',
        'account_id': account_id or '',
        'decision_type': decision_type,
        'status': decision_type,  # 向後兼容
        'created_at': now,
        'decided_at': now,
        'decision_latency_ms': 0,
        'ttl': now + AUDIT_TTL_LONG,  # 90 天保留（blocked/compliance 30 天）
    }
    if agent_id:
        item['agent_id'] = agent_id
    if verified_identity:
        item['verified_identity'] = verified_identity
    if decision_type in ('blocked', 'compliance_violation'):
        item['ttl'] = now + AUDIT_TTL_SHORT  # 30 天
    if risk_score is not None:
        item['risk_score'] = Decimal(str(risk_score))
        item['risk_category'] = kwargs.pop('risk_category', '')
    if risk_factors:
        item['risk_factors'] = risk_factors[:5]
    if sequence_modifier is not None:
        item['sequence_modifier'] = str(sequence_modifier)
    if exit_code is not None:
        item['exit_code'] = exit_code
    if error_output is not None:
        if len(error_output) > 2000:
            error_output = error_output[:2000] + '[truncated]'
        item['error_output'] = error_output or '(no output)'
    if result is not None:
        # Store full result for bouncer_status retrieval (DDB 400KB item limit → cap at 300KB)
        max_result_bytes = 300_000
        if len(result.encode('utf-8', errors='replace')) > max_result_bytes:
            # Binary-safe truncation: find a safe cut point within byte limit
            truncated = result.encode('utf-8', errors='replace')[:max_result_bytes].decode('utf-8', errors='ignore')
            item['result'] = truncated + '\n[truncated — result exceeded 300KB]'
        else:
            item['result'] = result
    if notification_suppressed:
        item['notification_suppressed'] = notification_suppressed
    item.update({k: v for k, v in kwargs.items() if v is not None})
    try:
        table.put_item(Item=item)
    except ClientError as e:
        logger.exception("Failed to log decision: %s", e, extra={"src_module": "utils", "operation": "log_decision", "error": str(e)})
    return item


def extract_exit_code(output: str) -> Optional[int]:
    """從命令輸出解析 exit code。

    - 匹配 `(exit code: N)` → return N
    - 以 `usage:` 或 `Usage:` 開頭 → return 2（AWS CLI 語法錯誤）
    - 以 `❌` 開頭且無 exit code marker → return -1（Bouncer-formatted error）
    - 成功（無 error marker）→ return None

    Args:
        output: 命令輸出字串

    Returns:
        exit code（int）或 None（表示成功/無錯誤）
    """
    m = re.search(r'\(exit code:\s*(\d+)\)', output)
    if m:
        return int(m.group(1))
    # AWS CLI prints usage on syntax errors (exit code 2)
    if output.startswith('usage:') or output.startswith('Usage:'):
        return 2
    if output.startswith('❌'):
        return -1  # Bouncer-formatted error, unknown exit code
    return None


def record_execution_error(table, request_id: str, exit_code: int,
                           error_output: str) -> None:
    """DDB update_item — 記錄命令執行失敗詳情到已存在的 request 記錄。

    更新欄位：status, exit_code, error_output, executed_at。
    使用 update_item 以避免覆蓋已有欄位。
    失敗時只 log，不 raise。
    """
    # Truncate error_output
    if len(error_output) > 2000:
        error_output = error_output[:2000] + '[truncated]'
    if not error_output:
        error_output = '(no output)'
    if exit_code is None:
        exit_code = -1  # unknown
    try:
        table.update_item(
            Key={'request_id': request_id},
            UpdateExpression=(
                'SET #s = :s, exit_code = :ec, error_output = :eo, executed_at = :ea'
            ),
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'executed_error',
                ':ec': exit_code,
                ':eo': error_output,
                ':ea': int(time.time()),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to record execution error for %s: %s", request_id, e, extra={"src_module": "utils", "operation": "record_execution_error", "request_id": request_id, "error": str(e)})
