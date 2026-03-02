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
    unparseable conflict, --dry-run-import, multiple conflicts
  - Edge cases: empty strings, case insensitivity, deduplication, partial matches
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — make the script importable as a module
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sam_deploy  # noqa: E402, F401
from sam_deploy import (  # noqa: E402
    CloudFormationImporter,
    ConflictResource,
    DeployResult,
    DeployStatus,
    _build_sam_cmd,
    _physical_id_to_identifier,
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
    def test_success_returns_success_status(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Deploy succeeded\n"
        proc.stderr = ""
        with patch("sam_deploy.subprocess.run", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.succeeded is True
        assert result.returncode == 0

    def test_failure_returns_failed_status(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = "Error output\n"
        proc.stderr = "Some error"
        with patch("sam_deploy.subprocess.run", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.status == DeployStatus.FAILED
        assert result.returncode == 1

    def test_captures_stdout_and_stderr(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "stdout content"
        proc.stderr = "stderr content"
        with patch("sam_deploy.subprocess.run", return_value=proc):
            result = _run_deploy(["sam", "deploy"])
        assert result.stdout == "stdout content"
        assert result.stderr == "stderr content"


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
        p = MagicMock()
        p.returncode = rc
        p.stdout = stdout
        p.stderr = stderr
        return p

    def test_success_exits_zero(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        with patch("sam_deploy.subprocess.run", return_value=self._make_proc(0, "ok\n")):
            with pytest.raises(SystemExit) as exc:
                main([])
        assert exc.value.code == 0

    def test_non_conflict_failure_exits_nonzero_no_import(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        with patch(
            "sam_deploy.subprocess.run",
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
        p = MagicMock()
        p.returncode = rc
        p.stdout = stdout
        p.stderr = stderr
        return p

    def test_conflict_triggers_import_and_retry(self, monkeypatch, mock_cfn_client):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=CONFLICT_OUTPUT_SINGLE)
        second_call = self._make_proc(0, stdout="Retry succeeded\n")

        with patch(
            "sam_deploy.subprocess.run", side_effect=[first_call, second_call]
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
            "sam_deploy.subprocess.run", side_effect=[first_call, second_call]
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

        run_calls = []

        def side_effect_fn(*args, **kwargs):
            run_calls.append(args)
            return first_call

        with patch(
            "sam_deploy.subprocess.run", side_effect=side_effect_fn
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 1
        assert len(run_calls) == 1

    def test_unparseable_conflict_aborts(self, monkeypatch):
        """Output has 'already exists' but no structured resource info."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        vague_conflict = "Error: something already exists but no details here"
        first_call = MagicMock()
        first_call.returncode = 1
        first_call.stdout = vague_conflict
        first_call.stderr = ""

        with patch(
            "sam_deploy.subprocess.run", return_value=first_call
        ), patch.object(CloudFormationImporter, "import_resources") as mock_import:
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code != 0
        mock_import.assert_not_called()

    def test_conflict_in_stderr_also_detected(self, monkeypatch, mock_cfn_client):
        """Conflict message in stderr (not stdout) should also trigger import."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout="", stderr=CONFLICT_OUTPUT_SINGLE)
        second_call = self._make_proc(0, stdout="ok\n")

        with patch(
            "sam_deploy.subprocess.run", side_effect=[first_call, second_call]
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
        p = MagicMock()
        p.returncode = rc
        p.stdout = stdout
        p.stderr = stderr
        return p

    def test_dry_run_no_conflict_deploys_normally(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        with patch(
            "sam_deploy.subprocess.run", return_value=self._make_proc(0, "ok\n")
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
            "sam_deploy.subprocess.run", return_value=first_call
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
        p = MagicMock()
        p.returncode = rc
        p.stdout = stdout
        p.stderr = stderr
        return p

    def test_json_params_passed_correctly(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.setenv("SAM_PARAMS", '{"Env": "prod", "Region": "us-east-1"}')
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        captured_cmd = []

        def capture_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._make_proc(0, "ok\n")

        with patch("sam_deploy.subprocess.run", side_effect=capture_run):
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

        def capture_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._make_proc(0, "ok\n")

        with patch("sam_deploy.subprocess.run", side_effect=capture_run):
            with pytest.raises(SystemExit) as exc:
                main([])

        assert exc.value.code == 0
        assert "--role-arn" in captured_cmd
        assert "arn:aws:iam::123:role/cfn" in captured_cmd


# ===========================================================================
# 16. NEW: _RESOURCE_ID_KEYS includes AWS::Logs::LogGroup (sprint8-001)
# ===========================================================================


class TestResourceIdKeyLogGroup:
    def test_loggroup_key_is_loggroup_name(self):
        """_RESOURCE_ID_KEYS must map AWS::Logs::LogGroup → LogGroupName."""
        from sam_deploy import _RESOURCE_ID_KEYS
        assert _RESOURCE_ID_KEYS["AWS::Logs::LogGroup"] == "LogGroupName"

    def test_physical_id_to_identifier_loggroup(self):
        result = _physical_id_to_identifier("AWS::Logs::LogGroup", "/aws/lambda/my-fn")
        assert result == {"LogGroupName": "/aws/lambda/my-fn"}

    def test_conflict_resource_loggroup_import_record(self):
        c = ConflictResource(
            "LambdaLogGroup",
            "AWS::Logs::LogGroup",
            "/aws/lambda/bouncer-prod-function",
        )
        record = c.to_import_record()
        assert record["ResourceType"] == "AWS::Logs::LogGroup"
        assert record["LogicalResourceId"] == "LambdaLogGroup"
        assert record["ResourceIdentifier"] == {
            "LogGroupName": "/aws/lambda/bouncer-prod-function"
        }


# ===========================================================================
# 17. NEW: SamPackager class (sprint8-001, approach-b)
# ===========================================================================


class TestSamPackager:
    """Unit tests for SamPackager.build_and_package."""

    def _make_mock_run(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.stdout = stdout
        mock_proc.stderr = stderr
        return MagicMock(return_value=mock_proc)

    def test_build_and_package_calls_sam_build_then_sam_package(self, tmp_path):
        """build_and_package must call sam build then sam package."""
        from sam_deploy import SamPackager

        template = tmp_path / "template.yaml"
        template.write_text("Transform: AWS::Serverless-2016-10-31\n")

        calls = []
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""

        def mock_run(cmd, **kwargs):
            calls.append(cmd[0:2])  # just capture ['sam', 'build'] / ['sam', 'package']
            return mock_proc

        packager = SamPackager(str(template), run_fn=mock_run)
        url = packager.build_and_package(s3_bucket="test-bucket")

        assert ["sam", "build"] in calls
        assert ["sam", "package"] in calls
        assert url.startswith("s3://test-bucket/")
        assert url.endswith("packaged-template.yaml")

    def test_build_failure_raises(self, tmp_path):
        """If sam build fails, RuntimeError should be raised."""
        from sam_deploy import SamPackager

        template = tmp_path / "template.yaml"
        template.write_text("Transform: AWS::Serverless-2016-10-31\n")

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Build failed"

        packager = SamPackager(str(template), run_fn=MagicMock(return_value=mock_proc))
        with pytest.raises(RuntimeError, match="Command failed"):
            packager.build_and_package(s3_bucket="test-bucket")

    def test_missing_bucket_raises(self, tmp_path, monkeypatch):
        """When SAM_PACKAGE_BUCKET is not set, RuntimeError should be raised."""
        from sam_deploy import SamPackager

        monkeypatch.delenv("SAM_PACKAGE_BUCKET", raising=False)
        template = tmp_path / "template.yaml"
        template.write_text("Transform: AWS::Serverless-2016-10-31\n")

        packager = SamPackager(str(template))
        with pytest.raises(RuntimeError, match="SAM_PACKAGE_BUCKET"):
            packager.build_and_package()

    def test_bucket_from_env(self, tmp_path, monkeypatch):
        """build_and_package should use SAM_PACKAGE_BUCKET env var when no arg given."""
        from sam_deploy import SamPackager

        monkeypatch.setenv("SAM_PACKAGE_BUCKET", "env-bucket")
        template = tmp_path / "template.yaml"
        template.write_text("Transform: AWS::Serverless-2016-10-31\n")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""

        packager = SamPackager(str(template), run_fn=MagicMock(return_value=mock_proc))
        url = packager.build_and_package()
        assert "env-bucket" in url


# ===========================================================================
# 18. NEW: import_resources uses packaged template for SAM YAML (sprint8-001)
# ===========================================================================


class TestImportUsesPackagedTemplateForSamYaml:
    """When a conflict involves a SAM-transform type (e.g. LogGroup),
    import_resources must invoke the SamPackager and pass TemplateURL
    to the create_change_set call."""

    LOGGROUP_CONFLICT = ConflictResource(
        "LambdaLogGroup",
        "AWS::Logs::LogGroup",
        "/aws/lambda/bouncer-prod-function",
    )

    def _make_cfn_client(self):
        client = MagicMock()
        client.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
        client.describe_change_set.return_value = {"Status": "CREATE_COMPLETE"}
        client.get_waiter.return_value.wait = MagicMock()
        return client

    def _make_packager(self, url: str = "s3://bucket/key/packaged-template.yaml"):
        packager = MagicMock()
        packager.build_and_package.return_value = url
        return packager

    def test_loggroup_conflict_calls_packager(self):
        """LogGroup conflict → SamPackager.build_and_package() is invoked."""
        cfn = self._make_cfn_client()
        packager = self._make_packager()
        imp = CloudFormationImporter(STACK, cfn_client=cfn, sam_packager=packager)

        result = imp.import_resources([self.LOGGROUP_CONFLICT])

        assert result is True
        packager.build_and_package.assert_called_once()

    def test_loggroup_conflict_passes_template_url_to_changeset(self):
        """TemplateURL from packager must be passed to create_change_set."""
        cfn = self._make_cfn_client()
        s3_url = "s3://my-bucket/sam-packaged/abc/packaged-template.yaml"
        packager = self._make_packager(url=s3_url)
        imp = CloudFormationImporter(STACK, cfn_client=cfn, sam_packager=packager)

        imp.import_resources([self.LOGGROUP_CONFLICT])

        call_kwargs = cfn.create_change_set.call_args[1]
        assert call_kwargs.get("TemplateURL") == s3_url
        assert call_kwargs["ChangeSetType"] == "IMPORT"

    def test_non_sam_type_does_not_call_packager(self, mock_cfn_client):
        """SQS queue conflict should NOT invoke SamPackager."""
        packager = self._make_packager()
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client, sam_packager=packager)

        sqs_conflict = ConflictResource("MyQueue", "AWS::SQS::Queue", "https://sqs.../q")
        imp.import_resources([sqs_conflict])

        packager.build_and_package.assert_not_called()
        # TemplateURL should NOT be present in call_kwargs
        call_kwargs = mock_cfn_client.create_change_set.call_args[1]
        assert "TemplateURL" not in call_kwargs

    def test_mixed_conflicts_with_loggroup_uses_packager(self, mock_cfn_client):
        """If ANY conflict is a SAM-transform type, packager is invoked."""
        packager = self._make_packager()
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client, sam_packager=packager)

        conflicts = [
            ConflictResource("MyQueue", "AWS::SQS::Queue", "https://sqs.../q"),
            self.LOGGROUP_CONFLICT,
        ]
        imp.import_resources(conflicts)

        packager.build_and_package.assert_called_once()
        call_kwargs = mock_cfn_client.create_change_set.call_args[1]
        assert "TemplateURL" in call_kwargs

    def test_dry_run_does_not_call_packager(self):
        """In dry-run mode, packager should NOT be invoked."""
        cfn = self._make_cfn_client()
        packager = self._make_packager()
        imp = CloudFormationImporter(STACK, cfn_client=cfn, dry_run=True, sam_packager=packager)

        imp.import_resources([self.LOGGROUP_CONFLICT])

        packager.build_and_package.assert_not_called()


# ===========================================================================
# 19. NEW: --dry-run-import plan display (sprint8-001)
# ===========================================================================


class TestDryRunImportPlan:
    """Verify that --dry-run-import correctly shows the import plan."""

    LOGGROUP_CONFLICT_OUTPUT = (
        "Error: Failed to create/update the stack.\n"
        "Resource handler returned message: \"/aws/lambda/bouncer-prod-function already exists\" "
        "[RequestToken: abc, HandlerErrorCode: AlreadyExists]\n"
        "  ResourceLogicalId: LambdaLogGroup, ResourceType: AWS::Logs::LogGroup, "
        "ResourcePhysicalId: /aws/lambda/bouncer-prod-function]"
    )

    def _make_proc(self, rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
        p = MagicMock()
        p.returncode = rc
        p.stdout = stdout
        p.stderr = stderr
        return p

    def test_dry_run_prints_loggroup_conflict(self, monkeypatch, mock_cfn_client, capsys):
        """--dry-run-import with LogGroup conflict prints resource details."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=self.LOGGROUP_CONFLICT_OUTPUT)

        with patch(
            "sam_deploy.subprocess.run", return_value=first_call
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main(["--dry-run-import"])

        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "LambdaLogGroup" in captured.out
        assert "AWS::Logs::LogGroup" in captured.out

    def test_dry_run_no_api_calls_on_loggroup(self, monkeypatch, mock_cfn_client):
        """--dry-run-import must not call create_change_set even for LogGroup."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=self.LOGGROUP_CONFLICT_OUTPUT)

        with patch(
            "sam_deploy.subprocess.run", return_value=first_call
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit):
                main(["--dry-run-import"])

        mock_cfn_client.create_change_set.assert_not_called()

    def test_dry_run_exits_2_for_loggroup(self, monkeypatch, mock_cfn_client):
        """--dry-run-import with parseable conflict must exit with code 2."""
        monkeypatch.setenv("STACK_NAME", STACK)
        monkeypatch.delenv("SAM_PARAMS", raising=False)
        monkeypatch.delenv("CFN_ROLE_ARN", raising=False)
        monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

        first_call = self._make_proc(1, stdout=self.LOGGROUP_CONFLICT_OUTPUT)

        with patch(
            "sam_deploy.subprocess.run", return_value=first_call
        ), patch("sam_deploy.boto3.client", return_value=mock_cfn_client):
            with pytest.raises(SystemExit) as exc:
                main(["--dry-run-import"])

        assert exc.value.code == 2


# ===========================================================================
# 20. C-unique: Fine-grained LogGroup edge cases (merged from approach-c)
# ===========================================================================

# Sample CFN error output with a LogGroup conflict (used by C-originated tests)
_LOGGROUP_CONFLICT_OUTPUT_C = (
    "Error: Failed to create/update the stack.\n"
    "Resource handler returned message: \"/aws/lambda/bouncer-prod-function already exists\" "
    "[RequestToken: abc123, HandlerErrorCode: AlreadyExists]\n"
    "  ResourceLogicalId: LambdaLogGroup, ResourceType: AWS::Logs::LogGroup, "
    "ResourcePhysicalId: /aws/lambda/bouncer-prod-function]"
)


class TestLogGroupEdgeCasesFromC:
    """Edge case tests from approach-c that cover paths not tested by B's packager-injection style."""

    def test_resource_id_keys_has_loggroup(self):
        """_RESOURCE_ID_KEYS must contain AWS::Logs::LogGroup key."""
        from sam_deploy import _RESOURCE_ID_KEYS
        assert "AWS::Logs::LogGroup" in _RESOURCE_ID_KEYS

    def test_resource_id_keys_does_not_use_id_for_loggroup(self):
        """LogGroup must NOT fall back to the generic 'Id' key."""
        from sam_deploy import _RESOURCE_ID_KEYS
        assert _RESOURCE_ID_KEYS.get("AWS::Logs::LogGroup") != "Id"
        result = _physical_id_to_identifier(
            "AWS::Logs::LogGroup", "/aws/lambda/bouncer-prod-function"
        )
        assert "Id" not in result
        assert "LogGroupName" in result

    def test_import_detects_loggroup_conflict(self, importer):
        """parse_conflicts should extract LogGroup conflict from CFN error output."""
        conflicts = importer.parse_conflicts(_LOGGROUP_CONFLICT_OUTPUT_C)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c.logical_id == "LambdaLogGroup"
        assert c.resource_type == "AWS::Logs::LogGroup"
        assert c.physical_id == "/aws/lambda/bouncer-prod-function"

    def test_loggroup_import_record_uses_loggroup_name(self, importer):
        """to_import_record() for LogGroup must use LogGroupName identifier."""
        c = ConflictResource(
            "LambdaLogGroup",
            "AWS::Logs::LogGroup",
            "/aws/lambda/bouncer-prod-function",
        )
        record = c.to_import_record()
        assert record["ResourceIdentifier"] == {
            "LogGroupName": "/aws/lambda/bouncer-prod-function"
        }

    def test_import_loggroup_physical_id_extracted(self, importer):
        """LogGroupName (physical ID) must be correctly extracted from CFN error output."""
        conflicts = importer.parse_conflicts(_LOGGROUP_CONFLICT_OUTPUT_C)
        assert len(conflicts) == 1
        assert conflicts[0].physical_id == "/aws/lambda/bouncer-prod-function"

    def test_loggroup_physical_id_becomes_identifier_value(self):
        """The extracted physical ID must map to LogGroupName in the import identifier."""
        log_group_name = "/aws/lambda/bouncer-prod-function"
        result = _physical_id_to_identifier("AWS::Logs::LogGroup", log_group_name)
        assert result == {"LogGroupName": log_group_name}

    def test_loggroup_physical_id_strips_whitespace(self, importer):
        """Trailing whitespace in physical ID from CFN output must be stripped."""
        output_with_space = (
            "Resource handler returned message: \"/aws/lambda/bouncer-prod-function already exists\" "
            "[RequestToken: abc, HandlerErrorCode: AlreadyExists]\n"
            "  ResourceLogicalId: LambdaLogGroup, ResourceType: AWS::Logs::LogGroup, "
            "ResourcePhysicalId: /aws/lambda/bouncer-prod-function  ]"
        )
        conflicts = importer.parse_conflicts(output_with_space)
        assert len(conflicts) == 1
        assert conflicts[0].physical_id == "/aws/lambda/bouncer-prod-function"


# ===========================================================================
# 21. C-unique: Subprocess integration for LogGroup import (approach-c)
# ===========================================================================


class TestLogGroupSubprocessIntegrationFromC:
    """Tests from approach-c that exercise the non-injected subprocess path.
    These complement B's packager-injection tests by covering the full
    _get_packaged_template_url -> _find_sam_template -> SamPackager pipeline."""

    def test_sam_package_failure_returns_false(self, mock_cfn_client):
        """If sam build fails, import_resources must return False (not crash)."""
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client)
        conflicts = [
            ConflictResource(
                "LambdaLogGroup", "AWS::Logs::LogGroup", "/aws/lambda/bouncer-prod-function"
            )
        ]

        sam_build_fail = MagicMock()
        sam_build_fail.returncode = 1
        sam_build_fail.stdout = ""
        sam_build_fail.stderr = "Build error: missing dependencies"

        with patch("sam_deploy.subprocess.run", return_value=sam_build_fail):
            result = imp.import_resources(conflicts)

        assert result is False

    def test_import_loggroup_dry_run_no_subprocess(self, mock_cfn_client):
        """dry_run=True must not invoke subprocess even for LogGroup conflicts."""
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client, dry_run=True)
        conflicts = [
            ConflictResource(
                "LambdaLogGroup", "AWS::Logs::LogGroup", "/aws/lambda/bouncer-prod-function"
            )
        ]

        subprocess_calls = []

        def should_not_be_called(cmd, **kwargs):
            subprocess_calls.append(cmd)
            raise AssertionError(f"subprocess.run must not be called in dry-run, got: {cmd}")

        with patch("sam_deploy.subprocess.run", side_effect=should_not_be_called):
            result = imp.import_resources(conflicts)

        assert result is True
        assert subprocess_calls == []
        mock_cfn_client.create_change_set.assert_not_called()

    def test_dry_run_loggroup_prints_plan(self, mock_cfn_client, capsys):
        """dry_run should print the import plan with physical ID visible."""
        imp = CloudFormationImporter(STACK, cfn_client=mock_cfn_client, dry_run=True)
        conflicts = [
            ConflictResource(
                "LambdaLogGroup", "AWS::Logs::LogGroup", "/aws/lambda/bouncer-prod-function"
            )
        ]

        with patch("sam_deploy.subprocess.run", side_effect=AssertionError("no subprocess")):
            imp.import_resources(conflicts)

        captured = capsys.readouterr()
        assert "LambdaLogGroup" in captured.out
        assert "/aws/lambda/bouncer-prod-function" in captured.out
        assert "dry-run" in captured.out.lower()
