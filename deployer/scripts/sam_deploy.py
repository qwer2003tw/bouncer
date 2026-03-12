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

    This must be called *after* ``sam build`` and *before* any cross-account
    assume-role, so the S3 upload uses the original CodeBuild credentials.
    """
    cmd = [
        "sam", "package",
        "--s3-bucket", artifacts_bucket,
        "--s3-prefix", f"{project_id}/templates",
        "--output-template-file", _PACKAGED_TEMPLATE,
    ]
    print(f"[package] sam package → s3://{artifacts_bucket}/{project_id}/templates/")
    subprocess.run(cmd, check=True)
    print(f"[package] Packaged template written to {_PACKAGED_TEMPLATE}")


def update_template_s3_url(project_id: str, artifacts_bucket: str) -> None:
    """Persist the packaged template S3 URL into the bouncer-projects DDB table.

    Must be called *before* any cross-account assume-role so that the update
    uses the original CodeBuild credentials (main-account IAM role), not the
    assumed cross-account role.

    Non-fatal: exceptions are caught and printed; deploy continues regardless.
    """
    if not project_id or not artifacts_bucket:
        print("[DDB] Skipping template_s3_url update: PROJECT_ID or ARTIFACTS_BUCKET not set")
        return
    try:
        s3_key = f"{project_id}/templates/packaged-template.yaml"
        template_url = f"https://{artifacts_bucket}.s3.amazonaws.com/{s3_key}"

        projects_table = os.environ.get("PROJECTS_TABLE", "bouncer-projects")
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

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
        "--template-file", _PACKAGED_TEMPLATE,
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

    # --- sam package: upload Lambda artifacts + produce packaged template ---
    # Must run BEFORE any cross-account assume-role so the S3 upload uses the
    # original CodeBuild IAM credentials (main account).
    _run_sam_package(artifacts_bucket, project_id)

    # --- Update template_s3_url in DDB (before assume-role) ---
    # After assume-role the env-var credentials are overwritten with the
    # cross-account role creds; DDB is in the main account, so we must call
    # this before the role switch.
    update_template_s3_url(project_id, artifacts_bucket)

    cmd = _build_sam_cmd(stack, params_raw, cfn_role, target_role)
    sys.stdout.flush()

    # --- First deploy attempt ---
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
