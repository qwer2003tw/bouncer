#!/usr/bin/env python3
"""
sam_deploy.py - SAM deployment script for CodeBuild

Reads environment variables set by the CodeBuild buildspec and invokes
``sam deploy`` with the appropriate parameters.  When a deploy fails because
one or more CloudFormation resources already exist outside the stack the
script automatically imports them and retries the deploy.

Supports two SAM_PARAMS formats:
  1. JSON object: {"Key1": "Value1", "Key2": "Value2"}
  2. Legacy space-separated: Key1=Value1 Key2=Value2

Environment Variables:
  STACK_NAME      - CloudFormation stack name (required)
  SAM_PARAMS      - Parameter overrides (optional, JSON or legacy format)
  CFN_ROLE_ARN    - CloudFormation execution role ARN (optional)
  TARGET_ROLE_ARN - Cross-account assume role ARN (optional; when set,
                    CFN_ROLE_ARN is skipped)

CLI Flags (appended after ``--``):
  --dry-run-import   Detect conflicts and print import plan without executing
  --suggest-import   Print JSON import plan to stdout and exit (machine-readable)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Sequence

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deploy mode constants
# ---------------------------------------------------------------------------

DEPLOY_MODE_PACKAGE_AND_CREATE = "package_and_create_changeset"
DEPLOY_MODE_EXECUTE = "execute_changeset"

# Regex to extract changeset ARN from sam deploy --no-execute-changeset output
_CHANGESET_ARN_RE = re.compile(
    r"(arn:aws:cloudformation:[^:]+:\d+:changeSet/\S+)"
)

# Regex to detect "No changes to deploy" from sam deploy output
_NO_CHANGES_RE = re.compile(r"No changes to deploy", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_CFN_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

# Pattern that CloudFormation / SAM emits when a resource already exists.
# Covers messages like:
#   "Resource handler returned message: "X already exists" (HandlerErrorCode)"
#   "… [ResourceLogicalId: SomeName, ResourceType: AWS::SQS::Queue, …]"
_ALREADY_EXISTS_RE = re.compile(
    r"already exists"
    r".*?"
    r"ResourceLogicalId:\s*(?P<logical>[^,\]]+)"
    r",\s*ResourceType:\s*(?P<rtype>[^,\]]+)"
    r",\s*ResourcePhysicalId:\s*(?P<physical>[^\]]+)",
    re.IGNORECASE | re.DOTALL,
)

# Simpler fallback: just capture the "already exists" part
_ALREADY_EXISTS_SIMPLE_RE = re.compile(r"already exists", re.IGNORECASE)

# Pattern for CloudFormation EarlyValidation hook failure.
# Emitted when a changeset fails due to resource existence conflicts *before*
# execution, e.g.:
#   "The following hook(s)/validation failed: [AWS::EarlyValidation::ResourceExistenceCheck]"
_EARLY_VALIDATION_RE = re.compile(
    r"AWS::EarlyValidation::ResourceExistenceCheck",
    re.IGNORECASE,
)

# CFN Import documentation URL
_CFN_IMPORT_DOCS_URL = (
    "https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/"
    "resource-import.html"
)


def _print_early_validation_hint(stack_name: str, output: str = "") -> None:
    """Print a structured [IMPORT NEEDED] hint when EarlyValidation is detected.

    Emits:
    - A clear [IMPORT NEEDED] header
    - Explanation of the error
    - Suggested AWS CLI import command skeleton (--resources-to-import format)
    - Link to CFN import docs
    """
    print("\n" + "=" * 70)
    print("[IMPORT NEEDED] CloudFormation EarlyValidation conflict detected!")
    print("=" * 70)
    print(
        "\nCloudFormation detected that one or more resources in your template\n"
        "already exist outside this stack (AWS::EarlyValidation::ResourceExistenceCheck).\n"
        "\nYou must import these resources into the stack before deploying.\n"
    )
    print("Suggested steps:")
    print(
        "  1. Identify the conflicting resources via:\n"
        f"       aws cloudformation describe-stack-events \\\n"
        f"           --stack-name {stack_name}\n"
    )
    print(
        "  2. Build a resources-to-import JSON file, e.g. import.json:\n"
        "       [\n"
        '         {\n'
        '           "ResourceType": "AWS::S3::Bucket",\n'
        '           "LogicalResourceId": "MyBucket",\n'
        '           "ResourceIdentifier": {"BucketName": "my-existing-bucket"}\n'
        '         }\n'
        "       ]\n"
    )
    print(
        "  3. Create an import changeset:\n"
        f"       aws cloudformation create-change-set \\\n"
        f"           --stack-name {stack_name} \\\n"
        "           --change-set-name import-changeset \\\n"
        "           --change-set-type IMPORT \\\n"
        "           --resources-to-import file://import.json \\\n"
        "           --template-body file://template.yaml\n"
    )
    print(
        "  4. Execute the import changeset:\n"
        f"       aws cloudformation execute-change-set \\\n"
        f"           --stack-name {stack_name} \\\n"
        "           --change-set-name import-changeset\n"
    )
    print(
        "  Alternatively, re-run this script with --dry-run-import to preview\n"
        "  the auto-import plan (if resource details are parseable).\n"
    )
    print(f"📖 Docs: {_CFN_IMPORT_DOCS_URL}")
    print("=" * 70 + "\n")


def _build_suggest_import_json(conflicts: "List[ConflictResource]") -> str:
    """Return a JSON string of the import plan for --suggest-import output."""
    records = [c.to_import_record() for c in conflicts]
    return json.dumps(records, indent=2)


def _validate_stack_name(name: str) -> None:
    """Ensure STACK_NAME is set and non-empty."""
    if not name:
        print("ERROR: STACK_NAME environment variable is empty or not set.", file=sys.stderr)
        sys.exit(1)


def _validate_param_key(key: str) -> None:
    """Reject parameter keys that don't look like valid CFN parameter names.

    Guards against argument-injection via crafted key names
    (e.g. ``--some-flag`` masquerading as a key).
    """
    if not _CFN_KEY_RE.match(key):
        print(
            f"ERROR: Invalid parameter key '{key}'. "
            "Keys must start with a letter and contain only alphanumeric characters.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class DeployStatus(Enum):
    SUCCESS = auto()
    CONFLICT = auto()
    IMPORT_NEEDED = auto()
    FAILED = auto()


@dataclass
class ConflictResource:
    """A CloudFormation resource that already exists outside the stack."""

    logical_id: str
    resource_type: str
    physical_id: str

    def to_import_record(self) -> dict:
        """Return the dict expected by CFN import API."""
        return {
            "ResourceType": self.resource_type.strip(),
            "LogicalResourceId": self.logical_id.strip(),
            "ResourceIdentifier": _physical_id_to_identifier(
                self.resource_type.strip(), self.physical_id.strip()
            ),
        }


@dataclass
class DeployResult:
    """Structured result from a deploy attempt."""

    status: DeployStatus
    returncode: int
    stdout: str = ""
    stderr: str = ""
    conflicts: List[ConflictResource] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == DeployStatus.SUCCESS

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


# ---------------------------------------------------------------------------
# Resource identifier helpers
# ---------------------------------------------------------------------------

# Mapping from CFN resource type to the identifier key name used by the
# IMPORT operation.  Extend as needed.
_RESOURCE_ID_KEYS: Dict[str, str] = {
    "AWS::SQS::Queue": "QueueUrl",
    "AWS::SNS::Topic": "TopicArn",
    "AWS::DynamoDB::Table": "TableName",
    "AWS::S3::Bucket": "BucketName",
    "AWS::IAM::Role": "RoleName",
    "AWS::IAM::Policy": "PolicyArn",
    "AWS::Lambda::Function": "FunctionName",
    "AWS::CloudWatch::Alarm": "AlarmName",
    "AWS::SecretsManager::Secret": "Id",
    "AWS::SSM::Parameter": "Name",
    "AWS::KMS::Key": "KeyId",
    "AWS::KMS::Alias": "AliasName",
    "AWS::EC2::SecurityGroup": "GroupId",
    "AWS::EC2::Subnet": "SubnetId",
    "AWS::EC2::VPC": "VpcId",
}


def _physical_id_to_identifier(resource_type: str, physical_id: str) -> dict:
    """Convert a physical resource ID to the CFN import identifier dict."""
    key = _RESOURCE_ID_KEYS.get(resource_type, "Id")
    return {key: physical_id}


# ---------------------------------------------------------------------------
# CloudFormationImporter
# ---------------------------------------------------------------------------


class CloudFormationImporter:
    """Handles detection and resolution of CloudFormation resource conflicts.

    Encapsulates all boto3 CloudFormation import logic behind a clean
    interface so that the main deploy flow stays readable.
    """

    def __init__(
        self,
        stack_name: str,
        cfn_client=None,
        *,
        dry_run: bool = False,
    ) -> None:
        self.stack_name = stack_name
        self.dry_run = dry_run
        self._cfn_override = cfn_client  # None → lazy-init on first use

    @property
    def _cfn(self):
        """Lazily create the boto3 CFN client so tests can patch boto3.client."""
        if self._cfn_override is None:
            self._cfn_override = boto3.client("cloudformation")
        return self._cfn_override

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_conflicts(self, output: str) -> List[ConflictResource]:
        """Extract conflict resources from sam/cfn error output."""
        conflicts: List[ConflictResource] = []
        seen: set = set()

        for m in _ALREADY_EXISTS_RE.finditer(output):
            logical = m.group("logical").strip()
            rtype = m.group("rtype").strip()
            physical = m.group("physical").strip()
            key = (logical, rtype, physical)
            if key not in seen:
                seen.add(key)
                conflicts.append(ConflictResource(logical, rtype, physical))

        return conflicts

    def has_conflict_error(self, output: str) -> bool:
        """Return True if the output contains any "already exists" message."""
        return bool(_ALREADY_EXISTS_SIMPLE_RE.search(output))

    def import_resources(self, conflicts: List[ConflictResource]) -> bool:
        """Import all conflicting resources into the stack in one changeset.

        Returns True on success, False on failure.
        """
        if not conflicts:
            return True

        print(
            f"[import] Preparing to import {len(conflicts)} resource(s) "
            f"into stack '{self.stack_name}':"
        )
        for c in conflicts:
            print(f"  • {c.logical_id} ({c.resource_type}) → {c.physical_id}")

        if self.dry_run:
            print("[dry-run] Import plan printed above. Skipping execution.")
            return True

        resources_to_import = [c.to_import_record() for c in conflicts]

        try:
            self._ensure_stack_exists()
            changeset_name = "auto-import-changeset"

            print(f"[import] Creating import changeset '{changeset_name}' …")
            self._cfn.create_change_set(
                StackName=self.stack_name,
                ChangeSetName=changeset_name,
                ChangeSetType="IMPORT",
                ResourcesToImport=resources_to_import,
                Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "CAPABILITY_NAMED_IAM"],
            )

            self._wait_for_changeset(changeset_name)

            print("[import] Executing import changeset …")
            self._cfn.execute_change_set(
                StackName=self.stack_name,
                ChangeSetName=changeset_name,
            )

            self._wait_for_stack_stable()
            print("[import] Import completed successfully.")
            return True

        except (ClientError, Exception) as exc:  # noqa: BLE001
            print(f"ERROR: Import failed: {exc}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_stack_exists(self) -> None:
        """Create an empty stack if it doesn't exist yet (needed for IMPORT)."""
        try:
            self._cfn.describe_stacks(StackName=self.stack_name)
        except ClientError as exc:
            if "does not exist" in str(exc):
                print(f"[import] Stack '{self.stack_name}' not found; creating empty stack …")
                self._cfn.create_stack(
                    StackName=self.stack_name,
                    TemplateBody='{"AWSTemplateFormatVersion":"2010-09-09","Resources":{}}',
                    Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "CAPABILITY_NAMED_IAM"],
                )
                waiter = self._cfn.get_waiter("stack_create_complete")
                waiter.wait(StackName=self.stack_name)
            else:
                raise

    def _wait_for_changeset(self, changeset_name: str) -> None:
        """Poll until changeset reaches a terminal state."""
        import time

        while True:
            resp = self._cfn.describe_change_set(
                StackName=self.stack_name,
                ChangeSetName=changeset_name,
            )
            status = resp["Status"]
            if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                return
            if "FAILED" in status:
                reason = resp.get("StatusReason", "unknown")
                raise RuntimeError(f"Changeset failed: {status} — {reason}")
            time.sleep(3)

    def _wait_for_stack_stable(self) -> None:
        """Wait for the stack to reach a stable state after import."""
        waiter = self._cfn.get_waiter("stack_import_complete")
        waiter.wait(StackName=self.stack_name)


# ---------------------------------------------------------------------------
# Deploy orchestration
# ---------------------------------------------------------------------------

_PACKAGED_TEMPLATE = "/tmp/packaged-template.yaml"


def _run_sam_package(artifacts_bucket: str, project_id: str) -> None:
    """Run ``sam package`` to upload Lambda artifacts and produce a packaged template.

    The resulting template file is written to ``_PACKAGED_TEMPLATE`` and later
    consumed by ``_build_sam_cmd`` so that ``sam deploy`` does not need
    ``--resolve-s3``.

    Also uploads the packaged template YAML to S3 so that changeset_analyzer
    can fetch it for dry-run changesets.

    This must be called *after* ``sam build`` and *before* any cross-account
    assume-role, so the S3 upload uses the original CodeBuild credentials.
    """
    cmd = [
        "sam", "package",
        "--s3-bucket", artifacts_bucket,
        "--s3-prefix", f"{project_id}/templates",
        "--output-template-file", _PACKAGED_TEMPLATE,
        "--force-upload",  # Always upload even if hash matches (fix #292)
    ]
    print(f"[package] sam package → s3://{artifacts_bucket}/{project_id}/templates/")
    subprocess.run(cmd, check=True)
    print(f"[package] Packaged template written to {_PACKAGED_TEMPLATE}")

    # Upload the packaged YAML template to S3 with a stable key so changeset_analyzer can fetch it
    template_s3_key = f"{project_id}/packaged-template.yaml"
    try:
        import boto3 as _boto3
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        s3 = _boto3.client("s3", region_name=region)
        with open(_PACKAGED_TEMPLATE, "rb") as f:
            s3.put_object(
                Bucket=artifacts_bucket,
                Key=template_s3_key,
                Body=f.read(),
                ContentType="application/x-yaml",
            )
        print(f"[package] Uploaded packaged template YAML to s3://{artifacts_bucket}/{template_s3_key}")
    except Exception as exc:
        print(f"[package] Warning: failed to upload packaged template YAML: {exc}")
        # Non-fatal


def _notify_sfn_package_complete(project_id: str, artifacts_bucket: str) -> None:
    """Notify Step Functions that sam package is complete.

    Called after _run_sam_package() succeeds. Sends taskToken back to SFN
    so the AnalyzeChangeset state can proceed.

    Security: taskToken is read from env var and used in-memory only.
    It is NEVER logged or persisted.
    """
    task_token = os.environ.get("SFN_TASK_TOKEN", "").strip()
    if not task_token:
        print("[sfn] SFN_TASK_TOKEN not set — skipping SendTaskSuccess (non-SFN deploy mode)")
        return

    template_s3_key = f"{project_id}/packaged-template.yaml"

    try:
        import boto3 as _boto3_sfn
        import json as _json
        sfn = _boto3_sfn.client("stepfunctions", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        sfn.send_task_success(
            taskToken=task_token,
            output=_json.dumps({
                "template_s3_key": template_s3_key,
                "project_id": project_id,
                "artifacts_bucket": artifacts_bucket,
            })
        )
        print(f"[sfn] SendTaskSuccess — package complete, template_s3_key={template_s3_key}")
    except Exception as exc:  # noqa: BLE001
        # Fail-safe: if SendTaskSuccess fails, SFN will timeout via HeartbeatSeconds
        # Do NOT raise — let CodeBuild exit cleanly
        print(f"[sfn] Warning: SendTaskSuccess failed: {exc}")


def _notify_progress(deploy_id: str, project_id: str, phase: str) -> None:
    """Invoke NotifierLambda to update deploy progress message.

    Non-fatal: exceptions are caught and printed; deploy continues regardless.

    Args:
        deploy_id: Deploy ID
        project_id: Project ID
        phase: Phase name — SCANNING, BUILDING, or DEPLOYING
    """
    notifier_arn = os.environ.get('NOTIFIER_LAMBDA_ARN', '')
    if not notifier_arn:
        print(f"[progress] NOTIFIER_LAMBDA_ARN not set — skipping progress update ({phase})")
        return

    try:
        import boto3 as _boto3_lambda
        import json as _json
        lambda_client = _boto3_lambda.client('lambda', region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
        payload = {
            'action': 'progress',
            'deploy_id': deploy_id,
            'project_id': project_id,
            'phase': phase,
        }
        lambda_client.invoke(
            FunctionName=notifier_arn,
            InvocationType='Event',  # async, fire-and-forget
            Payload=_json.dumps(payload).encode(),
        )
        print(f"[progress] Notified phase={phase}")
    except Exception as e:  # noqa: BLE001 — non-fatal
        print(f"[progress] Failed to notify phase={phase}: {e}")


def update_template_s3_url(project_id: str, artifacts_bucket: str) -> None:
    """Persist the packaged template S3 URL into the bouncer-projects DDB table.

    Must be called *before* any cross-account assume-role so that the update
    uses the original CodeBuild credentials (main-account IAM role), not the
    assumed cross-account role.

    Non-fatal: exceptions are caught and printed; deploy continues regardless.

    The S3 key is discovered by listing the prefix and picking the most-recently
    modified object — SAM uploads a content-addressed key (hash), not a fixed name.
    """
    if not project_id or not artifacts_bucket:
        print("[DDB] Skipping template_s3_url update: PROJECT_ID or ARTIFACTS_BUCKET not set")
        return
    try:
        # Use the stable key uploaded by _run_sam_package
        s3_key = f"{project_id}/packaged-template.yaml"
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        template_url = f"https://{artifacts_bucket}.s3.amazonaws.com/{s3_key}"

        projects_table = os.environ.get("PROJECTS_TABLE", "bouncer-projects")
        ddb = boto3.client("dynamodb", region_name=region)
        ddb.update_item(
            TableName=projects_table,
            Key={"project_id": {"S": project_id}},
            UpdateExpression="SET template_s3_url = :url",
            ExpressionAttributeValues={":url": {"S": template_url}},
        )
        print(f"[DDB] Updated template_s3_url for {project_id}: {template_url}")
    except Exception as exc:  # noqa: BLE001
        print(f"[DDB] Warning: failed to update template_s3_url: {exc}")
        # Non-fatal: don't break deploy


def _update_deploy_history(deploy_id: str, data: dict) -> None:
    """Write changeset metadata to the deploy-history DDB table.

    Non-fatal: exceptions are caught and printed; deploy continues regardless.
    Must be called BEFORE any cross-account assume-role so that the DDB write
    uses the original CodeBuild IAM credentials (main account).
    """
    if not deploy_id:
        print("[DDB] Skipping deploy history update: DEPLOY_ID not set")
        return
    try:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        table_name = os.environ.get("HISTORY_TABLE", "bouncer-deploy-history")
        ddb = boto3.resource("dynamodb", region_name=region)
        table = ddb.Table(table_name)
        update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in data)
        expr_names = {f"#{k}": k for k in data}
        expr_values = {f":{k}": v for k, v in data.items()}
        table.update_item(
            Key={"deploy_id": deploy_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        print(f"[DDB] Updated deploy history for {deploy_id}: {list(data.keys())}")
    except Exception as exc:  # noqa: BLE001
        print(f"[DDB] Warning: failed to update deploy history: {exc}")


def _get_deploy_history(deploy_id: str) -> dict:
    """Read deploy history from DDB.

    Non-fatal: exceptions return empty dict.
    """
    if not deploy_id:
        return {}
    try:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        table_name = os.environ.get("HISTORY_TABLE", "bouncer-deploy-history")
        ddb = boto3.resource("dynamodb", region_name=region)
        table = ddb.Table(table_name)
        result = table.get_item(Key={"deploy_id": deploy_id})
        return result.get("Item", {})
    except Exception as exc:  # noqa: BLE001
        print(f"[DDB] Warning: failed to read deploy history: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Phase 1: Create changeset (sam deploy --no-execute-changeset)
# ---------------------------------------------------------------------------


def _run_create_changeset(
    cmd: List[str],
    deploy_id: str,
) -> None:
    """Run sam deploy with --no-execute-changeset and store changeset info in DDB.

    Parses the changeset ARN from stdout.  If SAM reports "No changes to deploy",
    writes no_changes=True to DDB.  On any failure → sys.exit(1).
    """
    # Add --no-execute-changeset to the command
    full_cmd = list(cmd) + ["--no-execute-changeset"]
    print("[changeset] Running sam deploy --no-execute-changeset ...")

    deploy_result = _run_deploy(full_cmd)

    # Check for "No changes to deploy"
    if _NO_CHANGES_RE.search(deploy_result.stdout):
        print("[changeset] No changes to deploy — marking no_changes=true in DDB")
        _update_deploy_history(deploy_id, {
            "changeset_name": "",
            "no_changes": True,
        })
        return

    if not deploy_result.succeeded:
        print(
            f"ERROR: sam deploy --no-execute-changeset failed (rc={deploy_result.returncode})",
            file=sys.stderr,
        )
        sys.exit(deploy_result.returncode or 1)

    # Parse changeset ARN from output
    m = _CHANGESET_ARN_RE.search(deploy_result.stdout)
    if m:
        changeset_arn = m.group(1)
        print(f"[changeset] Created changeset: {changeset_arn}")
        _update_deploy_history(deploy_id, {
            "changeset_name": changeset_arn,
            "no_changes": False,
        })
    else:
        # Fallback: try listing changesets via CloudFormation API
        stack = os.environ.get("STACK_NAME", "").strip()
        changeset_arn = _find_latest_changeset(stack)
        if changeset_arn:
            print(f"[changeset] Found changeset via API: {changeset_arn}")
            _update_deploy_history(deploy_id, {
                "changeset_name": changeset_arn,
                "no_changes": False,
            })
        else:
            print(
                "WARNING: Could not determine changeset name from output or API. "
                "Storing empty changeset_name — AnalyzeChangeset will fail-safe.",
                file=sys.stderr,
            )
            _update_deploy_history(deploy_id, {
                "changeset_name": "",
                "no_changes": False,
            })


def _find_latest_changeset(stack_name: str) -> str:
    """List changesets for the stack and return the most recent available one.

    Returns the changeset ARN/name, or empty string if none found.
    """
    if not stack_name:
        return ""
    try:
        cfn = boto3.client("cloudformation")
        resp = cfn.list_change_sets(StackName=stack_name)
        for cs in resp.get("Summaries", []):
            if cs.get("ExecutionStatus") == "AVAILABLE":
                return cs.get("ChangeSetId", cs.get("ChangeSetName", ""))
        return ""
    except Exception as exc:  # noqa: BLE001
        print(f"[changeset] Warning: list_change_sets failed: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Phase 2: Execute changeset
# ---------------------------------------------------------------------------


def _run_execute_changeset(
    stack: str,
    deploy_id: str,
) -> None:
    """Read changeset info from DDB and execute it.

    Steps:
    1. Read changeset_name and no_changes from DDB
    2. If no_changes → exit 0
    3. Execute the changeset
    4. Wait for stack update to complete (15 min timeout)
    5. Best-effort cleanup of the changeset
    """
    history = _get_deploy_history(deploy_id)
    changeset_name = history.get("changeset_name", "")
    no_changes = history.get("no_changes", False)

    if no_changes:
        print("[execute] no_changes=true — nothing to deploy, exiting successfully")
        sys.exit(0)

    if not changeset_name:
        print(
            "ERROR: No changeset_name found in deploy history. "
            "Phase 1 may have failed to create a changeset.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[execute] Executing changeset: {changeset_name}")

    try:
        cfn = boto3.client("cloudformation")

        cfn.execute_change_set(
            StackName=stack,
            ChangeSetName=changeset_name,
        )
        print("[execute] execute-change-set initiated, waiting for stack update ...")

        # Wait for stack update to complete (15 min timeout)
        waiter = cfn.get_waiter("stack_update_complete")
        waiter.wait(
            StackName=stack,
            WaiterConfig={"Delay": 10, "MaxAttempts": 90},  # 10s * 90 = 15 min
        )
        print("[execute] Stack update completed successfully.")

    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: execute-change-set failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Best-effort cleanup of the changeset
        try:
            cfn_cleanup = boto3.client("cloudformation")
            cfn_cleanup.delete_change_set(
                StackName=stack,
                ChangeSetName=changeset_name,
            )
            print(f"[execute] Cleaned up changeset: {changeset_name}")
        except Exception as cleanup_exc:  # noqa: BLE001
            print(f"[execute] Cleanup of changeset failed (non-critical): {cleanup_exc}")

    sys.exit(0)


def _build_sam_cmd(
    stack: str,
    params_raw: str,
    cfn_role: str,
    target_role: str,
    artifacts_bucket: str = "",
) -> List[str]:
    """Construct the sam deploy command list."""
    cmd: List[str] = [
        "sam", "deploy",
        "--stack-name", stack,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "CAPABILITY_NAMED_IAM",
        "--template-file", _PACKAGED_TEMPLATE,
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
    ]
    # SAM CLI requires --s3-bucket when the packaged template exceeds 51,200 bytes.
    # Pass the artifacts bucket so SAM can upload the template automatically.
    if artifacts_bucket:
        cmd.extend(["--s3-bucket", artifacts_bucket])

    base_len = len(cmd)

    if params_raw:
        cmd.append("--parameter-overrides")
        try:
            params = json.loads(params_raw)
            if not isinstance(params, dict):
                raise ValueError("SAM_PARAMS JSON must be an object")
            for k, v in params.items():
                _validate_param_key(k)
                cmd.append(f"{k}={v}")
        except (json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "must be an object" in str(exc):
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(1)
            warnings.warn(
                "SAM_PARAMS is not valid JSON; falling back to legacy "
                "space-separated format. Consider migrating to JSON.",
                stacklevel=2,
            )
            parts = re.split(r"\s+(?=\w+=)", params_raw)
            cmd.extend(parts)

    if cfn_role and not target_role:
        cmd.extend(["--role-arn", cfn_role])
        print(f"Using CFN execution role: {cfn_role}")

    param_count = len(cmd) - base_len
    print(f"Running sam deploy --stack-name {stack} with {param_count} param args")

    return cmd


def _run_deploy(cmd: Sequence[str]) -> DeployResult:
    """Execute sam deploy and return a structured DeployResult (streaming output)."""
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines: List[str] = []
    for line in process.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        output_lines.append(line)
        logger.info("[sam] %s", line)  # streaming log
        print(line)
    process.wait()
    sys.stdout.flush()
    output = "\n".join(output_lines)

    return DeployResult(
        status=DeployStatus.SUCCESS if process.returncode == 0 else DeployStatus.FAILED,
        returncode=process.returncode,
        stdout=output,
        stderr="",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



def _check_github_pat() -> None:
    """Validate GitHub PAT before attempting git clone.

    Exits with clear error message if PAT is expired/invalid (HTTP 401).
    Gracefully skips validation on API errors (network, rate limit, etc.).
    """
    token = os.environ.get("GITHUB_PAT", "").strip()
    if not token:
        return  # No PAT in env, skip check

    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}", "User-Agent": "Bouncer/1.0"},
        )
        urllib.request.urlopen(req, timeout=5)
        print("[PAT] GitHub PAT is valid.")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(
                "[PAT] ERROR: GitHub PAT is expired or invalid (HTTP 401).\n"
                "Update the secret at: sam-deployer/github-pat (Secrets Manager, us-east-1)\n"
                "Then retry the deploy.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Other HTTP errors (403 rate limit, 5xx) → graceful degradation
        print(f"[PAT] GitHub API returned {e.code}, skipping PAT validation.")
    except Exception as exc:
        print(f"[PAT] Could not reach GitHub API ({exc}), skipping PAT validation.")

def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    _check_github_pat()  # Validate PAT before git clone

    dry_run_import = "--dry-run-import" in argv
    suggest_import = "--suggest-import" in argv

    stack = os.environ.get("STACK_NAME", "").strip()
    _validate_stack_name(stack)

    params_raw = os.environ.get("SAM_PARAMS", "").strip()
    cfn_role = os.environ.get("CFN_ROLE_ARN", "").strip()
    target_role = os.environ.get("TARGET_ROLE_ARN", "").strip()
    artifacts_bucket = os.environ.get("ARTIFACTS_BUCKET", "").strip()
    project_id = os.environ.get("PROJECT_ID", "").strip()
    deploy_id = os.environ.get("DEPLOY_ID", "").strip()
    skip_package = os.environ.get("SKIP_PACKAGE", "").lower() == "true"
    deploy_mode = os.environ.get("DEPLOY_MODE", "").strip()

    # --- DEPLOY_MODE=execute_changeset: Phase 2 — execute pre-created changeset ---
    if deploy_mode == DEPLOY_MODE_EXECUTE:
        print(f"[mode] DEPLOY_MODE={deploy_mode} — executing pre-created changeset")
        _notify_progress(deploy_id, project_id, "DEPLOYING")
        _run_execute_changeset(stack, deploy_id)
        # _run_execute_changeset calls sys.exit() — should not reach here
        return

    # --- sam package: upload Lambda artifacts + produce packaged template ---
    # Must run BEFORE any cross-account assume-role so the S3 upload uses the
    # original CodeBuild IAM credentials (main account).
    # SamDeploy state may set SKIP_PACKAGE=true to skip this (reusing prior packaged template).
    if not skip_package:
        _notify_progress(deploy_id, project_id, 'SCANNING')
        _run_sam_package(artifacts_bucket, project_id)
        # Notify Step Functions that package is complete
        _notify_sfn_package_complete(project_id, artifacts_bucket)

        # --- Update template_s3_url in DDB (before assume-role) ---
        # After assume-role the env-var credentials are overwritten with the
        # cross-account role creds; DDB is in the main account, so we must call
        # this before the role switch.
        update_template_s3_url(project_id, artifacts_bucket)
    else:
        print("[package] SKIP_PACKAGE=true — skipping sam package step")
        # When skipping package, we must still have the packaged template available.
        # Download it from S3 (uploaded by the previous package build).
        if artifacts_bucket and project_id:
            s3_key = f"{project_id}/packaged-template.yaml"
            print(f"[package] Downloading packaged template from s3://{artifacts_bucket}/{s3_key}")
            try:
                import boto3 as _boto3_s3
                s3 = _boto3_s3.client("s3")
                s3.download_file(artifacts_bucket, s3_key, _PACKAGED_TEMPLATE)
                print(f"[package] Downloaded packaged template to {_PACKAGED_TEMPLATE}")
            except Exception as exc:
                print(f"ERROR: Failed to download packaged template: {exc}", file=sys.stderr)
                sys.exit(1)
        else:
            print("ERROR: SKIP_PACKAGE=true but ARTIFACTS_BUCKET or PROJECT_ID not set", file=sys.stderr)
            sys.exit(1)

    _notify_progress(deploy_id, project_id, 'BUILDING')
    cmd = _build_sam_cmd(stack, params_raw, cfn_role, target_role, artifacts_bucket)
    sys.stdout.flush()

    # --- DEPLOY_MODE=package_and_create_changeset: Phase 1 — create only ---
    if deploy_mode == DEPLOY_MODE_PACKAGE_AND_CREATE:
        print(f"[mode] DEPLOY_MODE={deploy_mode} — creating changeset without executing")
        _notify_progress(deploy_id, project_id, "DEPLOYING")
        _run_create_changeset(cmd, deploy_id)
        sys.exit(0)

    # --- Default mode: full deploy (backward compatible) ---
    # --- First deploy attempt ---
    _notify_progress(deploy_id, project_id, 'DEPLOYING')
    deploy_result = _run_deploy(cmd)

    if deploy_result.succeeded:
        sys.exit(0)

    # --- Check for "already exists" conflicts ---
    combined_output = deploy_result.stdout + "\n" + deploy_result.stderr
    importer = CloudFormationImporter(stack, dry_run=dry_run_import)

    # --- Check for EarlyValidation hook failure (detected BEFORE "already exists") ---
    if _EARLY_VALIDATION_RE.search(combined_output):
        print(
            f"ERROR: sam deploy failed (rc={deploy_result.returncode}) "
            "with EarlyValidation conflict.",
            file=sys.stderr,
        )
        _print_early_validation_hint(stack, combined_output)

        # --suggest-import: also try to produce structured JSON plan if we can parse
        if suggest_import:
            conflicts = importer.parse_conflicts(combined_output)
            if conflicts:
                print("[SUGGEST-IMPORT] JSON import plan:", file=sys.stderr)
                print(_build_suggest_import_json(conflicts))
            else:
                print(
                    "[SUGGEST-IMPORT] Could not parse resource details from output. "
                    "Use `aws cloudformation describe-stack-events` to identify resources.",
                    file=sys.stderr,
                )

        sys.exit(deploy_result.returncode)

    if not importer.has_conflict_error(combined_output):
        # Non-conflict failure — surface it and exit
        print(
            f"ERROR: sam deploy failed (rc={deploy_result.returncode}) "
            "with no importable conflicts.",
            file=sys.stderr,
        )
        sys.exit(deploy_result.returncode)

    # --- Parse and import conflicting resources ---
    conflicts = importer.parse_conflicts(combined_output)

    if not conflicts:
        # We saw "already exists" but couldn't parse resource details.
        print(
            "ERROR: Deploy failed with 'already exists' error but could not "
            "parse resource details for import. Manual intervention required.",
            file=sys.stderr,
        )
        sys.exit(deploy_result.returncode)

    if dry_run_import:
        # Print plan and exit cleanly (non-zero to signal "not deployed")
        importer.import_resources(conflicts)
        print("[dry-run] Exiting without deploying.", file=sys.stderr)
        sys.exit(2)

    # --suggest-import: print JSON plan to stderr and exit without executing
    if suggest_import:
        print("[SUGGEST-IMPORT] JSON import plan:", file=sys.stderr)
        print(_build_suggest_import_json(conflicts), file=sys.stderr)
        sys.exit(2)

    import_ok = importer.import_resources(conflicts)
    if not import_ok:
        print(
            "ERROR: Auto-import failed. Aborting deployment. "
            "Resolve the conflicts manually and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Retry deploy after successful import ---
    print("\n[import] Retrying sam deploy after import …\n")
    sys.stdout.flush()
    retry_result = _run_deploy(cmd)
    sys.exit(retry_result.returncode)


if __name__ == "__main__":
    main()
