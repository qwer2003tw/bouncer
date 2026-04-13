"""
Sprint 33-002: Tests for sam explicit package + template_s3_url DDB update.

Covers:
  TC01 - _run_sam_package + main() success → update_template_s3_url called
  TC02 - update_template_s3_url uses original credentials (called before assume-role overrides env)
  TC03 - DDB update failure is non-fatal (does not raise, deploy continues)
  TC04 - PROJECT_ID empty → update_template_s3_url skips DDB call
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sam_deploy  # noqa: E402,F401
from sam_deploy import (  # noqa: E402
    _PACKAGED_TEMPLATE,
    _run_sam_package,
    update_template_s3_url,
    main,
)


# ---------------------------------------------------------------------------
# TC01: _run_sam_package + main() success → update_template_s3_url called
# ---------------------------------------------------------------------------


class TestRunSamPackage:
    """_run_sam_package constructs the correct sam package command."""

    def test_sam_package_command(self):
        with patch("sam_deploy.subprocess.run") as mock_run:
            _run_sam_package("my-artifacts-bucket", "my-project")

        mock_run.assert_called_once_with(
            [
                "sam", "package",
                "--s3-bucket", "my-artifacts-bucket",
                "--s3-prefix", "my-project/templates",
                "--output-template-file", _PACKAGED_TEMPLATE,
                "--force-upload",
            ],
            check=True,
        )

    def test_sam_package_check_true(self):
        """check=True ensures CalledProcessError propagates on failure."""
        with patch("sam_deploy.subprocess.run") as mock_run:
            _run_sam_package("bucket", "proj")
        _, kwargs = mock_run.call_args
        assert kwargs.get("check") is True


class TestMainCallsPackageAndDDB:
    """main() calls _run_sam_package and update_template_s3_url before _run_deploy."""

    def _make_env(self, **overrides):
        base = {
            "STACK_NAME": "my-stack",
            "SAM_PARAMS": "",
            "CFN_ROLE_ARN": "",
            "TARGET_ROLE_ARN": "",
            "ARTIFACTS_BUCKET": "my-artifacts-bucket",
            "PROJECT_ID": "my-project",
            "PROJECTS_TABLE": "bouncer-projects",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        base.update(overrides)
        return base

    def test_main_success_calls_package_and_ddb(self):
        env = self._make_env()

        mock_deploy_result = MagicMock()
        mock_deploy_result.succeeded = True
        mock_deploy_result.returncode = 0

        call_order = []

        def fake_package(bucket, project_id):
            call_order.append("package")

        def fake_ddb_update(project_id, artifacts_bucket):
            call_order.append("ddb_update")

        def fake_run_deploy(cmd):
            call_order.append("deploy")
            return mock_deploy_result

        with (
            patch.dict(os.environ, env, clear=False),
            patch("sam_deploy._check_github_pat"),
            patch("sam_deploy._run_sam_package", side_effect=fake_package),
            patch("sam_deploy.update_template_s3_url", side_effect=fake_ddb_update),
            patch("sam_deploy._run_deploy", side_effect=fake_run_deploy),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main([])
            assert exc_info.value.code == 0

        # Verify order: package → ddb_update → deploy
        assert call_order == ["package", "ddb_update", "deploy"], (
            f"Expected [package, ddb_update, deploy], got {call_order}"
        )

    def test_main_passes_correct_args_to_package(self):
        env = self._make_env(ARTIFACTS_BUCKET="test-bucket", PROJECT_ID="test-proj")

        mock_deploy_result = MagicMock()
        mock_deploy_result.succeeded = True

        with (
            patch.dict(os.environ, env, clear=False),
            patch("sam_deploy._check_github_pat"),
            patch("sam_deploy._run_sam_package") as mock_pkg,
            patch("sam_deploy.update_template_s3_url"),
            patch("sam_deploy._run_deploy", return_value=mock_deploy_result),
        ):
            with pytest.raises(SystemExit):
                main([])

        mock_pkg.assert_called_once_with("test-bucket", "test-proj")

    def test_main_passes_correct_args_to_ddb_update(self):
        env = self._make_env(ARTIFACTS_BUCKET="test-bucket", PROJECT_ID="test-proj")

        mock_deploy_result = MagicMock()
        mock_deploy_result.succeeded = True

        with (
            patch.dict(os.environ, env, clear=False),
            patch("sam_deploy._check_github_pat"),
            patch("sam_deploy._run_sam_package"),
            patch("sam_deploy.update_template_s3_url") as mock_ddb,
            patch("sam_deploy._run_deploy", return_value=mock_deploy_result),
        ):
            with pytest.raises(SystemExit):
                main([])

        mock_ddb.assert_called_once_with("test-proj", "test-bucket")


# ---------------------------------------------------------------------------
# TC02: update_template_s3_url uses original credentials (env-var based)
# ---------------------------------------------------------------------------


class TestUpdateTemplateS3UrlCredentials:
    """update_template_s3_url creates a boto3 client with region from env (not assumed creds)."""

    def test_uses_env_region(self):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "ap-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_boto3.return_value = mock_ddb

            update_template_s3_url("my-project", "my-bucket")

        mock_boto3.assert_called_once_with("dynamodb", region_name="ap-east-1")

    def test_constructs_correct_url(self):
        """template_url must be https://{bucket}.s3.amazonaws.com/{project}/templates/packaged-template.yaml"""
        captured = {}

        def fake_update_item(**kwargs):
            captured.update(kwargs)

        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_ddb.update_item.side_effect = fake_update_item
            mock_boto3.return_value = mock_ddb

            update_template_s3_url("my-project", "my-bucket")

        expected_url = "https://my-bucket.s3.amazonaws.com/my-project/packaged-template.yaml"
        assert captured["ExpressionAttributeValues"][":url"]["S"] == expected_url

    def test_uses_projects_table_env(self):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "custom-projects-table",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_boto3.return_value = mock_ddb

            update_template_s3_url("proj", "bucket")

        mock_ddb.update_item.assert_called_once()
        call_kwargs = mock_ddb.update_item.call_args[1]
        assert call_kwargs["TableName"] == "custom-projects-table"

    def test_ddb_key_contains_project_id(self):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_boto3.return_value = mock_ddb

            update_template_s3_url("my-project", "my-bucket")

        call_kwargs = mock_ddb.update_item.call_args[1]
        assert call_kwargs["Key"] == {"project_id": {"S": "my-project"}}

    def test_update_expression_correct(self):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_boto3.return_value = mock_ddb

            update_template_s3_url("proj", "bucket")

        call_kwargs = mock_ddb.update_item.call_args[1]
        assert call_kwargs["UpdateExpression"] == "SET template_s3_url = :url"


# ---------------------------------------------------------------------------
# TC03: DDB update failure is non-fatal
# ---------------------------------------------------------------------------


class TestUpdateTemplateS3UrlNonFatal:
    """DDB update failures must not raise — deploy continues regardless."""

    def test_boto3_exception_does_not_raise(self):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_ddb.update_item.side_effect = Exception("DynamoDB timeout")
            mock_boto3.return_value = mock_ddb

            # Must not raise
            update_template_s3_url("proj", "bucket")

    def test_boto3_client_creation_failure_does_not_raise(self):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client", side_effect=Exception("no credentials")),
        ):
            # Must not raise
            update_template_s3_url("proj", "bucket")

    def test_deploy_continues_after_ddb_failure(self):
        """main() must exit 0 even when DDB update raises."""
        env = {
            "STACK_NAME": "my-stack",
            "SAM_PARAMS": "",
            "CFN_ROLE_ARN": "",
            "TARGET_ROLE_ARN": "",
            "ARTIFACTS_BUCKET": "my-bucket",
            "PROJECT_ID": "my-project",
            "PROJECTS_TABLE": "bouncer-projects",
            "AWS_DEFAULT_REGION": "us-east-1",
        }

        mock_deploy_result = MagicMock()
        mock_deploy_result.succeeded = True
        mock_deploy_result.returncode = 0

        with (
            patch.dict(os.environ, env, clear=False),
            patch("sam_deploy._check_github_pat"),
            patch("sam_deploy._run_sam_package"),
            patch("sam_deploy.boto3.client") as mock_boto3,
            patch("sam_deploy._run_deploy", return_value=mock_deploy_result),
        ):
            mock_ddb = MagicMock()
            mock_ddb.update_item.side_effect = Exception("DDB failure")
            mock_boto3.return_value = mock_ddb

            with pytest.raises(SystemExit) as exc_info:
                main([])

        assert exc_info.value.code == 0

    def test_ddb_failure_prints_warning(self, capsys):
        with (
            patch.dict(os.environ, {
                "PROJECTS_TABLE": "bouncer-projects",
                "AWS_DEFAULT_REGION": "us-east-1",
            }),
            patch("sam_deploy.boto3.client") as mock_boto3,
        ):
            mock_ddb = MagicMock()
            mock_ddb.update_item.side_effect = Exception("connection refused")
            mock_boto3.return_value = mock_ddb

            update_template_s3_url("proj", "bucket")

        captured = capsys.readouterr()
        assert "[DDB] Warning:" in captured.out
        assert "connection refused" in captured.out


# ---------------------------------------------------------------------------
# TC04: PROJECT_ID empty → skip DDB update
# ---------------------------------------------------------------------------


class TestUpdateTemplateS3UrlSkipWhenEmpty:
    """update_template_s3_url must be a no-op when PROJECT_ID or ARTIFACTS_BUCKET is empty."""

    def test_empty_project_id_skips(self):
        with patch("sam_deploy.boto3.client") as mock_boto3:
            update_template_s3_url("", "my-bucket")
        mock_boto3.assert_not_called()

    def test_empty_artifacts_bucket_skips(self):
        with patch("sam_deploy.boto3.client") as mock_boto3:
            update_template_s3_url("my-project", "")
        mock_boto3.assert_not_called()

    def test_both_empty_skips(self):
        with patch("sam_deploy.boto3.client") as mock_boto3:
            update_template_s3_url("", "")
        mock_boto3.assert_not_called()

    def test_empty_project_id_prints_skip_message(self, capsys):
        update_template_s3_url("", "my-bucket")
        captured = capsys.readouterr()
        assert "Skipping" in captured.out or "[DDB]" in captured.out

    def test_main_with_empty_project_id_skips_ddb(self):
        """When PROJECT_ID env var is absent/empty, update_template_s3_url must not call DDB."""
        env = {
            "STACK_NAME": "my-stack",
            "SAM_PARAMS": "",
            "CFN_ROLE_ARN": "",
            "TARGET_ROLE_ARN": "",
            "ARTIFACTS_BUCKET": "my-bucket",
            "PROJECT_ID": "",
            "PROJECTS_TABLE": "bouncer-projects",
            "AWS_DEFAULT_REGION": "us-east-1",
        }

        mock_deploy_result = MagicMock()
        mock_deploy_result.succeeded = True
        mock_deploy_result.returncode = 0

        with (
            patch.dict(os.environ, env, clear=False),
            patch("sam_deploy._check_github_pat"),
            patch("sam_deploy._run_sam_package"),
            patch("sam_deploy.boto3.client") as mock_boto3,
            patch("sam_deploy._run_deploy", return_value=mock_deploy_result),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main([])

        assert exc_info.value.code == 0
        mock_boto3.assert_not_called()
