#!/usr/bin/env python3
"""
sam_deploy.py - SAM deployment script for CodeBuild

Reads environment variables set by the CodeBuild buildspec and invokes
`sam deploy` with the appropriate parameters.

Supports two SAM_PARAMS formats:
  1. JSON object: {"Key1": "Value1", "Key2": "Value2"}
  2. Legacy space-separated: Key1=Value1 Key2=Value2

Environment Variables:
  STACK_NAME     - CloudFormation stack name (required)
  SAM_PARAMS     - Parameter overrides (optional, JSON or legacy format)
  CFN_ROLE_ARN   - CloudFormation execution role ARN (optional)
  TARGET_ROLE_ARN - Cross-account assume role ARN (optional; when set, CFN_ROLE_ARN is skipped)
"""

import json
import os
import re
import subprocess
import sys
import warnings

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_CFN_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _validate_stack_name(name: str) -> None:
    """Ensure STACK_NAME is set and non-empty."""
    if not name:
        print("ERROR: STACK_NAME environment variable is empty or not set.", file=sys.stderr)
        sys.exit(1)


def _validate_param_key(key: str) -> None:
    """Reject parameter keys that don't look like valid CFN parameter names.

    This guards against argument-injection via crafted key names
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    stack = os.environ.get("STACK_NAME", "").strip()
    _validate_stack_name(stack)

    params_raw = os.environ.get("SAM_PARAMS", "").strip()

    cmd = [
        "sam", "deploy",
        "--stack-name", stack,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "CAPABILITY_NAMED_IAM",
        "--resolve-s3",
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
    ]

    base_len = len(cmd)  # length before adding parameter overrides

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
            # Legacy format: Key1=Value1 Key2=Value2
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

    # Use CFN execution role for local-account deploys (not cross-account)
    cfn_role = os.environ.get("CFN_ROLE_ARN", "").strip()
    target_role = os.environ.get("TARGET_ROLE_ARN", "").strip()
    if cfn_role and not target_role:
        cmd.extend(["--role-arn", cfn_role])
        print(f"Using CFN execution role: {cfn_role}")

    param_count = len(cmd) - base_len
    print(f"Running sam deploy --stack-name {stack} with {param_count} param args")
    sys.stdout.flush()

    result = subprocess.run(cmd, timeout=1800)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
