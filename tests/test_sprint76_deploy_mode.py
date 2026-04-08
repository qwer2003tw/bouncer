"""
Tests for Sprint 76 — #249 deploy_mode enum

TC01 - _resolve_deploy_mode: deploy_mode='manual' → 'manual'
TC02 - _resolve_deploy_mode: deploy_mode='auto_code' → 'auto_code'
TC03 - _resolve_deploy_mode: deploy_mode='auto_all' → 'auto_all'
TC04 - _resolve_deploy_mode: no deploy_mode + auto_approve_deploy=True → 'auto_code' (backward compat)
TC05 - _resolve_deploy_mode: no deploy_mode + auto_approve_deploy=False → 'manual' (backward compat)
TC06 - mcp_tool_deploy: deploy_mode='manual' → pending_approval (skips template_diff)
TC07 - mcp_tool_deploy: deploy_mode='auto_all' → starts deploy directly
TC08 - mcp_tool_deploy: deploy_mode='auto_code' + safe → starts deploy
TC09 - mcp_tool_deploy: deploy_mode='auto_code' + unsafe → pending_approval
TC10 - add_project stores deploy_mode in DDB item
TC11 - mcp_tool_deploy: auto_all + start_deploy error → returns error
"""
from __future__ import annotations

import json
import sys
import os
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import deployer
from deployer import _resolve_deploy_mode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(**overrides):
    base = {
        "project_id": "test-proj",
        "name": "Test Project",
        "stack_name": "test-stack",
        "default_branch": "master",
        "git_repo": "https://github.com/org/repo",
        "target_account": "123456789012",
        "enabled": True,
    }
    base.update(overrides)
    return base


def _call_mcp_tool_deploy(**kwargs):
    arguments = {
        "project": "test-proj",
        "reason": "Test deploy",
        "source": "test-source",
    }
    arguments.update(kwargs)
    raw = deployer.mcp_tool_deploy(
        req_id="test-req",
        arguments=arguments,
        table=MagicMock(),
        send_approval_func=MagicMock(),
    )
    body = json.loads(raw["body"])
    return body


@dataclass
class FakeDiffResult:
    is_safe: bool = True
    has_template_changes: bool = False
    high_risk_findings: list = None
    diff_summary: str = "no changes"
    error: str = ""

    def __post_init__(self):
        if self.high_risk_findings is None:
            self.high_risk_findings = []


# ---------------------------------------------------------------------------
# TC01-TC05: _resolve_deploy_mode
# ---------------------------------------------------------------------------

def test_tc01_resolve_manual():
    assert _resolve_deploy_mode({"deploy_mode": "manual"}) == "manual"


def test_tc02_resolve_auto_code():
    assert _resolve_deploy_mode({"deploy_mode": "auto_code"}) == "auto_code"


def test_tc03_resolve_auto_all():
    assert _resolve_deploy_mode({"deploy_mode": "auto_all"}) == "auto_all"


def test_tc04_backward_compat_auto_approve_true():
    """auto_approve_deploy=True without deploy_mode → auto_code"""
    assert _resolve_deploy_mode({"auto_approve_deploy": True}) == "auto_code"


def test_tc05_backward_compat_auto_approve_false():
    """auto_approve_deploy=False without deploy_mode → manual"""
    assert _resolve_deploy_mode({"auto_approve_deploy": False}) == "manual"
    assert _resolve_deploy_mode({}) == "manual"


def test_resolve_invalid_deploy_mode_falls_back():
    """Invalid deploy_mode value falls back to auto_approve_deploy check."""
    assert _resolve_deploy_mode({"deploy_mode": "bogus"}) == "manual"
    assert _resolve_deploy_mode({"deploy_mode": "bogus", "auto_approve_deploy": True}) == "auto_code"


# ---------------------------------------------------------------------------
# TC06: deploy_mode='manual' → pending_approval
# ---------------------------------------------------------------------------

def test_tc06_manual_mode_goes_to_approval():
    project = _make_project(deploy_mode="manual")

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "generate_request_id", return_value="req-manual"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    mock_approval.assert_called_once()
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "pending_approval"
    assert body["request_id"] == "req-manual"


# ---------------------------------------------------------------------------
# TC07: deploy_mode='auto_all' → starts deploy directly
# ---------------------------------------------------------------------------

def test_tc07_auto_all_starts_deploy():
    project = _make_project(deploy_mode="auto_all")

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch.object(deployer, "start_deploy", return_value={
             "status": "started", "deploy_id": "deploy-auto-all",
         }) as mock_start, \
         patch.object(deployer, "_get_changed_files", return_value=[]), \
         patch("notifications.send_auto_approve_deploy_notification") as mock_notify:

        result = _call_mcp_tool_deploy()

    mock_start.assert_called_once()
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "started"
    assert body["deploy_mode"] == "auto_all"
    assert body["auto_approved"] is True
    mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# TC08: deploy_mode='auto_code' + safe → starts deploy
# ---------------------------------------------------------------------------

def test_tc08_auto_code_safe_starts_deploy():
    project = _make_project(deploy_mode="auto_code")

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("template_diff_analyzer.analyze_template_diff", return_value=FakeDiffResult(is_safe=True)), \
         patch.object(deployer, "start_deploy", return_value={
             "status": "started", "deploy_id": "deploy-auto-code",
         }) as mock_start, \
         patch.object(deployer, "_get_changed_files", return_value=["src/app.py"]), \
         patch("notifications.send_auto_approve_deploy_notification"):

        result = _call_mcp_tool_deploy()

    mock_start.assert_called_once()
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "started"
    assert body["deploy_mode"] == "auto_code"
    assert body["auto_approved"] is True


# ---------------------------------------------------------------------------
# TC09: deploy_mode='auto_code' + unsafe → pending_approval
# ---------------------------------------------------------------------------

def test_tc09_auto_code_unsafe_goes_to_approval():
    project = _make_project(deploy_mode="auto_code")

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("template_diff_analyzer.analyze_template_diff", return_value=FakeDiffResult(
             is_safe=False, high_risk_findings=["IAM Role added"],
         )), \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "generate_request_id", return_value="req-unsafe"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    mock_approval.assert_called_once()
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "pending_approval"


# ---------------------------------------------------------------------------
# TC10: add_project stores deploy_mode
# ---------------------------------------------------------------------------

def test_tc10_add_project_stores_deploy_mode():
    mock_table = MagicMock()
    with patch.object(deployer, "projects_table", mock_table):
        result = deployer.add_project("proj-tc10", {
            "name": "TC10 Project",
            "deploy_mode": "auto_code",
        })

    assert result["deploy_mode"] == "auto_code"
    put_item_arg = mock_table.put_item.call_args[1]["Item"]
    assert put_item_arg["deploy_mode"] == "auto_code"


def test_tc10b_add_project_default_deploy_mode():
    mock_table = MagicMock()
    with patch.object(deployer, "projects_table", mock_table):
        result = deployer.add_project("proj-tc10b", {"name": "TC10b"})

    assert result["deploy_mode"] == "manual"


# ---------------------------------------------------------------------------
# TC11: auto_all + start_deploy error → returns error
# ---------------------------------------------------------------------------

def test_tc11_auto_all_start_deploy_error():
    project = _make_project(deploy_mode="auto_all")

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch.object(deployer, "start_deploy", return_value={
             "error": "無法取得部署鎖",
         }):

        result = _call_mcp_tool_deploy()

    body = json.loads(result["result"]["content"][0]["text"])
    assert "error" in body
    assert result["result"].get("isError") is True
