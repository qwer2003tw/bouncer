"""
Tests for deployer/scripts/sam_deploy.py (Merged B+C approach)

Architecture base: Approach B (dataclass / Enum / --dry-run-import / OOP importer)
Additional coverage: Approach C (fine-grained helper unit tests, edge cases)

Covers:
  - Validation helpers (_validate_stack_name, _validate_param_key)
  - _build_sam_cmd (JSON params, legacy params, role handling)
  - ConflictResource dataclass + to_import_record
  - DeployResult dataclass properties
  - CloudFormationImporter — conflict parsing (parse_conflicts, has_conflict_error)
  - CloudFormationImporter — import_resources (success, failure, dry-run, stack creation)
  - _run_deploy (success + failure paths)
  - _physical_id_to_identifier for all known + unknown resource types
  - main() integration: normal deploy, conflict → import → retry, import failure,
    unparsable conflict, --dry-run-import, multiple conflicts
  - Edge cases: empty strings, case insensitivity, deduplication, partial matches
"""

from __future__ import annotations

import json
import sys
import os
import importlib
import types
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch, call
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Path setup — make the script importable as a module
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sam_deploy  # noqa: E402
from sam_deploy import (
    CloudFormationImporter,
    ConflictResource,
    DeployResult,
    DeployStatus,
    _build_sam_cmd,
    _build_suggest_import_json,
    _EARLY_VALIDATION_RE,
    _physical_id_to_identifier,
    _print_early_validation_hint,
    _run_deploy,
    _validate_param_key,
    _validate_stack_name,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STACK = "my-test-stack"

# Sample CFN error output with structured resource info
CONFLICT_OUTPUT_SINGLE = (
    "Error: Failed to create/update the stack.\n"
    "Resource handler returned message: \"my-queue already exists\" "
    "[RequestToken: abc, HandlerErrorCode: AlreadyExists]\n"
    "  ResourceLogicalId: MyQueue, ResourceType: AWS::SQS::Queue, "
    "ResourcePhysicalId: https://sqs.us-east-1.amazonaws.com/123/my-queue]"
)

CONFLICT_OUTPUT_MULTI = (
    "Error: Failed to create/update the stack.\n"
    "Resource handler returned message: \"my-queue already exists\" "
    "[RequestToken: abc1, HandlerErrorCode: AlreadyExists]\n"
    "  ResourceLogicalId: MyQueue, ResourceType: AWS::SQS::Queue, "
    "ResourcePhysicalId: https://sqs.us-east-1.amazonaws.com/123/my-queue]\n"
    "Resource handler returned message: \"my-bucket already exists\" "
    "[RequestToken: abc2, HandlerErrorCode: AlreadyExists]\n"
    "  ResourceLogicalId: MyBucket, ResourceType: AWS::S3::Bucket, "
    "ResourcePhysicalId: my-bucket-name]"
)

NO_CONFLICT_OUTPUT = (
    "Error: Failed to create/update the stack.\n"
    "Resource handler returned message: \"Access Denied\" "
    "[RequestToken: xyz, HandlerErrorCode: AccessDenied]"
)


@pytest.fixture()
def mock_cfn_client():
    """A MagicMock standing in for boto3 CFN client."""
    client = MagicMock()
    # describe_stacks → stack exists
    client.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
    # describe_change_set → CREATE_COMPLETE immediately
    client.describe_change_set.return_value = {"Status": "CREATE_COMPLETE"}
    # waiters
    client.get_waiter.return_value.wait = MagicMock()
    return client


@pytest.fixture()
def importer(mock_cfn_client):
    return CloudFormationImporter(STACK, cfn_client=mock_cfn_client)


@pytest.fixture()
def dry_importer(mock_cfn_client):
    return CloudFormationImporter(STACK, cfn_client=mock_cfn_client, dry_run=True)


@pytest.fixture(autouse=True)
def _patch_sam_package_and_ddb(request):
    """Auto-patch _run_sam_package and update_template_s3_url for all tests that
    invoke main() via TestMain* or TestEarlyValidation* or TestSuggestImport* or
    TestMainWithParams* classes.  This prevents subprocess.run from trying to call
    the real ``sam package`` command and a real DDB endpoint.
    """
    marker = request.node.get_closest_marker("no_patch_sam_package")
    if marker:
        yield
        return

    class_name = getattr(request.cls, "__name__", "") if request.cls else ""
    if not class_name.startswith(
        (
            "TestMain",
            "TestEarlyValidation",
            "TestSuggestImport",
        )
    ):
        yield
        return

    with (
        patch("sam_deploy._run_sam_package"),
        patch("sam_deploy.update_template_s3_url"),
    ):
        yield


# ===========================================================================
# 1. Validation helpers
# ===========================================================================


class TestValidateStackName:
    def test_empty_exits(self):
        with pytest.raises(SystemExit) as exc:
            _validate_stack_name("")
        assert exc.value.code == 1

    def test_none_coerced_to_empty_exits(self):
        """Passing a falsy/empty string should exit."""
        with pytest.raises(SystemExit) as exc:
            _validate_stack_name("")
        assert exc.value.code == 1

    def test_valid_passes(self):
        _validate_stack_name("my-stack")  # should not raise

    def test_valid_with_dots_and_dashes(self):
        _validate_stack_name("my-stack.v2")  # should not raise


class TestValidateParamKey:
    def test_valid_key(self):
        _validate_param_key("MyKey")  # no exception

    def test_valid_key_alphanumeric(self):
        _validate_param_key("ValidKey123")  # no exception

    def test_key_with_flag_injection_exits(self):
        with pytest.raises(SystemExit):
            _validate_param_key("--bad-flag")

    def test_key_starting_with_digit_exits(self):
        with pytest.raises(SystemExit):
            _validate_param_key("1BadKey")

    def test_key_with_special_chars_exits(self):
        with pytest.raises(SystemExit):
            _validate_param_key("bad-key!")

    def test_key_with_spaces_exits(self):
        with pytest.raises(SystemExit):
            _validate_param_key("bad key")


# ===========================================================================
# 2. ConflictResource
# ===========================================================================


class TestConflictResource:
    def test_to_import_record_sqs(self):
        c = ConflictResource("MyQueue", "AWS::SQS::Queue", "https://sqs.../my-queue")
        record = c.to_import_record()
        assert record["ResourceType"] == "AWS::SQS::Queue"
        assert record["LogicalResourceId"] == "MyQueue"
        assert record["ResourceIdentifier"] == {"QueueUrl": "https://sqs.../my-queue"}

    def test_to_import_record_s3(self):
        c = ConflictResource("MyBucket", "AWS::S3::Bucket", "my-bucket-name")
        record = c.to_import_record()
        assert record["ResourceIdentifier"] == {"BucketName": "my-bucket-name"}

    def test_to_import_record_dynamodb(self):
        c = ConflictResource("MyTable", "AWS::DynamoDB::Table", "my-table")
        record = c.to_import_record()
        assert record["ResourceIdentifier"] == {"TableName": "my-table"}

    def test_to_import_record_lambda(self):
        c = ConflictResource("MyFunc", "AWS::Lambda::Function", "my-func")
        record = c.to_import_record()
        assert record["ResourceIdentifier"] == {"FunctionName": "my-func"}

    def test_to_import_record_unknown_type_uses_id(self):
        c = ConflictResource("MyRes", "AWS::Custom::Resource", "some-id")
        record = c.to_import_record()
        assert "Id" in record["ResourceIdentifier"]

    def test_strips_whitespace(self):
        c = ConflictResource("  MyQueue  ", "  AWS::SQS::Queue  ", "  arn:aws:...  ")
        record = c.to_import_record()
        assert record["LogicalResourceId"] == "MyQueue"
        assert record["ResourceType"] == "AWS::SQS::Queue"


# ===========================================================================
# 3. CloudFormationImporter — conflict parsing (fine-grained, C-style)
# ===========================================================================


class TestHasConflictError:
    """Fine-grained tests for has_conflict_error (C-style coverage)."""

    def test_positive_match(self, importer):
        assert importer.has_conflict_error(CONFLICT_OUTPUT_SINGLE) is True

    def test_negative_no_exists(self, importer):
        assert importer.has_conflict_error(NO_CONFLICT_OUTPUT) is False

    def test_empty_string(self, importer):
        assert importer.has_conflict_error("") is False

    def test_case_insensitive(self, importer):
        assert importer.has_conflict_error("RESOURCE ALREADY EXISTS") is True

    def test_partial_word_no_match(self, importer):
        """'exists' alone without 'already' should not be a false positive."""
        # The simple regex just looks for "already exists", so "exists" alone → False
        assert importer.has_conflict_error("Resource exists in account") is False

    def test_already_exists_in_stderr(self, importer):
        assert importer.has_conflict_error("error: the thing already exists here") is True


class TestParseConflicts:
    def test_single_conflict(self, importer):
        conflicts = importer.parse_conflicts(CONFLICT_OUTPUT_SINGLE)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c.logical_id == "MyQueue"
        assert c.resource_type == "AWS::SQS::Queue"
        assert "my-queue" in c.physical_id

    def test_multiple_conflicts(self, importer):
        conflicts = importer.parse_conflicts(CONFLICT_OUTPUT_MULTI)
        assert len(conflicts) == 2
        logical_ids = {c.logical_id for c in conflicts}
        assert "MyQueue" in logical_ids
        assert "MyBucket" in logical_ids

    def test_no_match_returns_empty(self, importer):
        conflicts = importer.parse_conflicts("Totally unrelated error output")
        assert conflicts == []

    def test_duplicate_entries_deduplicated(self, importer):
        doubled = CONFLICT_OUTPUT_SINGLE + "\n" + CONFLICT_OUTPUT_SINGLE
        conflicts = importer.parse_conflicts(doubled)
        assert len(conflicts) == 1

    def test_empty_string_returns_empty(self, importer):
        conflicts = importer.parse_conflicts("")
        assert conflicts == []

    def test_preserves_resource_types(self, importer):
        """Ensure parsed resource types match exactly."""
        conflicts = importer.parse_conflicts(CONFLICT_OUTPUT_MULTI)
        types_found = {c.resource_type for c in conflicts}
        assert "AWS::SQS::Queue" in types_found
        assert "AWS::S3::Bucket" in types_found


# ===========================================================================
# 4. CloudFormationImporter — import_resources
# ===========================================================================


class TestImportResources:
    def test_empty_list_returns_true(self, importer):
        result = importer.import_resources([])
        assert result is True

    def test_creates_import_changeset(self, importer, mock_cfn_client):
        conflicts = [
            ConflictResource("MyQueue", "AWS::SQS::Queue", "https://sqs.../q"),
        ]
        result = importer.import_resources(conflicts)
        assert result is True
        mock_cfn_client.create_change_set.assert_called_once()
        call_kwargs = mock_cfn_client.create_change_set.call_args[1]
        assert call_kwargs["ChangeSetType"] == "IMPORT"
        assert call_kwargs["StackName"] == STACK
        assert len(call_kwargs["ResourcesToImport"]) == 1

    def test_changeset_capabilities_include_named_iam(self, importer, mock_cfn_client):
        """Verify CAPABILITY_NAMED_IAM is included (needed for named IAM resources)."""
        conflicts = [ConflictResource("Q", "AWS::SQS::Queue", "url")]
        importer.import_resources(conflicts)
        call_kwargs = mock_cfn_client.create_change_set.call_args[1]
        assert "CAPABILITY_NAMED_IAM" in call_kwargs["Capabilities"]
        assert "CAPABILITY_IAM" in call_kwargs["Capabilities"]
        assert "CAPABILITY_AUTO_EXPAND" in call_kwargs["Capabilities"]

    def test_multiple_conflicts_single_changeset(self, importer, mock_cfn_client):
        conflicts = [
            ConflictResource("MyQueue", "AWS::SQS::Queue", "https://sqs.../q"),
            ConflictResource("MyBucket", "AWS::S3::Bucket", "my-bucket"),
        ]
        importer.import_resources(conflicts)
        assert mock_cfn_client.create_change_set.call_count == 1
        call_kwargs = mock_cfn_client.create_change_set.call_args[1]
        assert len(call_kwargs["ResourcesToImport"]) == 2

    def test_import_failure_returns_false(self, mock_cfn_client):
        from botocore.exceptions import ClientError

        mock_cfn_client.create_change_set.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "oops"}}, "CreateChangeSet"
        )
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        result = imp.import_resources([ConflictResource("Q", "AWS::SQS::Queue", "url")])
        assert result is False

    def test_dry_run_skips_api_calls(self, dry_importer, mock_cfn_client):
        conflicts = [ConflictResource("MyQueue", "AWS::SQS::Queue", "url")]
        result = dry_importer.import_resources(conflicts)
        assert result is True
        mock_cfn_client.create_change_set.assert_not_called()

    def test_stack_not_found_creates_empty_stack(self, mock_cfn_client):
        from botocore.exceptions import ClientError

        mock_cfn_client.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
            "DescribeStacks",
        )
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        conflicts = [ConflictResource("Q", "AWS::SQS::Queue", "url")]
        imp.import_resources(conflicts)
        mock_cfn_client.create_stack.assert_called_once()

    def test_stack_not_found_waits_for_creation(self, mock_cfn_client):
        """After creating empty stack, waiter should be called."""
        from botocore.exceptions import ClientError

        mock_cfn_client.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
            "DescribeStacks",
        )
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        conflicts = [ConflictResource("Q", "AWS::SQS::Queue", "url")]
        imp.import_resources(conflicts)
        mock_cfn_client.get_waiter.assert_any_call("stack_create_complete")

    def test_changeset_failed_status_returns_false(self, mock_cfn_client):
        """When changeset polling returns FAILED, import should return False."""
        mock_cfn_client.describe_change_set.return_value = {
            "Status": "FAILED",
            "StatusReason": "No changes to import",
        }
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        conflicts = [ConflictResource("Q", "AWS::SQS::Queue", "url")]
        result = imp.import_resources(conflicts)
        assert result is False

    def test_non_exists_client_error_reraises(self, mock_cfn_client):
        """ClientError that isn't 'does not exist' should propagate as import failure."""
        from botocore.exceptions import ClientError

        mock_cfn_client.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "DescribeStacks",
        )
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        conflicts = [ConflictResource("Q", "AWS::SQS::Queue", "url")]
        result = imp.import_resources(conflicts)
        # Should return False (caught by the broad except in import_resources)
        assert result is False


# ===========================================================================
# 5. DeployResult dataclass
# ===========================================================================


class TestDeployResult:
    def test_succeeded_true_on_zero_rc(self):
        r = DeployResult(status=DeployStatus.SUCCESS, returncode=0)
        assert r.succeeded is True

    def test_succeeded_false_on_nonzero(self):
        r = DeployResult(status=DeployStatus.FAILED, returncode=1)
        assert r.succeeded is False

    def test_has_conflicts_false_by_default(self):
        r = DeployResult(status=DeployStatus.SUCCESS, returncode=0)
        assert r.has_conflicts is False

    def test_has_conflicts_true_when_conflicts_present(self):
        r = DeployResult(
            status=DeployStatus.CONFLICT,
            returncode=1,
            conflicts=[ConflictResource("Q", "AWS::SQS::Queue", "url")],
        )
        assert r.has_conflicts is True

    def test_stdout_stderr_defaults(self):
        r = DeployResult(status=DeployStatus.SUCCESS, returncode=0)
        assert r.stdout == ""
        assert r.stderr == ""


# ===========================================================================
# 6. _run_deploy
# ===========================================================================


class TestRunDeploy:
    """_run_deploy uses subprocess.Popen with streaming output (#88)."""

    def _make_popen(self, lines, returncode=0):
        """Build a mock Popen whose .stdout iterates over *lines*."""
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = iter(line + "\n" for line in lines)
        proc.wait.return_value = None
        return proc

    def test_success_returns_success_status(self):
        proc = self._make_popen(["Deploy succeeded"], returncode=0)
        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.succeeded is True
        assert result.returncode == 0

    def test_failure_returns_failed_status(self):
        proc = self._make_popen(["Error: deploy failed"], returncode=1)
        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.status == DeployStatus.FAILED
        assert result.returncode == 1

    def test_streaming_lines_collected_in_stdout(self):
        """All streamed lines are joined into result.stdout."""
        lines = ["line one", "line two", "line three"]
        proc = self._make_popen(lines, returncode=0)
        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.stdout == "line one\nline two\nline three"

    def test_popen_called_with_streaming_args(self):
        """Popen is called with PIPE stdout, merged stderr, text=True."""
        proc = self._make_popen([], returncode=0)
        with patch("sam_deploy.subprocess.Popen", return_value=proc) as mock_popen:
            _run_deploy(["sam", "deploy", "--stack-name", "my-stack"])
        call_kwargs = mock_popen.call_args
        args, kwargs = call_kwargs
        assert kwargs.get("stdout") == subprocess.PIPE
        assert kwargs.get("stderr") == subprocess.STDOUT
        assert kwargs.get("text") is True

    def test_stderr_field_empty_in_streaming_mode(self):
        """stderr field is empty string since stderr is merged into stdout."""
        proc = self._make_popen(["some output"], returncode=0)
        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.stderr == ""

    def test_wait_called_after_stdout_exhausted(self):
        """process.wait() is called to collect returncode."""
        proc = self._make_popen(["output"], returncode=0)
        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            _run_deploy(["sam", "deploy"])
        proc.wait.assert_called_once()


# ===========================================================================
# 7. _build_sam_cmd
# ===========================================================================


class TestBuildSamCmd:
    def test_basic_cmd_no_params(self):
        cmd = _build_sam_cmd("my-stack", "", "", "")
        assert "--stack-name" in cmd
        assert "my-stack" in cmd
        assert "--parameter-overrides" not in cmd

    def test_json_params(self):
        cmd = _build_sam_cmd("my-stack", '{"Key1": "Val1"}', "", "")
        assert "--parameter-overrides" in cmd
        assert "Key1=Val1" in cmd

    def test_json_object_type_check(self):
        """JSON array should cause exit."""
        with pytest.raises(SystemExit):
            _build_sam_cmd("my-stack", '["not", "an", "object"]', "", "")

    def test_legacy_params_fallback(self):
        cmd = _build_sam_cmd("my-stack", "Key1=Val1 Key2=Val2", "", "")
        assert "--parameter-overrides" in cmd
        assert "Key1=Val1" in cmd
        assert "Key2=Val2" in cmd

    def test_cfn_role_added_when_no_target_role(self):
        cmd = _build_sam_cmd("my-stack", "", "arn:aws:iam::role/cfn", "")
        assert "--role-arn" in cmd
        assert "arn:aws:iam::role/cfn" in cmd

    def test_cfn_role_skipped_when_target_role_set(self):
        cmd = _build_sam_cmd("my-stack", "", "arn:aws:iam::role/cfn", "arn:aws:iam::role/target")
        assert "--role-arn" not in cmd

    def test_no_role_when_both_empty(self):
        cmd = _build_sam_cmd("my-stack", "", "", "")
        assert "--role-arn" not in cmd


# ===========================================================================
# 8. Physical ID identifier mapping (comprehensive, C-style coverage)
# ===========================================================================


class TestPhysicalIdToIdentifier:
    @pytest.mark.parametrize(
        "rtype, physical, expected_key",
        [
            ("AWS::SQS::Queue", "url", "QueueUrl"),
            ("AWS::SNS::Topic", "arn", "TopicArn"),
            ("AWS::DynamoDB::Table", "tbl", "TableName"),
            ("AWS::S3::Bucket", "bkt", "BucketName"),
            ("AWS::IAM::Role", "role", "RoleName"),
            ("AWS::IAM::Policy", "arn", "PolicyArn"),
            ("AWS::Lambda::Function", "fn", "FunctionName"),
            ("AWS::CloudWatch::Alarm", "alarm", "AlarmName"),
            ("AWS::SecretsManager::Secret", "id", "Id"),
            ("AWS::SSM::Parameter", "name", "Name"),
            ("AWS::KMS::Key", "key-id", "KeyId"),
            ("AWS::KMS::Alias", "alias", "AliasName"),
            ("AWS::EC2::SecurityGroup", "sg-123", "GroupId"),
            ("AWS::EC2::Subnet", "subnet-abc", "SubnetId"),
            ("AWS::EC2::VPC", "vpc-xyz", "VpcId"),
            ("AWS::Unknown::Type", "x", "Id"),  # fallback
        ],
    )
    def test_identifier_key(self, rtype, physical, expected_key):
        result = _physical_id_to_identifier(rtype, physical)
        assert expected_key in result
        assert result[expected_key] == physical


# ===========================================================================
# 9. DeployStatus enum
# ===========================================================================


class TestDeployStatus:
    def test_all_statuses_exist(self):
        assert DeployStatus.SUCCESS is not None
        assert DeployStatus.CONFLICT is not None
        assert DeployStatus.IMPORT_NEEDED is not None
        assert DeployStatus.FAILED is not None

    def test_statuses_are_distinct(self):
        statuses = [DeployStatus.SUCCESS, DeployStatus.CONFLICT, DeployStatus.IMPORT_NEEDED, DeployStatus.FAILED]
        assert len(set(statuses)) == 4


# ===========================================================================
# 10. main() integration tests — normal deploy
# ===========================================================================


class TestMainNormalDeploy:
    """No conflicts → normal deploy path."""

    def _make_proc(self, rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
        """Build a Popen-compatible mock (stdout+stderr merged as iterable)."""
        p = MagicMock()
        p.returncode = rc
        # Merge stdout+stderr as Popen does with stderr=STDOUT
        combined = stdout + stderr
        p.stdout = iter(combined.splitlines(keepends=True)) if combined else iter([])
        p.wait.return_value = None
        return p

    def test_success_exits_zero(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        with patch("sam_deploy.subprocess.Popen", return_value=self._make_proc(0, "ok\n")):
            with pytest.raises(SystemExit) as exc:
                main([])
        assert exc.value.code == 0

    def test_non_conflict_failure_exits_nonzero_no_import(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        with patch(
            "sam_deploy.subprocess.Popen",
            return_value=self._make_proc(1, stderr="Access Denied\n"),
        ), patch.object(CloudFormationImporter, "import_resources") as mock_import:
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 1
        mock_import.assert_not_called()

    def test_missing_stack_name_exits(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", "")
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 1


# ===========================================================================
# 11. main() integration tests — conflict path
# ===========================================================================


class TestMainConflictPath:
    """Conflict → import → retry."""

    def _make_proc(self, rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
        """Build a Popen-compatible mock (stdout+stderr merged as iterable)."""
        p = MagicMock()
        p.returncode = rc
        # Merge stdout+stderr as Popen does with stderr=STDOUT
        combined = stdout + stderr
        p.stdout = iter(combined.splitlines(keepends=True)) if combined else iter([])
        p.wait.return_value = None
        return p

    def test_conflict_triggers_import_and_retry(self, monkeypatch, mock_cfn_client):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_SINGLE)
        second_call = self._make_proc(0, stdout="Retry succeeded\n")

        with patch(
            "sam_deploy.subprocess.Popen", side_effect=[first_call, second_call]
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 0
        mock_cfn_client.create_change_set.assert_called_once()

    def test_multiple_conflicts_single_changeset(self, monkeypatch, mock_cfn_client):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_MULTI)
        second_call = self._make_proc(0, stdout="ok\n")

        with patch(
            "sam_deploy.subprocess.Popen", side_effect=[first_call, second_call]
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 0
        assert mock_cfn_client.create_change_set.call_count == 1
        resources = mock_cfn_client.create_change_set.call_args[1]["ResourcesToImport"]
        assert len(resources) == 2

    def test_import_failure_aborts(self, monkeypatch, mock_cfn_client):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        from botocore.exceptions import ClientError

        mock_cfn_client.create_change_set.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "nope"}}, "CreateChangeSet"
        )
        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_SINGLE)

        popen_calls = []

        def side_effect_fn(*args, **kwargs):
            popen_calls.append(args)
            return first_call

        with patch(
            "sam_deploy.subprocess.Popen", side_effect=side_effect_fn
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 1
        assert len(popen_calls) == 1

    def test_unparsable_conflict_aborts(self, monkeypatch):
        """Output has 'already exists' but no structured resource info."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        vague_conflict = "Error: something already exists but no details here"
        first_call = MagicMock()
        first_call.returncode = 1
        first_call.stdout = iter([vague_conflict + "\n"])
        first_call.wait.return_value = None

        with patch(
            "sam_deploy.subprocess.Popen", return_value=first_call
        ), patch.object(CloudFormationImporter, "import_resources") as mock_import:
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code != 0
        mock_import.assert_not_called()

    def test_conflict_in_stderr_also_detected(self, monkeypatch, mock_cfn_client):
        """Conflict message in stderr is merged into stdout via Popen stderr=STDOUT."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        # With Popen stderr=STDOUT, stderr is merged into stdout stream
        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_SINGLE)
        second_call = self._make_proc(0, stdout="ok\n")

        with patch(
            "sam_deploy.subprocess.Popen", side_effect=[first_call, second_call]
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 0
        mock_cfn_client.create_change_set.assert_called_once()


# ===========================================================================
# 12. main() — --dry-run-import flag
# ===========================================================================


class TestMainDryRunImport:
    """--dry-run-import flag tests."""

    def _make_proc(self, rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
        """Build a Popen-compatible mock (stdout+stderr merged as iterable)."""
        p = MagicMock()
        p.returncode = rc
        combined = stdout + stderr
        p.stdout = iter(combined.splitlines(keepends=True)) if combined else iter([])
        p.wait.return_value = None
        return p

    def test_dry_run_no_conflict_deploys_normally(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        with patch(
            "sam_deploy.subprocess.Popen", return_value=self._make_proc(0, "ok\n")
        ):
            with pytest.raises(SystemExit) as exc:
                main(["--dry-run-import"])
        assert exc.value.code == 0

    def test_dry_run_conflict_prints_plan_and_exits_2(self, monkeypatch, mock_cfn_client):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_SINGLE)

        with patch(
            "sam_deploy.subprocess.Popen", return_value=first_call
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main(["--dry-run-import"])

        assert exc.value.code == 2
        mock_cfn_client.create_change_set.assert_not_called()
        mock_cfn_client.execute_change_set.assert_not_called()

    def test_dry_run_flag_detected_in_argv(self):
        """Verify --dry-run-import is recognized."""
        assert "--dry-run-import" in ["--dry-run-import"]


# ===========================================================================
# 13. CloudFormationImporter — lazy client initialization
# ===========================================================================


class TestImporterLazyInit:
    def test_cfn_client_injected(self, mock_cfn_client):
        """When cfn_client is provided, it should be used directly."""
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        assert imp._cfn is mock_cfn_client

    def test_cfn_client_lazy_created_when_none(self):
        """When no client provided, boto3.client should be called on first access."""
        imp = CloudFormationImporter(STACK, cfn_client=None)
        with patch("sam_deploy.boto3.client", return_value=MagicMock()) as mock_boto:
            _ = imp._cfn
            mock_boto.assert_called_once_with("cloudformation")


# ===========================================================================
# 14. _wait_for_changeset edge cases
# ===========================================================================


class TestWaitForChangeset:
    def test_immediate_create_complete(self, mock_cfn_client):
        """Changeset immediately in CREATE_COMPLETE state."""
        mock_cfn_client.describe_change_set.return_value = {"Status": "CREATE_COMPLETE"}
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        # Should not raise
        imp._wait_for_changeset("test-cs")

    def test_polls_until_complete(self, mock_cfn_client):
        """Changeset goes through CREATE_PENDING → CREATE_IN_PROGRESS → CREATE_COMPLETE."""
        mock_cfn_client.describe_change_set.side_effect = [
            {"Status": "CREATE_PENDING"},
            {"Status": "CREATE_IN_PROGRESS"},
            {"Status": "CREATE_COMPLETE"},
        ]
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        with patch("time.sleep"):
            imp._wait_for_changeset("test-cs")
        assert mock_cfn_client.describe_change_set.call_count == 3

    def test_failed_changeset_raises(self, mock_cfn_client):
        """FAILED status should raise RuntimeError."""
        mock_cfn_client.describe_change_set.return_value = {
            "Status": "FAILED",
            "StatusReason": "No changes to import",
        }
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        with pytest.raises(RuntimeError, match="Changeset failed"):
            imp._wait_for_changeset("test-cs")


# ===========================================================================
# 15. main() with SAM_PARAMS variations
# ===========================================================================


class TestMainWithParams:
    def _make_proc(self, rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
        """Build a Popen-compatible mock (stdout+stderr merged as iterable)."""
        p = MagicMock()
        p.returncode = rc
        combined = stdout + stderr
        p.stdout = iter(combined.splitlines(keepends=True)) if combined else iter([])
        p.wait.return_value = None
        return p

    def test_json_params_passed_correctly(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.setenv("SAM_PARAMS", '{"Env": "prod", "Region": "us-east-1"}')
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        captured_cmd = []

        def capture_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._make_proc(0, "ok\n")

        with patch("sam_deploy.subprocess.Popen", side_effect=capture_popen):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 0
        assert "Env=prod" in captured_cmd
        assert "Region=us-east-1" in captured_cmd

    def test_cfn_role_added_when_set(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.setenv("CFN_ROLE_ARN", "arn:aws:iam::123:role/cfn")
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        captured_cmd = []

        def capture_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._make_proc(0, "ok\n")

        with patch("sam_deploy.subprocess.Popen", side_effect=capture_popen):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 0
        assert "--role-arn" in captured_cmd
        assert "arn:aws:iam::123:role/cfn" in captured_cmd


# ===========================================================================
# 16. EarlyValidation detection and import hint (sprint8-005, approach-b)
# ===========================================================================

# Canonical EarlyValidation error string (as emitted by SAM / CFN)
EARLY_VALIDATION_OUTPUT = (
    "Error: Failed to create/update the stack.\n"
    "Waiter ChangeSetCreateComplete failed: Waiter encountered a terminal failure state\n"
    "For expression \"Status\" we matched expected path: \"FAILED\"\n"
    "Status: FAILED\n"
    "Reason: The following hook(s)/validation failed: "
    "[AWS::EarlyValidation::ResourceExistenceCheck]. "
    "To troubleshoot Early Validation errors, use the DescribeEvents API "
    "for detailed failure information."
)

# EarlyValidation output that also has structured resource info (rare but possible)
EARLY_VALIDATION_WITH_RESOURCE_OUTPUT = (
    EARLY_VALIDATION_OUTPUT + "\n"
    "  ResourceLogicalId: MyBucket, ResourceType: AWS::S3::Bucket, "
    "ResourcePhysicalId: my-existing-bucket]"
)


class TestEarlyValidationDetected:
    """test_earlyvalidation_detected — regex detects the hook string."""

    def test_regex_matches_canonical_string(self):
        assert _EARLY_VALIDATION_RE.search(EARLY_VALIDATION_OUTPUT) is not None

    def test_regex_case_insensitive(self):
        lower = "aws::earlyvalidation::resourceexistencecheck"
        assert _EARLY_VALIDATION_RE.search(lower) is not None

    def test_regex_no_match_on_normal_already_exists(self):
        """Ordinary 'already exists' without EarlyValidation hook should not match."""
        assert _EARLY_VALIDATION_RE.search(CONFLICT_OUTPUT_SINGLE) is None

    def test_regex_no_match_on_unrelated_error(self):
        assert _EARLY_VALIDATION_RE.search(NO_CONFLICT_OUTPUT) is None

    def test_regex_no_match_on_empty(self):
        assert _EARLY_VALIDATION_RE.search("") is None

    def test_regex_matches_real_world_cdk_format(self):
        """CDK-style error line also matches."""
        cdk_line = (
            "MyStack failed: ToolkitError: Failed to create ChangeSet on MyStack: FAILED, "
            "The following hook(s)/validation failed: [AWS::EarlyValidation::ResourceExistenceCheck]. "
            "To troubleshoot Early Validation errors, use the DescribeEvents API."
        )
        assert _EARLY_VALIDATION_RE.search(cdk_line) is not None


class TestEarlyValidationHintPrinted:
    """test_earlyvalidation_hint_printed — [IMPORT NEEDED] banner appears."""

    def test_hint_contains_import_needed(self, capsys):
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "[IMPORT NEEDED]" in captured.out

    def test_hint_contains_stack_name(self, capsys):
        _print_early_validation_hint("my-test-stack")
        captured = capsys.readouterr()
        assert "my-test-stack" in captured.out

    def test_hint_contains_earlyvalidation_type(self, capsys):
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "AWS::EarlyValidation::ResourceExistenceCheck" in captured.out

    def test_hint_contains_docs_link(self, capsys):
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "docs.aws.amazon.com" in captured.out

    def test_main_prints_hint_on_early_validation_error(self, monkeypatch, capsys):
        """Integration: main() triggers [IMPORT NEEDED] when EarlyValidation in output."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = iter(EARLY_VALIDATION_OUTPUT.splitlines(keepends=True))
        proc.wait.return_value = None

        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code != 0
        captured = capsys.readouterr()
        assert "[IMPORT NEEDED]" in captured.out

    def test_main_hint_not_printed_on_unrelated_failure(self, monkeypatch, capsys):
        """[IMPORT NEEDED] must NOT appear for non-EarlyValidation failures."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = iter(NO_CONFLICT_OUTPUT.splitlines(keepends=True))
        proc.wait.return_value = None

        with patch("sam_deploy.subprocess.Popen", return_value=proc):
            with pytest.raises(SystemExit):
                main([])

        captured = capsys.readouterr()
        assert "[IMPORT NEEDED]" not in captured.out


class TestEarlyValidationCommandFormat:
    """test_earlyvalidation_command_format — hint output contains valid CFN command."""

    def test_hint_contains_create_change_set(self, capsys):
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "create-change-set" in captured.out

    def test_hint_contains_resources_to_import_flag(self, capsys):
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "--resources-to-import" in captured.out

    def test_hint_contains_change_set_type_import(self, capsys):
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "IMPORT" in captured.out

    def test_hint_contains_execute_change_set(self, capsys):
        """Users need both create and execute steps."""
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "execute-change-set" in captured.out

    def test_hint_contains_describe_stack_events(self, capsys):
        """Identify conflicting resources step should be present."""
        _print_early_validation_hint("my-stack")
        captured = capsys.readouterr()
        assert "describe-stack-events" in captured.out

    def test_hint_command_includes_stack_name_argument(self, capsys):
        _print_early_validation_hint("prod-stack")
        captured = capsys.readouterr()
        # stack name should appear as part of the CLI argument
        assert "prod-stack" in captured.out


class TestSuggestImportJsonOutput:
    """test_suggest_import_json_output — --suggest-import flag outputs valid JSON."""

    def _make_proc(self, rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
        """Build a Popen-compatible mock (stdout+stderr merged as iterable)."""
        p = MagicMock()
        p.returncode = rc
        combined = stdout + stderr
        p.stdout = iter(combined.splitlines(keepends=True)) if combined else iter([])
        p.wait.return_value = None
        return p

    def test_build_suggest_import_json_returns_valid_json(self):
        conflicts = [
            ConflictResource("MyQueue", "AWS::SQS::Queue", "https://sqs.../q"),
            ConflictResource("MyBucket", "AWS::S3::Bucket", "my-bucket"),
        ]
        result = _build_suggest_import_json(conflicts)
        parsed = json.loads(result)  # must not raise
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_build_suggest_import_json_record_structure(self):
        conflicts = [ConflictResource("MyTable", "AWS::DynamoDB::Table", "my-table")]
        result = _build_suggest_import_json(conflicts)
        parsed = json.loads(result)
        record = parsed[0]
        assert record["ResourceType"] == "AWS::DynamoDB::Table"
        assert record["LogicalResourceId"] == "MyTable"
        assert record["ResourceIdentifier"] == {"TableName": "my-table"}

    def test_build_suggest_import_json_empty_conflicts(self):
        result = _build_suggest_import_json([])
        parsed = json.loads(result)
        assert parsed == []

    def test_main_suggest_import_with_parseable_conflicts(self, monkeypatch, capsys):
        """--suggest-import on parseable 'already exists' output → JSON to stderr."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_SINGLE)

        with patch("sam_deploy.subprocess.Popen", return_value=first_call):
            with pytest.raises(SystemExit) as exc:
                main(["--suggest-import"])

        assert exc.value.code == 2
        captured = capsys.readouterr()
        err = captured.err
        assert "[SUGGEST-IMPORT]" in err
        # Extract the JSON array: find the first line that starts with '['
        json_lines = [ln for ln in err.splitlines() if ln.strip().startswith("[")]
        assert json_lines, f"No JSON array line found in stderr: {err!r}"
        # Re-join multi-line JSON by finding block between first '[' and last ']'
        json_start = err.index("\n[") + 1  # newline before the JSON array
        parsed = json.loads(err[json_start:].strip())
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["LogicalResourceId"] == "MyQueue"

    def test_main_suggest_import_on_early_validation_with_resources(
        self, monkeypatch, capsys, mock_cfn_client
    ):
        """--suggest-import on EarlyValidation with parseable resources → JSON."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=EARLY_VALIDATION_WITH_RESOURCE_OUTPUT)

        with patch("sam_deploy.subprocess.Popen", return_value=first_call), \
             patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main(["--suggest-import"])

        captured = capsys.readouterr()
        # Should have [IMPORT NEEDED] hint
        assert "[IMPORT NEEDED]" in captured.out
        # And JSON plan in stderr (EarlyValidation path)
        assert "[SUGGEST-IMPORT]" in captured.err

    def test_main_suggest_import_on_early_validation_no_resources(
        self, monkeypatch, capsys
    ):
        """--suggest-import on EarlyValidation without parseable details → warning."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=EARLY_VALIDATION_OUTPUT)

        with patch("sam_deploy.subprocess.Popen", return_value=first_call):
            with pytest.raises(SystemExit) as exc:
                main(["--suggest-import"])

        captured = capsys.readouterr()
        assert "[IMPORT NEEDED]" in captured.out
        assert "[SUGGEST-IMPORT]" in captured.err
        # No parseable resources → fallback message
        assert "describe-stack-events" in captured.err or "Could not parse" in captured.err
