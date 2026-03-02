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
"""

from __future__ import annotations

import json
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
    "AWS::Logs::LogGroup": "LogGroupName",
}


def _physical_id_to_identifier(resource_type: str, physical_id: str) -> dict:
    """Convert a physical resource ID to the CFN import identifier dict."""
    key = _RESOURCE_ID_KEYS.get(resource_type, "Id")
    return {key: physical_id}


# ---------------------------------------------------------------------------
# SamPackager — encapsulates sam build + sam package for CFN import
# ---------------------------------------------------------------------------

# Resource types that require SAM-transformed templates for CFN IMPORT.
# SAM transforms AWS::Serverless::* → plain CFN types, and the IMPORT
# changeset must reference the post-transform template (not raw SAM YAML).
_SAM_TRANSFORM_TYPES: frozenset = frozenset(
    {
        "AWS::Logs::LogGroup",
        "AWS::Lambda::Function",
    }
)

# Template files that use the SAM Transform directive.
_SAM_TEMPLATE_NAMES: tuple = ("template.yaml", "template.yml")


class SamPackager:
    """Runs ``sam build`` + ``sam package`` and returns a packaged S3 template URL.

    Encapsulates the build/package pipeline so that :class:`CloudFormationImporter`
    stays focused on CFN operations and callers remain testable via dependency injection.

    Usage::

        packager = SamPackager(template_path="/path/to/template.yaml")
        s3_url = packager.build_and_package(s3_bucket="my-bucket")
        # → "https://s3.amazonaws.com/my-bucket/path/to/packaged.yaml"

    In production the S3 bucket for packaging is resolved from the environment
    variable ``SAM_PACKAGE_BUCKET``.  When running tests you can patch
    :meth:`build_and_package` directly.
    """

    def __init__(
        self,
        template_path: str,
        *,
        run_fn=None,
    ) -> None:
        self.template_path = template_path
        # Allow injection of a subprocess runner for testing
        self._run = run_fn or subprocess.run

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_bucket() -> str:
        """Return the S3 bucket to use for SAM package output.

        Reads SAM_PACKAGE_BUCKET from the environment; raises if not set.
        """
        bucket = os.environ.get("SAM_PACKAGE_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError(
                "SAM_PACKAGE_BUCKET environment variable is required for SAM packaging. "
                "Set it to an S3 bucket where packaged templates can be stored."
            )
        return bucket

    def _run_cmd(self, cmd: List[str]) -> None:
        """Run a subprocess command; raise on non-zero exit."""
        result = self._run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed (rc={result.returncode}): {' '.join(cmd)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_and_package(self, s3_bucket: Optional[str] = None) -> str:
        """Run ``sam build`` then ``sam package`` and return the S3 template URL.

        Args:
            s3_bucket: Override the bucket; defaults to ``SAM_PACKAGE_BUCKET`` env var.

        Returns:
            The S3 URL of the packaged (transformed) template, e.g.
            ``s3://bucket/uuid/template.yaml``.
        """
        bucket = s3_bucket or self._resolve_bucket()
        template_dir = os.path.dirname(os.path.abspath(self.template_path))

        print(f"[packager] sam build  (template: {self.template_path})")
        self._run_cmd(["sam", "build", "--template-file", self.template_path])

        import uuid

        s3_prefix = f"sam-packaged/{uuid.uuid4()}"
        output_template = os.path.join(template_dir, ".aws-sam", "packaged-template.yaml")

        print(f"[packager] sam package → s3://{bucket}/{s3_prefix}/")
        self._run_cmd(
            [
                "sam", "package",
                "--template-file", os.path.join(template_dir, ".aws-sam", "build", "template.yaml"),
                "--s3-bucket", bucket,
                "--s3-prefix", s3_prefix,
                "--output-template-file", output_template,
            ]
        )

        return f"s3://{bucket}/{s3_prefix}/packaged-template.yaml"


def _template_needs_sam_transform(template_path: str) -> bool:
    """Return True if the template uses SAM Transform (has AWS::Serverless:: resources)."""
    try:
        with open(template_path) as fh:
            content = fh.read()
        return "Transform: AWS::Serverless" in content
    except OSError:
        return False


def _find_sam_template(start_dir: Optional[str] = None) -> Optional[str]:
    """Walk up from *start_dir* looking for a SAM template file.

    Returns the first ``template.yaml`` / ``template.yml`` found that
    contains the SAM Transform directive, or *None* if not found.
    """
    search_dir = start_dir or os.getcwd()
    current = os.path.abspath(search_dir)
    for _ in range(6):  # max 6 levels up
        for name in _SAM_TEMPLATE_NAMES:
            candidate = os.path.join(current, name)
            if os.path.isfile(candidate) and _template_needs_sam_transform(candidate):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


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
        sam_packager: Optional["SamPackager"] = None,
    ) -> None:
        self.stack_name = stack_name
        self.dry_run = dry_run
        self._cfn_override = cfn_client  # None → lazy-init on first use
        self._sam_packager = sam_packager  # None → auto-detect from cwd

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

        # Determine if any conflict type requires a SAM-transformed template.
        # SAM YAML cannot be used directly for CFN IMPORT; we need the
        # post-transform template uploaded to S3 first.
        needs_sam_template = any(
            c.resource_type in _SAM_TRANSFORM_TYPES for c in conflicts
        )

        try:
            self._ensure_stack_exists()
            changeset_name = "auto-import-changeset"

            create_kwargs: dict = {
                "StackName": self.stack_name,
                "ChangeSetName": changeset_name,
                "ChangeSetType": "IMPORT",
                "ResourcesToImport": resources_to_import,
                "Capabilities": ["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "CAPABILITY_NAMED_IAM"],
            }

            if needs_sam_template:
                packaged_url = self._get_packaged_template_url()
                print(f"[import] Using SAM-packaged template: {packaged_url}")
                create_kwargs["TemplateURL"] = packaged_url

            print(f"[import] Creating import changeset '{changeset_name}' …")
            self._cfn.create_change_set(**create_kwargs)

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

    def _get_packaged_template_url(self) -> str:
        """Build + package the SAM template and return the S3 template URL.

        Uses the injected *sam_packager* if provided; otherwise auto-detects the
        SAM template from the working directory and creates a :class:`SamPackager`.
        """
        if self._sam_packager is not None:
            return self._sam_packager.build_and_package()

        template_path = _find_sam_template()
        if not template_path:
            raise RuntimeError(
                "No SAM template found in current directory tree. "
                "Set SAM_PACKAGE_BUCKET and ensure template.yaml is present, "
                "or inject a SamPackager instance."
            )
        packager = SamPackager(template_path)
        return packager.build_and_package()

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


def _build_sam_cmd(
    stack: str,
    params_raw: str,
    cfn_role: str,
    target_role: str,
) -> List[str]:
    """Construct the sam deploy command list."""
    cmd: List[str] = [
        "sam", "deploy",
        "--stack-name", stack,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "CAPABILITY_NAMED_IAM",
        "--resolve-s3",
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
    ]

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
    """Execute sam deploy and return a structured DeployResult."""
    result = subprocess.run(
        list(cmd),
        timeout=1800,
        capture_output=True,
        text=True,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    sys.stdout.flush()

    return DeployResult(
        status=DeployStatus.SUCCESS if result.returncode == 0 else DeployStatus.FAILED,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    dry_run_import = "--dry-run-import" in argv

    stack = os.environ.get("STACK_NAME", "").strip()
    _validate_stack_name(stack)

    params_raw = os.environ.get("SAM_PARAMS", "").strip()
    cfn_role = os.environ.get("CFN_ROLE_ARN", "").strip()
    target_role = os.environ.get("TARGET_ROLE_ARN", "").strip()

    cmd = _build_sam_cmd(stack, params_raw, cfn_role, target_role)
    sys.stdout.flush()

    # --- First deploy attempt ---
    deploy_result = _run_deploy(cmd)

    if deploy_result.succeeded:
        sys.exit(0)

    # --- Check for "already exists" conflicts ---
    combined_output = deploy_result.stdout + "\n" + deploy_result.stderr
    importer = CloudFormationImporter(stack, dry_run=dry_run_import)

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
