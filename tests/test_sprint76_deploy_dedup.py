"""
Tests for Sprint 76 — #248 deploy request dedup + rate limit

TC01 - _find_pending_deploy: finds valid pending deploy request
TC02 - _find_pending_deploy: skips expired pending request
TC03 - _find_pending_deploy: returns None when no pending deploy
TC04 - _is_deploy_rate_limited: returns True when recent request exists
TC05 - _is_deploy_rate_limited: returns False when no recent request
TC06 - mcp_tool_deploy: dedup returns existing pending request
TC07 - mcp_tool_deploy: rate limited returns error
TC08 - _find_pending_deploy: DDB error → fail-open (returns None)
TC09 - _is_deploy_rate_limited: DDB error → fail-open (returns False)
"""
from __future__ import annotations

import json
import sys
import os
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# TC01-03: _find_pending_deploy
# ---------------------------------------------------------------------------

def test_tc01_find_pending_deploy_valid():
    """Finds a valid (non-expired) pending deploy request."""
    import deployer

    now = int(time.time())
    mock_table = MagicMock()
    mock_table.query.return_value = {
        'Items': [{
            'request_id': 'deploy:proj-123',
            'action': 'deploy',
            'project_id': 'my-proj',
            'status': 'pending_approval',
            'approval_expiry': now + 300,
            'created_at': now - 60,
        }]
    }

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._find_pending_deploy('my-proj')

    assert result is not None
    assert result['request_id'] == 'deploy:proj-123'


def test_tc02_find_pending_deploy_expired():
    """Skips expired pending request (approval_expiry passed)."""
    import deployer

    now = int(time.time())
    mock_table = MagicMock()
    mock_table.query.return_value = {
        'Items': [{
            'request_id': 'deploy:expired-123',
            'action': 'deploy',
            'project_id': 'my-proj',
            'status': 'pending_approval',
            'approval_expiry': now - 60,  # expired 1 min ago
            'created_at': now - 700,
        }]
    }

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._find_pending_deploy('my-proj')

    assert result is None


def test_tc03_find_pending_deploy_none():
    """Returns None when no pending deploy request exists."""
    import deployer

    mock_table = MagicMock()
    mock_table.query.return_value = {'Items': []}

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._find_pending_deploy('my-proj')

    assert result is None


# ---------------------------------------------------------------------------
# TC04-05: _is_deploy_rate_limited
# ---------------------------------------------------------------------------

def test_tc04_rate_limited_recent_request():
    """Returns True when recent deploy request exists within window."""
    import deployer

    mock_table = MagicMock()
    # First query (pending_approval) returns count=1
    mock_table.query.return_value = {'Count': 1}

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._is_deploy_rate_limited('my-proj')

    assert result is True


def test_tc05_not_rate_limited():
    """Returns False when no recent deploy request exists."""
    import deployer

    mock_table = MagicMock()
    # All queries return count=0
    mock_table.query.return_value = {'Count': 0}

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._is_deploy_rate_limited('my-proj')

    assert result is False


# ---------------------------------------------------------------------------
# TC06: mcp_tool_deploy dedup
# ---------------------------------------------------------------------------

def test_tc06_dedup_returns_existing():
    """mcp_tool_deploy returns existing pending request instead of creating new."""
    import deployer

    now = int(time.time())
    project = {
        "project_id": "test-proj",
        "name": "Test Project",
        "stack_name": "test-stack",
        "default_branch": "master",
        "enabled": True,
    }
    pending_item = {
        'request_id': 'deploy:test-proj-existing',
        'action': 'deploy',
        'project_id': 'test-proj',
        'status': 'pending_approval',
        'approval_expiry': now + 300,
        'created_at': now - 60,
    }

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "_find_pending_deploy", return_value=pending_item):

        raw = deployer.mcp_tool_deploy(
            req_id="test-req",
            arguments={"project": "test-proj", "reason": "test"},
            table=MagicMock(),
            send_approval_func=MagicMock(),
        )

    body = json.loads(raw["body"])
    inner = json.loads(body["result"]["content"][0]["text"])
    assert inner["status"] == "pending_approval"
    assert inner["request_id"] == "deploy:test-proj-existing"
    assert inner["duplicate"] is True


# ---------------------------------------------------------------------------
# TC07: mcp_tool_deploy rate limit
# ---------------------------------------------------------------------------

def test_tc07_rate_limited_returns_error():
    """mcp_tool_deploy returns rate_limited error."""
    import deployer

    project = {
        "project_id": "test-proj",
        "name": "Test Project",
        "stack_name": "test-stack",
        "default_branch": "master",
        "enabled": True,
    }

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "_find_pending_deploy", return_value=None), \
         patch.object(deployer, "_is_deploy_rate_limited", return_value=True):

        raw = deployer.mcp_tool_deploy(
            req_id="test-req",
            arguments={"project": "test-proj", "reason": "test"},
            table=MagicMock(),
            send_approval_func=MagicMock(),
        )

    body = json.loads(raw["body"])
    inner = json.loads(body["result"]["content"][0]["text"])
    assert inner["status"] == "rate_limited"
    assert body["result"]["isError"] is True


# ---------------------------------------------------------------------------
# TC08-09: fail-open on DDB errors
# ---------------------------------------------------------------------------

def test_tc08_find_pending_deploy_ddb_error_fail_open():
    """DDB error in _find_pending_deploy → returns None (fail-open)."""
    import deployer

    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("DDB down")

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._find_pending_deploy('my-proj')

    assert result is None


def test_tc09_rate_limited_ddb_error_fail_open():
    """DDB error in _is_deploy_rate_limited → returns False (fail-open)."""
    import deployer

    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("DDB down")

    with patch.object(deployer, '_db') as mock_db:
        mock_db.table = mock_table
        result = deployer._is_deploy_rate_limited('my-proj')

    assert result is False
