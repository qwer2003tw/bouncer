"""Centralized Bouncer module list for test isolation (Sprint 58 s58-001).

Import this from test files instead of conftest to avoid xdist worker
ModuleNotFoundError (conftest is not importable as a regular module).

Usage:
    from _module_list import BOUNCER_MODS
"""

BOUNCER_MODS = [
    'app', 'db', 'trust', 'notifications', 'callbacks',
    'callbacks_command', 'callbacks_upload', 'callbacks_grant',
    'mcp_execute', 'mcp_tools', 'telegram', 'commands',
    'mcp_upload', 'mcp_admin', 'mcp_history', 'mcp_confirm',
    'mcp_presigned', 'accounts', 'rate_limit', 'utils',
    'paging', 'smart_approval', 'risk_scorer', 'template_scanner',
    'scheduler_service', 'compliance_checker', 'grant', 'deployer',
    'constants', 'metrics', 'sequence_analyzer', 'help_command',
    'tool_schema', 'otp', 'trust_expiry', 'telegram_commands',
    'telegram_entities', 'mcp_deploy_frontend',
    # NOTE: template_diff_analyzer, changeset_analyzer, upload_scanner, aws_clients
    # are intentionally excluded — they use module-level state/AWS clients and must NOT
    # be reset between tests (resetting causes real AWS calls without mock context).
    # Tests for these modules use their own isolation via @patch decorators.
]
