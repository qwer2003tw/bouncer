"""
Tests for bouncer_request_presigned (Approach B).

Coverage targets:
  - Happy path
  - Missing required parameters
  - expires_in range (< 60, > 3600, type error)
  - Filename sanitization / path-traversal guard
  - DynamoDB write (and failure fallthrough)
  - S3 generate_presigned_url ClientError
  - S3 generate_presigned_url unexpected exception
  - Rate limiting
"""

import importlib
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

# ---------------------------------------------------------------------------
# Environment must be set BEFORE any src module is imported
# ---------------------------------------------------------------------------

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("TABLE_NAME", "clawdbot-approval-requests")
os.environ.setdefault("DEFAULT_ACCOUNT_ID", "190825685292")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("REQUEST_SECRET", "test-secret")
os.environ.setdefault("APPROVED_CHAT_ID", "999999999")

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ---------------------------------------------------------------------------
# Module-reload helper
# ---------------------------------------------------------------------------

_PRESIGNED_MODS = [
    "constants", "db", "utils", "rate_limit",
    "mcp_presigned",
]


def _reload_presigned():
    """Remove cached modules so the next import picks up mocked AWS clients."""
    for mod in _PRESIGNED_MODS:
        sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_result(result: dict) -> dict:
    """Extract the inner JSON payload from an mcp_result response."""
    body = json.loads(result["body"])
    result_obj = body.get("result", {})
    content = result_obj.get("content", [])
    if content:
        return json.loads(content[0]["text"])
    return result_obj


def _make_valid_args(**overrides):
    base = {
        "filename": "assets/pdf.worker.min.mjs",
        "content_type": "application/javascript",
        "reason": "deploy ztp-files frontend",
        "source": "Private Bot (deploy)",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Session-level moto mock  (stays active for the entire test session)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def aws_mock():
    """Activate moto for the entire test session and create the DDB table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="clawdbot-approval-requests",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "request_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # Reload modules inside the mock context so they use moto endpoints
        _reload_presigned()
        yield ddb


@pytest.fixture
def presigned_mod(aws_mock):
    """Return the mcp_presigned module (guaranteed to be loaded inside moto)."""
    import mcp_presigned  # noqa: PLC0415
    return mcp_presigned


@pytest.fixture
def mock_ddb_table(aws_mock):
    """Return the moto DynamoDB table used by mcp_presigned."""
    return aws_mock.Table("clawdbot-approval-requests")


@pytest.fixture
def mock_s3_presigned():
    """Return a mock S3 client whose generate_presigned_url returns a fake URL."""
    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = (
        "https://bouncer-uploads-190825685292.s3.amazonaws.com/presigned-test"
    )
    return mock_client


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_ready_status(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("req1", _make_valid_args())
            )
        assert result["status"] == "ready"

    def test_returns_all_required_fields(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("req2", _make_valid_args())
            )
        assert "presigned_url" in result
        assert "s3_key" in result
        assert "s3_uri" in result
        assert "request_id" in result
        assert "expires_at" in result
        assert result["method"] == "PUT"
        assert result["headers"]["Content-Type"] == "application/javascript"

    def test_s3_key_format(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        """s3_key must follow {date}/{request_id}/{filename} pattern."""
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("req3", _make_valid_args())
            )
        date_str = time.strftime("%Y-%m-%d")
        assert result["s3_key"].startswith(date_str + "/")
        assert result["s3_key"].endswith("/assets/pdf.worker.min.mjs")

    def test_s3_uri_matches_key(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("req4", _make_valid_args())
            )
        # s3_uri must be consistent with the s3_key
        assert result["s3_uri"] == f"s3://{result['s3_uri'].split('/')[2]}/{result['s3_key']}"

    def test_custom_expires_in(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned(
                    "req5", _make_valid_args(expires_in=1800)
                )
            )
        assert result["status"] == "ready"
        call_kwargs = mock_s3_presigned.generate_presigned_url.call_args[1]
        assert call_kwargs["ExpiresIn"] == 1800

    def test_custom_account(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned(
                    "req6", _make_valid_args(account="992382394211")
                )
            )
        assert "s3://bouncer-uploads-992382394211/" in result["s3_uri"]

    def test_presigned_url_passed_through(self, presigned_mod, mock_ddb_table, mock_s3_presigned):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("req7", _make_valid_args())
            )
        assert result["presigned_url"].startswith("https://")


# ---------------------------------------------------------------------------
# Parameter validation tests
# ---------------------------------------------------------------------------


class TestParameterValidation:
    def _call(self, presigned_mod, args):
        return _parse_result(
            presigned_mod.mcp_tool_request_presigned("v1", args)
        )

    def test_missing_filename(self, presigned_mod):
        args = _make_valid_args()
        del args["filename"]
        result = self._call(presigned_mod, args)
        assert result["status"] == "error"
        assert "filename" in result["error"]

    def test_missing_content_type(self, presigned_mod):
        args = _make_valid_args()
        del args["content_type"]
        result = self._call(presigned_mod, args)
        assert result["status"] == "error"
        assert "content_type" in result["error"]

    def test_missing_reason(self, presigned_mod):
        args = _make_valid_args()
        del args["reason"]
        result = self._call(presigned_mod, args)
        assert result["status"] == "error"
        assert "reason" in result["error"]

    def test_missing_source(self, presigned_mod):
        args = _make_valid_args()
        del args["source"]
        result = self._call(presigned_mod, args)
        assert result["status"] == "error"
        assert "source" in result["error"]

    def test_expires_in_too_large(self, presigned_mod):
        result = self._call(presigned_mod, _make_valid_args(expires_in=3601))
        assert result["status"] == "error"
        assert "3600" in result["error"]

    def test_expires_in_too_small(self, presigned_mod):
        """Approach B: minimum expires_in validation."""
        result = self._call(presigned_mod, _make_valid_args(expires_in=30))
        assert result["status"] == "error"
        assert "60" in result["error"]

    def test_expires_in_zero(self, presigned_mod):
        result = self._call(presigned_mod, _make_valid_args(expires_in=0))
        assert result["status"] == "error"

    def test_expires_in_negative(self, presigned_mod):
        result = self._call(presigned_mod, _make_valid_args(expires_in=-10))
        assert result["status"] == "error"

    def test_expires_in_non_integer_string(self, presigned_mod):
        result = self._call(presigned_mod, _make_valid_args(expires_in="abc"))
        assert result["status"] == "error"
        assert "integer" in result["error"]

    def test_expires_in_exactly_60_accepted(
        self, presigned_mod, mock_ddb_table, mock_s3_presigned
    ):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned(
                    "ve1", _make_valid_args(expires_in=60)
                )
            )
        assert result["status"] == "ready"

    def test_expires_in_exactly_3600_accepted(
        self, presigned_mod, mock_ddb_table, mock_s3_presigned
    ):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned(
                    "ve2", _make_valid_args(expires_in=3600)
                )
            )
        assert result["status"] == "ready"


# ---------------------------------------------------------------------------
# Filename sanitization unit tests
# ---------------------------------------------------------------------------


class TestFilenameSanitization:
    def _sanitize(self, presigned_mod, filename: str) -> str:
        return presigned_mod._sanitize_filename_presigned(filename)

    def test_normal_filename(self, presigned_mod):
        assert self._sanitize(presigned_mod, "file.txt") == "file.txt"

    def test_subpath_preserved(self, presigned_mod):
        assert (
            self._sanitize(presigned_mod, "assets/pdf.worker.min.mjs")
            == "assets/pdf.worker.min.mjs"
        )

    def test_path_traversal_dotdot(self, presigned_mod):
        result = self._sanitize(presigned_mod, "../../etc/passwd")
        assert ".." not in result

    def test_path_traversal_in_middle(self, presigned_mod):
        result = self._sanitize(presigned_mod, "assets/../../../etc/passwd")
        assert ".." not in result

    def test_absolute_path_stripped(self, presigned_mod):
        result = self._sanitize(presigned_mod, "/absolute/path/file.txt")
        assert not result.startswith("/")

    def test_windows_backslash(self, presigned_mod):
        result = self._sanitize(presigned_mod, "assets\\pdf.worker.min.mjs")
        assert "\\" not in result

    def test_null_byte_removed(self, presigned_mod):
        result = self._sanitize(presigned_mod, "file\x00.txt")
        assert "\x00" not in result

    def test_empty_string_returns_unnamed(self, presigned_mod):
        assert self._sanitize(presigned_mod, "") == "unnamed"

    def test_path_traversal_not_in_s3_key(
        self, presigned_mod, mock_ddb_table, mock_s3_presigned
    ):
        """Path traversal input must not produce '..' in the s3_key."""
        args = _make_valid_args(filename="../../etc/passwd")
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://example.com/url"
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_client
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("pt1", args)
            )
        if result.get("status") == "ready":
            assert ".." not in result["s3_key"]

    def test_only_dotdot_filename_rejected(self, presigned_mod):
        """A filename that sanitizes to nothing should be rejected."""
        args = _make_valid_args(filename="../../..")
        result = _parse_result(
            presigned_mod.mcp_tool_request_presigned("pt2", args)
        )
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# DynamoDB audit record tests
# ---------------------------------------------------------------------------


class TestDynamoDBWrite:
    def test_audit_record_written(
        self, presigned_mod, mock_ddb_table, mock_s3_presigned
    ):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("ddb1", _make_valid_args())
            )
        assert result["status"] == "ready"
        request_id = result["request_id"]
        item = mock_ddb_table.get_item(Key={"request_id": request_id}).get("Item")
        assert item is not None
        assert item["action"] == "presigned_upload"
        assert item["status"] == "url_issued"

    def test_audit_record_fields(
        self, presigned_mod, mock_ddb_table, mock_s3_presigned
    ):
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_s3_presigned
            args = _make_valid_args(
                filename="assets/test.js",
                content_type="application/javascript",
                reason="test reason",
                source="Test Bot",
            )
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("ddb2", args)
            )
        request_id = result["request_id"]
        item = mock_ddb_table.get_item(Key={"request_id": request_id}).get("Item")
        assert item["filename"] == "assets/test.js"
        assert item["content_type"] == "application/javascript"
        assert item["source"] == "Test Bot"
        assert item["reason"] == "test reason"
        assert "bucket" in item
        assert "s3_key" in item
        assert "expires_at" in item
        assert "ttl" in item

    def test_ddb_failure_does_not_block_response(
        self, presigned_mod, mock_s3_presigned
    ):
        """DynamoDB write failure should be non-fatal — presigned URL still returned."""
        failing_table = MagicMock()
        failing_table.put_item.side_effect = Exception("DDB unavailable")
        with patch.object(presigned_mod, "table", failing_table):
            with patch.object(presigned_mod, "boto3") as mb:
                mb.client.return_value = mock_s3_presigned
                result = _parse_result(
                    presigned_mod.mcp_tool_request_presigned("ddb3", _make_valid_args())
                )
        assert result["status"] == "ready"
        assert "presigned_url" in result


# ---------------------------------------------------------------------------
# S3 failure tests
# ---------------------------------------------------------------------------


class TestS3Failures:
    def test_client_error_returns_error(self, presigned_mod, mock_ddb_table):
        """ClientError from S3 should return a clear error message."""
        mock_client = MagicMock()
        mock_client.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "GeneratePresignedUrl",
        )
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_client
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("s3err1", _make_valid_args())
            )
        assert result["status"] == "error"
        assert "AccessDenied" in result["error"] or "Access Denied" in result["error"]

    def test_client_error_includes_code(self, presigned_mod, mock_ddb_table):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "The bucket does not exist"}},
            "GeneratePresignedUrl",
        )
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_client
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("s3err2", _make_valid_args())
            )
        assert result["status"] == "error"
        assert "NoSuchBucket" in result["error"]

    def test_unexpected_exception_returns_error(self, presigned_mod, mock_ddb_table):
        """Non-ClientError exceptions should also produce a clear error."""
        mock_client = MagicMock()
        mock_client.generate_presigned_url.side_effect = RuntimeError("Network timeout")
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_client
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("s3err3", _make_valid_args())
            )
        assert result["status"] == "error"
        assert "Network timeout" in result["error"] or "RuntimeError" in result["error"]

    def test_error_response_is_error_flag(self, presigned_mod, mock_ddb_table):
        """isError must be True in the MCP result on S3 failure."""
        mock_client = MagicMock()
        mock_client.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "Internal S3 error"}},
            "GeneratePresignedUrl",
        )
        with patch.object(presigned_mod, "boto3") as mb:
            mb.client.return_value = mock_client
            raw = presigned_mod.mcp_tool_request_presigned("s3err4", _make_valid_args())
        body = json.loads(raw["body"])
        assert body["result"].get("isError") is True


# ---------------------------------------------------------------------------
# Rate limiting tests (Approach B)
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_rate_limit_exceeded_returns_error(self, presigned_mod):
        from rate_limit import RateLimitExceeded

        with patch.object(presigned_mod, "check_rate_limit") as mock_rl:
            mock_rl.side_effect = RateLimitExceeded("Rate limit exceeded: 5/5")
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("rl1", _make_valid_args())
            )
        assert result["status"] == "error"
        assert "Rate limit" in result["error"]

    def test_pending_limit_exceeded_returns_error(self, presigned_mod):
        from rate_limit import PendingLimitExceeded

        with patch.object(presigned_mod, "check_rate_limit") as mock_rl:
            mock_rl.side_effect = PendingLimitExceeded("Pending limit exceeded: 10/10")
            result = _parse_result(
                presigned_mod.mcp_tool_request_presigned("rl2", _make_valid_args())
            )
        assert result["status"] == "error"
        assert "Pending limit" in result["error"]

    def test_no_source_skips_rate_limit(self, presigned_mod):
        """When source is missing (validation error), rate limiter should not be called."""
        with patch.object(presigned_mod, "check_rate_limit") as mock_rl:
            args = _make_valid_args()
            del args["source"]
            presigned_mod.mcp_tool_request_presigned("rl3", args)
        mock_rl.assert_not_called()

    def test_rate_limit_called_with_source(
        self, presigned_mod, mock_ddb_table, mock_s3_presigned
    ):
        """check_rate_limit should be invoked with the source value."""
        with patch.object(presigned_mod, "check_rate_limit") as mock_rl:
            with patch.object(presigned_mod, "boto3") as mb:
                mb.client.return_value = mock_s3_presigned
                presigned_mod.mcp_tool_request_presigned(
                    "rl4", _make_valid_args(source="MyBot")
                )
        mock_rl.assert_called_once_with("MyBot")
