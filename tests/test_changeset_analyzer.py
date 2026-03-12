"""
Tests for changeset_analyzer — Sprint 32-001a
TC01-TC10 covering is_code_only_change, analyze_changeset, and cleanup_changeset.
"""
from __future__ import annotations

import sys
import os

import pytest
from botocore.exceptions import ClientError
from unittest.mock import MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from changeset_analyzer import (  # noqa: E402
    AnalysisResult,
    analyze_changeset,
    cleanup_changeset,
    create_dry_run_changeset,
    is_code_only_change,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lambda_code_modify(logical_id: str = "MyFn") -> dict:
    """Build a Change dict for a Lambda Code-only Modify."""
    return {
        "ResourceChange": {
            "Action": "Modify",
            "ResourceType": "AWS::Lambda::Function",
            "LogicalResourceId": logical_id,
            "Details": [
                {
                    "Target": {
                        "Attribute": "Properties",
                        "Name": "Code",
                    }
                }
            ],
        }
    }


def _make_client_error(code: str = "TestError", message: str = "test") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "TestOp")


# ---------------------------------------------------------------------------
# TC01 — 2x Lambda Code Modify → is_code_only == True
# ---------------------------------------------------------------------------


def test_tc01_two_lambda_code_modify():
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[
            _make_lambda_code_modify("Fn1"),
            _make_lambda_code_modify("Fn2"),
        ],
    )
    assert is_code_only_change(result) is True


# ---------------------------------------------------------------------------
# TC02 — Lambda + DynamoDB Modify → False
# ---------------------------------------------------------------------------


def test_tc02_lambda_and_dynamodb_modify():
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[
            _make_lambda_code_modify("Fn1"),
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::DynamoDB::Table",
                    "LogicalResourceId": "MyTable",
                    "Details": [
                        {
                            "Target": {
                                "Attribute": "Properties",
                                "Name": "BillingMode",
                            }
                        }
                    ],
                }
            },
        ],
    )
    assert is_code_only_change(result) is False


# ---------------------------------------------------------------------------
# TC03 — Lambda Action=Add → False
# ---------------------------------------------------------------------------


def test_tc03_lambda_action_add():
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Add",
                    "ResourceType": "AWS::Lambda::Function",
                    "LogicalResourceId": "NewFn",
                    "Details": [],
                }
            }
        ],
    )
    assert is_code_only_change(result) is False


# ---------------------------------------------------------------------------
# TC04 — Lambda Action=Remove → False
# ---------------------------------------------------------------------------


def test_tc04_lambda_action_remove():
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Remove",
                    "ResourceType": "AWS::Lambda::Function",
                    "LogicalResourceId": "OldFn",
                    "Details": [],
                }
            }
        ],
    )
    assert is_code_only_change(result) is False


# ---------------------------------------------------------------------------
# TC05 — Lambda Timeout (non-Code) property change → False
# ---------------------------------------------------------------------------


def test_tc05_lambda_timeout_change():
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::Lambda::Function",
                    "LogicalResourceId": "Fn1",
                    "Details": [
                        {
                            "Target": {
                                "Attribute": "Properties",
                                "Name": "Timeout",
                            }
                        }
                    ],
                }
            }
        ],
    )
    assert is_code_only_change(result) is False


# ---------------------------------------------------------------------------
# TC06 — AnalysisResult.error != None → is_code_only_change returns False
# ---------------------------------------------------------------------------


def test_tc06_error_not_none():
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[_make_lambda_code_modify()],
        error="something went wrong",
    )
    assert is_code_only_change(result) is False


# ---------------------------------------------------------------------------
# TC07 — empty resource_changes (no-op) → True
# ---------------------------------------------------------------------------


def test_tc07_empty_resource_changes_noop():
    result = AnalysisResult(is_code_only=False, resource_changes=[])
    assert is_code_only_change(result) is True


# ---------------------------------------------------------------------------
# TC08 — cleanup ChangeSetNotFoundException → no exception raised
# ---------------------------------------------------------------------------


def test_tc08_cleanup_changeset_not_found():
    mock_cfn = MagicMock()
    mock_cfn.delete_change_set.side_effect = _make_client_error(
        "ChangeSetNotFoundException"
    )
    # Should not raise
    cleanup_changeset(mock_cfn, "my-stack", "bouncer-dryrun-abc123")
    mock_cfn.delete_change_set.assert_called_once_with(
        StackName="my-stack",
        ChangeSetName="bouncer-dryrun-abc123",
    )


# ---------------------------------------------------------------------------
# TC09 — analyze_changeset FAILED status → AnalysisResult.error populated
# ---------------------------------------------------------------------------


def test_tc09_analyze_changeset_failed_status():
    mock_cfn = MagicMock()
    mock_cfn.describe_change_set.return_value = {
        "Status": "FAILED",
        "StatusReason": "No updates are to be performed.",
        "Changes": [],
    }

    result = analyze_changeset(
        mock_cfn,
        "my-stack",
        "bouncer-dryrun-xyz",
        max_wait=10,
        poll_interval=2,
    )

    assert result.error is not None
    assert "No updates" in result.error
    assert result.is_code_only is False
    assert result.resource_changes == []


# ---------------------------------------------------------------------------
# TC10 — analyze_changeset timeout → AnalysisResult.error == 'timeout'
# ---------------------------------------------------------------------------


def test_tc10_analyze_changeset_timeout(monkeypatch):
    mock_cfn = MagicMock()
    # Always return IN_PROGRESS so we never reach CREATE_COMPLETE
    mock_cfn.describe_change_set.return_value = {
        "Status": "CREATE_IN_PROGRESS",
        "StatusReason": "",
        "Changes": [],
    }

    # Patch time.sleep to avoid actual waiting
    monkeypatch.setattr("changeset_analyzer.time.sleep", lambda _: None)

    result = analyze_changeset(
        mock_cfn,
        "my-stack",
        "bouncer-dryrun-timeout",
        max_wait=4,
        poll_interval=2,
    )

    assert result.error == "timeout"
    assert result.is_code_only is False
    assert result.resource_changes == []


# ---------------------------------------------------------------------------
# TC11 — SAM AutoPublishAlias lifecycle (Version Add + Alias Modify + Function Code Modify) → True
# ---------------------------------------------------------------------------


def test_tc11_sam_autopublishalias_lifecycle():
    """SAM AutoPublishAlias causes Lambda::Version Add, Lambda::Version Delete,
    Lambda::Alias Modify alongside Lambda::Function Code Modify — all safe."""
    result = AnalysisResult(
        is_code_only=True,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::Lambda::Function",
                    "LogicalResourceId": "ApprovalFunction",
                    "Details": [
                        {"Target": {"Attribute": "Properties", "Name": "Code"}}
                    ],
                }
            },
            {
                "ResourceChange": {
                    "Action": "Add",
                    "ResourceType": "AWS::Lambda::Version",
                    "LogicalResourceId": "ApprovalFunctionVersionABC",
                    "Details": [],
                }
            },
            {
                "ResourceChange": {
                    "Action": "Delete",
                    "ResourceType": "AWS::Lambda::Version",
                    "LogicalResourceId": "ApprovalFunctionVersionOLD",
                    "Details": [],
                }
            },
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::Lambda::Alias",
                    "LogicalResourceId": "ApprovalFunctionAliaslive",
                    "Details": [],
                }
            },
        ],
    )
    assert is_code_only_change(result) is True


# ---------------------------------------------------------------------------
# TC12 — Lambda::Version Add OK but DynamoDB Modify → False
# ---------------------------------------------------------------------------


def test_tc12_version_add_with_ddb_modify_is_false():
    """Lambda::Version Add is safe but DynamoDB::Table Modify is infra change."""
    result = AnalysisResult(
        is_code_only=False,
        resource_changes=[
            {
                "ResourceChange": {
                    "Action": "Add",
                    "ResourceType": "AWS::Lambda::Version",
                    "LogicalResourceId": "ApprovalFunctionVersionNew",
                    "Details": [],
                }
            },
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "ResourceType": "AWS::DynamoDB::Table",
                    "LogicalResourceId": "RequestsTable",
                    "Details": [],
                }
            },
        ],
    )
    assert is_code_only_change(result) is False
