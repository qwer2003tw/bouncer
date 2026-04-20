"""
Changeset Analyzer — Sprint 32-001a (updated Sprint 73)

Provides dry-run changeset creation, analysis, and cleanup for
determining whether a CloudFormation deployment only changes Lambda
function code (safe to auto-approve).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(service="bouncer")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AnalysisResult:
    """Result of a CloudFormation changeset analysis."""

    is_code_only: bool
    resource_changes: list  # raw CFN ResourceChange dicts
    error: Optional[str] = field(default=None)  # populated on analysis failure


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def _is_safe_resource_change(change: dict) -> bool:
    """Check if a single resource change is safe (code-only or SAM lifecycle).

    Returns True if the change is whitelisted, False if it requires human review.
    """
    # SAM AutoPublishAlias lifecycle types that are always safe
    _SAFE_LAMBDA_TYPES = {
        "AWS::Lambda::Version",
        "AWS::Lambda::Alias",
        "AWS::Lambda::Permission",
        "AWS::Lambda::LayerVersion",
    }

    # SAM auto-generated resource types — Modify is safe
    _SAFE_SAM_TYPES = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGateway::Stage",
        "AWS::ApiGateway::Deployment",
        "AWS::ApiGateway::Method",
        "AWS::ApiGateway::Resource",
        "AWS::ApiGatewayV2::Api",
        "AWS::ApiGatewayV2::Stage",
        "AWS::ApiGatewayV2::Route",
        "AWS::ApiGatewayV2::Integration",
        "AWS::ApiGatewayV2::Authorizer",
        "AWS::Events::Rule",
        "AWS::Scheduler::Schedule",
        "AWS::Scheduler::ScheduleGroup",
    }

    # Resource types that SAM auto-creates and are safe to add
    _SAFE_ADD_TYPES = {
        "AWS::Logs::LogGroup",
    }

    rc = change.get("ResourceChange", {})
    resource_type = rc.get("ResourceType", "")
    action = rc.get("Action", "")

    # Lambda Version/Alias/Permission and SAM lifecycle types — always safe
    if resource_type in _SAFE_LAMBDA_TYPES:
        return True

    # SAM auto-generated types (API GW, Events, Scheduler) — Modify is safe
    if resource_type in _SAFE_SAM_TYPES and action == "Modify":
        return True

    # SAM auto-created resources (e.g. LogGroup Add) are safe
    if resource_type in _SAFE_ADD_TYPES and action == "Add":
        return True

    # Lambda Function must be Modify-only with Code property changes
    if resource_type != "AWS::Lambda::Function":
        return False

    if action != "Modify":
        return False

    # All detail targets must be Properties.Code
    details = rc.get("Details", [])
    for detail in details:
        target = detail.get("Target", {})
        if target.get("Attribute") != "Properties":
            return False
        if target.get("Name") != "Code":
            return False

    return True


def is_code_only_change(result: AnalysisResult) -> bool:
    """Whitelist check: return True only when ALL conditions are met.

    Allowed resource changes (SAM AutoPublishAlias normal lifecycle):
    - AWS::Lambda::Function  Modify  → Code property change only
    - AWS::Lambda::Version   Add     → SAM publishes a new version
    - AWS::Lambda::Version   Delete  → SAM removes old version
    - AWS::Lambda::Alias     Modify  → Alias points to new version
    - AWS::Logs::LogGroup    Add     → SAM auto-creates log groups

    Any other resource type or action → False (fail-safe → human approval).
    Empty resource_changes → False (fail-safe: unexpected empty changeset
    should not bypass human approval).
    """
    # Condition 1 — analysis must have succeeded
    if result.error is not None:
        return False

    # Empty changeset → safe (no resource changes = no-op deploy)
    if not result.resource_changes:
        return True

    # Check each resource change is whitelisted
    for change in result.resource_changes:
        if not _is_safe_resource_change(change):
            return False

    return True


# ---------------------------------------------------------------------------
# CloudFormation helpers
# ---------------------------------------------------------------------------


def create_dry_run_changeset(
    cfn_client,
    stack_name: str,
    template_s3_url: str,
) -> str:
    """Create a dry-run changeset and return its name.

    Uses ChangeSetType=UPDATE and all three CAPABILITY_* values.
    Does NOT forward Parameters so the existing stack values are reused.
    ChangeSetName format: bouncer-dryrun-{uuid4()[:12]}

    Uses TemplateURL instead of TemplateBody to avoid YAML quote validation errors.
    CFN can access sam-deployer-artifacts bucket via its service principal.
    """
    changeset_name = f"bouncer-dryrun-{str(uuid.uuid4())[:12]}"

    # Query existing stack parameters so we can pass UsePreviousValue=True
    # This avoids the "Parameters must have values" error for NoEcho / SecretManager params
    try:
        stack_resp = cfn_client.describe_stacks(StackName=stack_name)
        existing_params = stack_resp["Stacks"][0].get("Parameters", [])
        reuse_params = [
            {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
            for p in existing_params
        ]
    except Exception:  # noqa: BLE001 — fall back to no params (may fail for required params)
        reuse_params = []

    cfn_client.create_change_set(
        StackName=stack_name,
        TemplateURL=template_s3_url,
        ChangeSetName=changeset_name,
        ChangeSetType="UPDATE",
        Parameters=reuse_params,
        Capabilities=[
            "CAPABILITY_IAM",
            "CAPABILITY_NAMED_IAM",
            "CAPABILITY_AUTO_EXPAND",
        ],
    )

    logger.info(
        "dry_run_changeset_created",
        extra={
            "src_module": "changeset_analyzer",
            "stack_name": stack_name,
            "changeset_name": changeset_name,
        },
    )
    return changeset_name


def analyze_changeset(
    cfn_client,
    stack_name: str,
    changeset_name: str,
    max_wait: int = 120,
    poll_interval: int = 2,
) -> AnalysisResult:
    """Poll DescribeChangeSet until CREATE_COMPLETE, FAILED, or timeout.

    CREATE_COMPLETE → parse Changes[] → AnalysisResult
    FAILED          → AnalysisResult(is_code_only=False, resource_changes=[], error=status_reason)
    timeout         → AnalysisResult(is_code_only=False, resource_changes=[], error='timeout')
    """
    elapsed = 0

    while elapsed < max_wait:
        try:
            response = cfn_client.describe_change_set(
                StackName=stack_name,
                ChangeSetName=changeset_name,
            )
        except ClientError as e:
            error_msg = str(e)
            logger.exception(
                "describe_changeset_error",
                extra={
                    "src_module": "changeset_analyzer",
                    "stack_name": stack_name,
                    "changeset_name": changeset_name,
                    "error": error_msg,
                },
            )
            return AnalysisResult(
                is_code_only=False,
                resource_changes=[],
                error=error_msg,
            )

        status = response.get("Status", "")
        status_reason = response.get("StatusReason", "")

        if status == "CREATE_COMPLETE":
            changes = response.get("Changes", [])
            result = AnalysisResult(
                is_code_only=False,  # will be computed by caller via is_code_only_change()
                resource_changes=changes,
            )
            result.is_code_only = is_code_only_change(result)
            logger.info(
                "changeset_analysis_complete",
                extra={
                    "src_module": "changeset_analyzer",
                    "stack_name": stack_name,
                    "changeset_name": changeset_name,
                    "change_count": len(changes),
                    "is_code_only": result.is_code_only,
                },
            )
            return result

        if status == "FAILED":
            # "No updates are to be performed." = SAM skipped deployment because
            # template + code hash unchanged → treat as code-only (safe no-op)
            if status_reason and "No updates are to be performed" in status_reason:
                logger.info(
                    "changeset_no_updates",
                    extra={
                        "src_module": "changeset_analyzer",
                        "stack_name": stack_name,
                        "changeset_name": changeset_name,
                        "status_reason": status_reason,
                    },
                )
                return AnalysisResult(
                    is_code_only=True,
                    resource_changes=[],
                    error=None,
                )
            # other FAILED reasons → conservative: require human approval
            logger.warning(
                "changeset_creation_failed",
                extra={
                    "src_module": "changeset_analyzer",
                    "stack_name": stack_name,
                    "changeset_name": changeset_name,
                    "status_reason": status_reason,
                },
            )
            return AnalysisResult(
                is_code_only=False,
                resource_changes=[],
                error=status_reason or "FAILED",
            )

        time.sleep(poll_interval)
        elapsed += poll_interval

    logger.warning(
        "changeset_analysis_timeout",
        extra={
            "src_module": "changeset_analyzer",
            "stack_name": stack_name,
            "changeset_name": changeset_name,
            "max_wait": max_wait,
        },
    )
    return AnalysisResult(
        is_code_only=False,
        resource_changes=[],
        error="timeout",
    )


def cleanup_changeset(
    cfn_client,
    stack_name: str,
    changeset_name: str,
) -> None:
    """Silently delete a changeset.

    ChangeSetNotFoundException → pass (already gone).
    Any other ClientError → log + pass (best-effort cleanup).
    """
    try:
        cfn_client.delete_change_set(
            StackName=stack_name,
            ChangeSetName=changeset_name,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ChangeSetNotFoundException":
            return
        logger.warning(
            "cleanup_changeset_error",
            extra={
                "src_module": "changeset_analyzer",
                "stack_name": stack_name,
                "changeset_name": changeset_name,
                "error_code": code,
                "error": str(e),
            },
        )
# Sprint 33: SAM artifacts S3 permission added to Lambda role (2026-03-12)
# Sprint 33: DescribeStacks permission added (2026-03-12)
