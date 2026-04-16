"""Centralized Bouncer module list for test isolation (Sprint 58 s58-001).

Import this from test files instead of conftest to avoid xdist worker
ModuleNotFoundError (conftest is not importable as a regular module).

Usage:
    from _module_list import BOUNCER_MODS
"""

# Modules safe to clear between tests (no module-level AWS state).
# These are reset in fixtures that manage their own moto context (e.g. app_mod).
#
# EXCLUDED (must NOT be in this list):
#   - template_diff_analyzer: module-level caches, tests use @patch decorators
#   - changeset_analyzer: same reason
#   - upload_scanner: same reason
#   - aws_clients: module-level boto3 clients
#   - mcp_deploy_frontend: loads project config from DynamoDB at call time;
#     clearing it causes "Unknown project" in tests that don't re-seed DynamoDB
BOUNCER_MODS = [
    'app', 'db', 'trust', 'notifications', 'callbacks',
    'callbacks_command', 'callbacks_upload', 'callbacks_grant',
    'mcp_execute', 'telegram', 'commands',
    'mcp_upload', 'mcp_admin', 'mcp_grant', 'mcp_history', 'mcp_confirm',
    'mcp_presigned', 'accounts', 'rate_limit', 'utils',
    'paging', 'smart_approval', 'risk_scorer', 'template_scanner',
    'scheduler_service', 'compliance_checker', 'grant', 'deployer',
    'deploy_db', 'deploy_preflight',
    'constants', 'metrics', 'sequence_analyzer', 'help_command',
    'tool_schema', 'otp', 'trust_expiry', 'telegram_commands',
    'telegram_entities', 'webhook_router',
]
