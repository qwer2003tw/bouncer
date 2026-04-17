"""
Tests for Sprint 32-001b — Deploy Auto-Approve Integration

TC01-TC05, TC09-TC10 replaced by regression test (Sprint 75, #243):
  auto_approve_code_only path removed — changeset analysis delegated to Step Functions.

TC06 - add_project with auto_approve_deploy=True → DDB item has auto_approve_deploy=True
TC07 - update_project_config patches auto_approve_deploy → DDB updated correctly
TC08 - send_auto_approve_deploy_notification sends silent Telegram with deploy_id
"""
from __future__ import annotations

import json
import os

import pytest
from unittest.mock import MagicMock, patch

import deploy_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(auto_approve=True, template_s3_url="https://bouncer-test.s3.us-east-1.amazonaws.com/packaged-template.yaml", stack_name="my-stack"):
    return {
        "project_id": "my-project",
        "name": "My Project",
        "stack_name": stack_name,
        "default_branch": "master",
        "git_repo": "https://github.com/org/repo",
        "target_account": "123456789012",
        "auto_approve_code_only": auto_approve,
        "template_s3_url": template_s3_url,
        "enabled": True,
    }


def _call_mcp_tool_deploy(project=None, branch=None, reason="Test deploy", source="test-source",
                           context=None):
    """Helper to call mcp_tool_deploy with mocked dependencies."""
    import deployer
    arguments = {
        "project": "my-project",
        "reason": reason,
        "source": source,
    }
    if branch:
        arguments["branch"] = branch
    if context:
        arguments["context"] = context

    raw = deployer.mcp_tool_deploy(
        req_id="test-req-1",
        arguments=arguments,
        table=MagicMock(),
        send_approval_func=MagicMock(),
    )
    # mcp_result wraps in {'statusCode':..., 'body': json_str}
    # unwrap body and return inner JSON-RPC result dict
    import json as _json
    body = _json.loads(raw["body"])
    return body  # {'jsonrpc': '2.0', 'id': ..., 'result': {...}}


# ---------------------------------------------------------------------------
# Regression: auto_approve_code_only removed (Sprint 75 #243)
# ---------------------------------------------------------------------------

def test_regression_auto_approve_code_only_ignored():
    """#243: auto_approve_code_only=True no longer triggers dry-run changeset;
    deploy goes through normal approval (Step Functions handles changeset)."""
    import deployer

    project = _make_project(auto_approve=False)  # auto_approve_deploy=False
    project['auto_approve_code_only'] = True      # this flag is now ignored

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "generate_request_id", return_value="req-regression"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    # Should route to normal approval — no changeset analysis
    mock_approval.assert_called_once()
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "pending_approval"


# ---------------------------------------------------------------------------
# TC06 — add_project with auto_approve_deploy=True → DDB item correct
# ---------------------------------------------------------------------------

def test_tc06_add_project_includes_auto_approve_fields():
    """add_project stores auto_approve_deploy and template_s3_url in DDB item."""
    import deployer

    mock_table = MagicMock()
    with patch("deploy_db._get_projects_table", return_value=mock_table):
        result = deployer.add_project("proj-tc06", {
            "name": "TC06 Project",
            "stack_name": "tc06-stack",
            "auto_approve_deploy": True,
            "template_s3_url": "https://bouncer-test.s3.us-east-1.amazonaws.com/template.yaml",
        })

    assert result["auto_approve_deploy"] is True
    assert result["template_s3_url"] == "https://bouncer-test.s3.us-east-1.amazonaws.com/template.yaml"
    mock_table.put_item.assert_called_once()
    put_item_arg = mock_table.put_item.call_args[1]["Item"]
    assert put_item_arg["auto_approve_deploy"] is True
    assert put_item_arg["template_s3_url"] == "https://bouncer-test.s3.us-east-1.amazonaws.com/template.yaml"


# ---------------------------------------------------------------------------
# TC07 — update_project_config patches auto_approve_deploy
# ---------------------------------------------------------------------------

def test_tc07_update_project_config_patches_field():
    """update_project_config correctly builds UpdateExpression and returns merged dict."""
    import deployer

    existing_project = {
        "project_id": "proj-tc07",
        "name": "TC07 Project",
        "auto_approve_deploy": False,
    }
    mock_table = MagicMock()

    with patch("deploy_db.get_project", return_value=existing_project), \
         patch("deploy_db._get_projects_table", return_value=mock_table):

        result = deployer.update_project_config("proj-tc07", {
            "auto_approve_deploy": True,
            "template_s3_url": "https://bouncer-test.s3.us-east-1.amazonaws.com/tmpl.yaml",
        })

    # Merged dict returned
    assert result["auto_approve_deploy"] is True
    assert result["template_s3_url"] == "https://bouncer-test.s3.us-east-1.amazonaws.com/tmpl.yaml"
    assert result["name"] == "TC07 Project"

    # DDB update_item called
    mock_table.update_item.assert_called_once()
    call_kwargs = mock_table.update_item.call_args[1]
    assert call_kwargs["Key"] == {"project_id": "proj-tc07"}
    assert "SET" in call_kwargs["UpdateExpression"]


def test_tc07b_update_project_config_not_found_raises():
    """update_project_config raises ValueError when project doesn't exist."""
    import deployer

    with patch("deploy_db.get_project", return_value=None):
        with pytest.raises(ValueError, match="not found"):
            deployer.update_project_config("nonexistent", {"auto_approve_deploy": True})


# ---------------------------------------------------------------------------
# TC08 — send_auto_approve_deploy_notification sends silent Telegram
# ---------------------------------------------------------------------------

def test_tc08_send_auto_approve_deploy_notification():
    """Notification sends silent Telegram message with project_id and deploy_id."""
    import notifications

    with patch.object(notifications._telegram, "send_message_with_entities") as mock_send:
        notifications.send_auto_approve_deploy_notification(
            project_id="my-project",
            deploy_id="deploy-tc08abc",
            source="Private Bot (test)",
            reason="Sprint 32 TC08 test",
        )

    mock_send.assert_called_once()
    call_kwargs = mock_send.call_args
    # silent=True must be passed
    assert call_kwargs[1].get("silent") is True or (len(call_kwargs[0]) > 2 and call_kwargs[0][2] is True)
    # The text should contain project and deploy id
    text_arg = call_kwargs[0][0]
    assert "my-project" in text_arg
    assert "deploy-tc08abc" in text_arg
