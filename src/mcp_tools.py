"""
Bouncer - MCP Tool 實作模組（向後兼容 re-export hub）

實際實作已拆分到:
  - mcp_execute.py  — execute pipeline + grant session tools
  - mcp_upload.py   — upload pipeline + batch upload
  - mcp_admin.py    — admin/query tools

本模組保留所有公開名稱的 re-export，確保既有的 import 和 patch 路徑不會壞掉。

MCP 錯誤格式規則：
- Business error（命令被阻擋、帳號不存在、格式錯誤等）→ mcp_result with isError: True
- Protocol error（缺少參數、JSON 解析失敗、內部錯誤等）→ mcp_error
"""

# ---------------------------------------------------------------------------
# Re-export everything from sub-modules so that:
#   from mcp_tools import mcp_tool_execute   — still works
#   import mcp_tools; mcp_tools.ExecuteContext — still works
#   @patch('mcp_tools.execute_command')        — still works
# ---------------------------------------------------------------------------

# --- Execute pipeline ---
from mcp_execute import (  # noqa: F401
    ExecuteContext,
    mcp_tool_execute,
    mcp_tool_request_grant,
    mcp_tool_grant_status,
    mcp_tool_revoke_grant,
    # internal helpers (used by tests via patch)
    _check_grant_session,
    _check_compliance,
    _check_blocked,
    _check_auto_approve,
    _check_rate_limit,
    _check_trust_session,
    _submit_for_approval,
    _parse_execute_request,
    _score_risk,
    _safe_risk_category,
    _safe_risk_factors,
    _log_smart_approval_shadow,
    SHADOW_TABLE_NAME,
)

# --- Upload pipeline ---
from mcp_upload import (  # noqa: F401
    UploadContext,
    mcp_tool_upload,
    mcp_tool_upload_batch,
    _sanitize_filename,
    _format_size_human,
    _check_upload_trust,
    _check_upload_rate_limit,
    _submit_upload_for_approval,
    _parse_upload_request,
    _resolve_upload_target,
)

# --- Presigned upload ---
from mcp_presigned import (  # noqa: F401
    PresignedContext,
    PresignedBatchContext,
    mcp_tool_request_presigned,
    mcp_tool_request_presigned_batch,
    _sanitize_filename as _sanitize_presigned_filename,
    _parse_presigned_request,
    _parse_presigned_batch_request,
    _parse_common_presigned_params,
    _check_rate_limit_for_source,
    _generate_presigned_url_for_file,
    _resolve_presigned_target,
    _generate_presigned_url,
)

# --- Admin / query tools ---
from mcp_admin import (  # noqa: F401
    mcp_tool_status,
    mcp_tool_help,
    mcp_tool_trust_status,
    mcp_tool_trust_revoke,
    mcp_tool_add_account,
    mcp_tool_list_accounts,
    mcp_tool_get_page,
    mcp_tool_list_pending,
    mcp_tool_remove_account,
    mcp_tool_list_safelist,
)

# ---------------------------------------------------------------------------
# Re-export names that tests patch via 'mcp_tools.<name>'.
# These were previously imported at module-level in the old monolith.
# By importing them here, @patch('mcp_tools.execute_command') resolves to
# mcp_tools.execute_command, which is looked up at call time in the
# sub-modules that also imported the same name from the same source.
#
# IMPORTANT: For @patch('mcp_tools.X') to work, the sub-module must
# reference X through *this* module or the patch must target the sub-module.
# We handle this by re-exporting AND updating test patch targets.
# ---------------------------------------------------------------------------

from commands import execute_command, get_block_reason, is_auto_approve  # noqa: F401
from accounts import (  # noqa: F401
    init_default_account, get_account, list_accounts,
    validate_account_id, validate_role_arn,
)
from rate_limit import RateLimitExceeded, PendingLimitExceeded, check_rate_limit  # noqa: F401
from trust import (  # noqa: F401
    revoke_trust_session, increment_trust_command_count, should_trust_approve,
    should_trust_approve_upload, increment_trust_upload_count,
)
from telegram import escape_markdown, send_telegram_message  # noqa: F401
from db import table  # noqa: F401
from notifications import (  # noqa: F401
    send_approval_request,
    send_account_approval_request,
    send_trust_auto_approve_notification,
    send_grant_request_notification,
    send_grant_execute_notification,
    send_blocked_notification,
    send_trust_upload_notification,
    send_batch_upload_notification,
)
from utils import mcp_result, mcp_error, generate_request_id, decimal_to_native, log_decision  # noqa: F401
from paging import store_paged_output, get_paged_output  # noqa: F401
from constants import (  # noqa: F401
    DEFAULT_ACCOUNT_ID, MCP_MAX_WAIT, RATE_LIMIT_WINDOW,
    TRUST_SESSION_MAX_COMMANDS, TRUST_SESSION_MAX_UPLOADS,
    TRUST_UPLOAD_MAX_BYTES_PER_FILE, TRUST_UPLOAD_MAX_BYTES_TOTAL,
    APPROVAL_TIMEOUT_DEFAULT, APPROVAL_TTL_BUFFER, UPLOAD_TIMEOUT,
    AUDIT_TTL_SHORT,
    GRANT_SESSION_ENABLED,
    AUTO_APPROVE_PREFIXES, BLOCKED_PATTERNS,
)
