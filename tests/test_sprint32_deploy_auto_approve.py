"""
Tests for Sprint 32-001b — Deploy Auto-Approve Integration

TC01 - auto_approve=True + code-only → start_deploy called, status="started", auto_approved=True
TC02 - auto_approve=True + infra change → send_deploy_approval_request, changeset_summary in context
TC03 - auto_approve=True + changeset error → fail-safe (normal approval), error in context
TC04 - auto_approve=False → changeset_analyzer NOT called, normal approval flow
TC05 - auto_approve=True + template_s3_url empty → fail-safe (no changeset, normal approval)
TC06 - add_project with auto_approve_deploy=True → DDB item has auto_approve_deploy=True
TC07 - update_project_config patches auto_approve_deploy → DDB updated correctly
TC08 - send_auto_approve_deploy_notification sends silent Telegram with deploy_id
TC09 - cleanup_changeset always called in finally (even if analysis fails)
TC10 - auto_approve=True + code-only → send_auto_approve_deploy_notification called once
"""
from __future__ import annotations

import json
import sys
import os

import pytest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_code_only_analysis():
    """AnalysisResult with code-only change."""
    from changeset_analyzer import AnalysisResult
    return AnalysisResult(
        is_code_only=True,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::Lambda::Function",
                    "LogicalResourceId": "MyFn",
                    "Details": [{"Target": {"Attribute": "Properties", "Name": "Code"}}],
                }
            }
        ],
    )


def _make_infra_analysis():
    """AnalysisResult with infra change (DynamoDB)."""
    from changeset_analyzer import AnalysisResult
    return AnalysisResult(
        is_code_only=False,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::DynamoDB::Table",
                    "LogicalResourceId": "MyTable",
                    "Details": [{"Target": {"Attribute": "Properties", "Name": "BillingMode"}}],
                }
            }
        ],
    )


def _make_error_analysis():
    """AnalysisResult with error."""
    from changeset_analyzer import AnalysisResult
    return AnalysisResult(
        is_code_only=False,
        resource_changes=[],
        error="FAILED: changeset creation error",
    )


def _make_project(auto_approve=True, template_s3_url="s3://my-bucket/template.yaml", stack_name="my-stack"):
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


def _make_deploy_result():
    return {
        "status": "started",
        "deploy_id": "deploy-abc123456789",
        "project_id": "my-project",
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
# TC01 — auto_approve=True + code-only → start_deploy called, auto_approved=True
# ---------------------------------------------------------------------------

def test_tc01_auto_approve_code_only_start_deploy():
    """Code-only change: mcp_tool_deploy returns status=started, auto_approved=True."""
    import deployer

    project = _make_project()
    deploy_result = _make_deploy_result()

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset", return_value="bouncer-dryrun-tc01"), \
         patch("changeset_analyzer.analyze_changeset", return_value=_make_code_only_analysis()), \
         patch("changeset_analyzer.cleanup_changeset") as mock_cleanup, \
         patch.object(deployer, "start_deploy", return_value=deploy_result) as mock_start, \
         patch("notifications.send_auto_approve_deploy_notification") as mock_notify, \
         patch.object(deployer, "_get_cfn_client", return_value=MagicMock()), \
         patch.object(deployer._db, "table", MagicMock()):

        result = _call_mcp_tool_deploy()

    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "started"
    assert body["auto_approved"] is True
    assert body["deploy_id"] == "deploy-abc123456789"
    mock_start.assert_called_once()
    mock_notify.assert_called_once()
    mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# TC02 — auto_approve=True + infra change → approval request, context has summary
# ---------------------------------------------------------------------------

def test_tc02_auto_approve_infra_change_goes_to_approval():
    """Infra change: falls back to human approval and injects context summary."""
    import deployer

    project = _make_project()

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset", return_value="bouncer-dryrun-tc02"), \
         patch("changeset_analyzer.analyze_changeset", return_value=_make_infra_analysis()), \
         patch("changeset_analyzer.cleanup_changeset") as mock_cleanup, \
         patch.object(deployer, "start_deploy") as mock_start, \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "_get_cfn_client", return_value=MagicMock()), \
         patch.object(deployer, "generate_request_id", return_value="req-tc02"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    # Should NOT have called start_deploy
    mock_start.assert_not_called()
    # Should have called send_deploy_approval_request
    mock_approval.assert_called_once()
    # Context injected into approval call should mention infra change
    call_kwargs = mock_approval.call_args
    context_arg = call_kwargs[1].get("context") or (call_kwargs[0][5] if len(call_kwargs[0]) > 5 else "")
    assert "infra" in context_arg or "MyTable" in context_arg or "需審批" in context_arg
    # Cleanup still called
    mock_cleanup.assert_called_once()
    # Result is pending_approval
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "pending_approval"


# ---------------------------------------------------------------------------
# TC03 — auto_approve=True + changeset error → fail-safe, error in context
# ---------------------------------------------------------------------------

def test_tc03_auto_approve_changeset_error_fallback():
    """Changeset error: fail-safe → human approval, error context injected."""
    import deployer

    project = _make_project()

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset", return_value="bouncer-dryrun-tc03"), \
         patch("changeset_analyzer.analyze_changeset", return_value=_make_error_analysis()), \
         patch("changeset_analyzer.cleanup_changeset") as mock_cleanup, \
         patch.object(deployer, "start_deploy") as mock_start, \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "_get_cfn_client", return_value=MagicMock()), \
         patch.object(deployer, "generate_request_id", return_value="req-tc03"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    mock_start.assert_not_called()
    mock_approval.assert_called_once()
    # Check context has error info
    call_kwargs = mock_approval.call_args
    context_arg = call_kwargs[1].get("context") or ""
    assert "changeset 分析失敗" in context_arg or "error" in context_arg.lower()
    mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# TC04 — auto_approve=False → changeset_analyzer NOT called
# ---------------------------------------------------------------------------

def test_tc04_auto_approve_disabled_no_changeset():
    """auto_approve_deploy=False: changeset_analyzer must not be called."""
    import deployer

    project = _make_project(auto_approve=False)

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset") as mock_create, \
         patch("changeset_analyzer.analyze_changeset") as mock_analyze, \
         patch("changeset_analyzer.cleanup_changeset") as mock_cleanup, \
         patch.object(deployer, "start_deploy") as mock_start, \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "generate_request_id", return_value="req-tc04"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    mock_create.assert_not_called()
    mock_analyze.assert_not_called()
    mock_cleanup.assert_not_called()
    mock_start.assert_not_called()
    mock_approval.assert_called_once()
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["status"] == "pending_approval"


# ---------------------------------------------------------------------------
# TC05 — auto_approve=True + template_s3_url empty → fail-safe
# ---------------------------------------------------------------------------

def test_tc05_auto_approve_empty_template_url_no_changeset():
    """template_s3_url is empty: changeset NOT created, falls to normal approval."""
    import deployer

    project = _make_project(auto_approve=True, template_s3_url="")

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset") as mock_create, \
         patch("changeset_analyzer.analyze_changeset") as mock_analyze, \
         patch("changeset_analyzer.cleanup_changeset") as mock_cleanup, \
         patch.object(deployer, "start_deploy") as mock_start, \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "generate_request_id", return_value="req-tc05"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    mock_create.assert_not_called()
    mock_analyze.assert_not_called()
    mock_start.assert_not_called()
    mock_approval.assert_called_once()


# ---------------------------------------------------------------------------
# TC06 — add_project with auto_approve_deploy=True → DDB item correct
# ---------------------------------------------------------------------------

def test_tc06_add_project_includes_auto_approve_fields():
    """add_project stores auto_approve_deploy and template_s3_url in DDB item."""
    import deployer

    mock_table = MagicMock()
    with patch.object(deployer, "_get_projects_table", return_value=mock_table):
        result = deployer.add_project("proj-tc06", {
            "name": "TC06 Project",
            "stack_name": "tc06-stack",
            "auto_approve_deploy": True,
            "template_s3_url": "s3://my-bucket/template.yaml",
        })

    assert result["auto_approve_deploy"] is True
    assert result["template_s3_url"] == "s3://my-bucket/template.yaml"
    mock_table.put_item.assert_called_once()
    put_item_arg = mock_table.put_item.call_args[1]["Item"]
    assert put_item_arg["auto_approve_deploy"] is True
    assert put_item_arg["template_s3_url"] == "s3://my-bucket/template.yaml"


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

    with patch.object(deployer, "get_project", return_value=existing_project), \
         patch.object(deployer, "_get_projects_table", return_value=mock_table):

        result = deployer.update_project_config("proj-tc07", {
            "auto_approve_deploy": True,
            "template_s3_url": "s3://bucket/tmpl.yaml",
        })

    # Merged dict returned
    assert result["auto_approve_deploy"] is True
    assert result["template_s3_url"] == "s3://bucket/tmpl.yaml"
    assert result["name"] == "TC07 Project"

    # DDB update_item called
    mock_table.update_item.assert_called_once()
    call_kwargs = mock_table.update_item.call_args[1]
    assert call_kwargs["Key"] == {"project_id": "proj-tc07"}
    assert "SET" in call_kwargs["UpdateExpression"]


def test_tc07b_update_project_config_not_found_raises():
    """update_project_config raises ValueError when project doesn't exist."""
    import deployer

    with patch.object(deployer, "get_project", return_value=None):
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


# ---------------------------------------------------------------------------
# TC09 — cleanup_changeset always called in finally (even if analysis raises)
# ---------------------------------------------------------------------------

def test_tc09_cleanup_always_called_in_finally():
    """cleanup_changeset is invoked in finally even when analyze_changeset raises."""
    import deployer

    project = _make_project()

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset", return_value="bouncer-dryrun-tc09"), \
         patch("changeset_analyzer.analyze_changeset", side_effect=RuntimeError("boom")), \
         patch("changeset_analyzer.cleanup_changeset") as mock_cleanup, \
         patch.object(deployer, "start_deploy") as mock_start, \
         patch.object(deployer, "send_deploy_approval_request") as mock_approval, \
         patch.object(deployer, "_get_cfn_client", return_value=MagicMock()), \
         patch.object(deployer, "generate_request_id", return_value="req-tc09"), \
         patch.object(deployer, "_db") as mock_db:

        mock_db.table = MagicMock()
        result = _call_mcp_tool_deploy()

    # cleanup must always be called (finally block)
    mock_cleanup.assert_called_once()
    # Fell back to human approval (fail-safe)
    mock_start.assert_not_called()
    mock_approval.assert_called_once()


# ---------------------------------------------------------------------------
# TC10 — auto_approve=True + code-only → send_auto_approve_deploy_notification called once
# ---------------------------------------------------------------------------

def test_tc10_auto_approve_notification_called_once():
    """Code-only auto-approve: notification sent exactly once."""
    import deployer

    project = _make_project()
    deploy_result = _make_deploy_result()

    with patch.object(deployer, "get_project", return_value=project), \
         patch.object(deployer, "preflight_check_secrets", return_value=[]), \
         patch.object(deployer, "get_lock", return_value=None), \
         patch("changeset_analyzer.create_dry_run_changeset", return_value="bouncer-dryrun-tc10"), \
         patch("changeset_analyzer.analyze_changeset", return_value=_make_code_only_analysis()), \
         patch("changeset_analyzer.cleanup_changeset"), \
         patch.object(deployer, "start_deploy", return_value=deploy_result), \
         patch("notifications.send_auto_approve_deploy_notification") as mock_notify, \
         patch.object(deployer, "_get_cfn_client", return_value=MagicMock()):

        _call_mcp_tool_deploy()

    # Notification called exactly once
    assert mock_notify.call_count == 1
    call_kwargs = mock_notify.call_args[1]
    assert call_kwargs["project_id"] == "my-project"
    assert call_kwargs["deploy_id"] == "deploy-abc123456789"
