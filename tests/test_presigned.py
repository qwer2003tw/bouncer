"""
Tests for mcp_tool_request_presigned (Approach C - dataclass pipeline style).

Covers:
- Happy path
- Missing required params
- expires_in validation (> 3600, <= 0, non-integer)
- filename sanitization / path traversal
- DynamoDB write verification
- S3 generate_presigned_url failure
"""

import importlib
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Set region early (before any boto3 import inside src modules)
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

_SRC_MODS = [
    "mcp_presigned", "db", "constants", "utils", "accounts",
    "mcp_tools", "mcp_upload", "mcp_admin", "mcp_execute", "app",
    "trust", "telegram", "notifications", "callbacks", "rate_limit",
    "commands", "paging", "tool_schema", "grant", "smart_approval",
    "risk_scorer", "sequence_analyzer", "compliance_checker",
    "template_scanner", "help_command", "telegram_commands", "metrics",
    "deployer",
]


def _reload_src():
    """Clear and reload src modules inside a mock_aws context."""
    for mod in _SRC_MODS:
        sys.modules.pop(mod, None)
        sys.modules.pop(f"src.{mod}", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_ACCOUNT_ID", "190825685292")
    monkeypatch.setenv("TABLE_NAME", "clawdbot-approval-requests")
    monkeypatch.setenv("ACCOUNTS_TABLE_NAME", "bouncer-accounts")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("APPROVED_CHAT_ID", "12345")
    monkeypatch.setenv("REQUEST_SECRET", "test-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture()
def mocked_aws(aws_env):
    """Provide a live mock_aws context with DynamoDB table pre-created."""
    with mock_aws():
        _reload_src()
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        tbl = dynamodb.create_table(
            TableName="clawdbot-approval-requests",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "request_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        tbl.wait_until_exists()

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="bouncer-uploads-190825685292")

        import mcp_presigned
        import db as db_mod

        db_mod.table = tbl
        mcp_presigned.table = tbl

        yield tbl, mcp_presigned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_args(**overrides):
    base = {
        "filename": "assets/pdf.worker.min.mjs",
        "content_type": "application/javascript",
        "reason": "deploy ZTP frontend",
        "source": "Private Bot (deploy)",
    }
    base.update(overrides)
    return base


def _body(result: dict) -> dict:
    """Extract the JSON payload from an MCP result dict.

    mcp_result() returns an HTTP-envelope:
      {"statusCode": 200, "body": "<json-string>"}
    where body is a JSON-RPC response:
      {"jsonrpc": "2.0", "result": {"content": [{"text": "<inner-json>"}]}}
    """
    # Unwrap HTTP envelope
    if "body" in result:
        outer = json.loads(result["body"])
    else:
        outer = result

    # Unwrap JSON-RPC result or error
    if "result" in outer:
        rpc_payload = outer["result"]
    elif "error" in outer:
        # JSON-RPC error object (protocol-level, not business-level)
        return {"status": "error", "error": outer["error"].get("message", str(outer["error"]))}
    else:
        rpc_payload = outer

    # Content array
    if "content" in rpc_payload:
        text = rpc_payload["content"][0]["text"]
        return json.loads(text)

    return rpc_payload


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_ready(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/fake"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned("req-1", _valid_args())

    body = _body(result)
    assert body["status"] == "ready"
    assert body["presigned_url"] == "https://s3.amazonaws.com/fake"
    assert body["method"] == "PUT"
    assert "s3_key" in body
    assert "s3_uri" in body
    assert "request_id" in body
    assert "expires_at" in body
    assert body["headers"]["Content-Type"] == "application/javascript"


def test_s3_key_contains_filename(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned("req-2", _valid_args())

    body = _body(result)
    assert "pdf.worker.min.mjs" in body["s3_key"]


def test_s3_uri_uses_correct_bucket(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned("req-3", _valid_args())

    body = _body(result)
    assert body["s3_uri"].startswith("s3://bouncer-uploads-190825685292/")


def test_custom_expires_in(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-4", _valid_args(expires_in=1800)
        )

    body = _body(result)
    assert body["status"] == "ready"
    # Verify ExpiresIn was forwarded to boto3
    call_kwargs = mock_s3.generate_presigned_url.call_args[1]
    assert call_kwargs.get("ExpiresIn") == 1800


def test_custom_account(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-5", _valid_args(account="992382394211")
        )

    body = _body(result)
    assert "992382394211" in body["s3_uri"]


# ---------------------------------------------------------------------------
# Tests — DynamoDB write
# ---------------------------------------------------------------------------


def test_dynamodb_audit_record_written(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned("req-6", _valid_args())

    body = _body(result)
    request_id = body["request_id"]

    item = tbl.get_item(Key={"request_id": request_id}).get("Item")
    assert item is not None
    assert item["action"] == "presigned_upload"
    assert item["status"] == "url_issued"
    assert item["filename"] == "assets/pdf.worker.min.mjs"
    assert item["content_type"] == "application/javascript"
    assert item["source"] == "Private Bot (deploy)"
    assert item["reason"] == "deploy ZTP frontend"
    assert "expires_at" in item
    assert "created_at" in item


# ---------------------------------------------------------------------------
# Tests — missing required params
# ---------------------------------------------------------------------------


def test_missing_filename_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["filename"]
    result = mcp_presigned.mcp_tool_request_presigned("req-7", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "filename" in body["error"]


def test_missing_content_type_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["content_type"]
    result = mcp_presigned.mcp_tool_request_presigned("req-8", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "content_type" in body["error"]


def test_missing_reason_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["reason"]
    result = mcp_presigned.mcp_tool_request_presigned("req-9", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "reason" in body["error"]


def test_missing_source_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["source"]
    result = mcp_presigned.mcp_tool_request_presigned("req-10", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "source" in body["error"]


# ---------------------------------------------------------------------------
# Tests — expires_in validation
# ---------------------------------------------------------------------------


def test_expires_in_exceeds_max(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned(
        "req-11", _valid_args(expires_in=3601)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "3600" in body["error"]


def test_expires_in_at_exact_max_is_ok(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-12", _valid_args(expires_in=3600)
        )

    body = _body(result)
    assert body["status"] == "ready"


def test_expires_in_zero_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned(
        "req-13", _valid_args(expires_in=0)
    )
    body = _body(result)
    assert body["status"] == "error"


def test_expires_in_non_integer_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned(
        "req-14", _valid_args(expires_in="not-a-number")
    )
    body = _body(result)
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# Tests — filename sanitization / path traversal
# ---------------------------------------------------------------------------


def test_path_traversal_removed(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-15", _valid_args(filename="../../etc/passwd")
        )

    body = _body(result)
    assert body["status"] == "ready"
    assert ".." not in body["s3_key"]


def test_null_byte_removed(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-16", _valid_args(filename="file\x00name.txt")
        )

    body = _body(result)
    assert body["status"] == "ready"
    assert "\x00" not in body["s3_key"]


def test_windows_backslash_normalized(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-17", _valid_args(filename=r"assets\foo.js")
        )

    body = _body(result)
    assert body["status"] == "ready"
    assert "\\" not in body["s3_key"]


def test_subdir_path_preserved(mocked_aws):
    """assets/foo.js should keep its sub-directory structure."""
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned(
            "req-18", _valid_args(filename="assets/chunk-abc.js")
        )

    body = _body(result)
    assert "assets/chunk-abc.js" in body["s3_key"]


# ---------------------------------------------------------------------------
# Tests — S3 generate failure
# ---------------------------------------------------------------------------


def test_s3_generate_failure_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = Exception("S3 credentials expired")

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned("req-19", _valid_args())

    body = _body(result)
    assert body["status"] == "error"
    assert "S3 credentials expired" in body["error"] or "Failed" in body["error"]


# ---------------------------------------------------------------------------
# Tests — _sanitize_filename unit tests (pure function, no AWS needed)
# ---------------------------------------------------------------------------


def test_sanitize_preserves_safe_filename(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    assert mcp_presigned._sanitize_filename("hello.txt") == "hello.txt"


def test_sanitize_removes_dotdot(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned._sanitize_filename("../../etc/passwd")
    assert ".." not in result


def test_sanitize_empty_returns_unnamed(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    assert mcp_presigned._sanitize_filename("") == "unnamed"


def test_sanitize_preserves_subdir(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned._sanitize_filename("assets/foo.js")
    assert result == "assets/foo.js"


def test_sanitize_normalizes_backslash(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned._sanitize_filename(r"assets\bar.css")
    assert "\\" not in result
    assert "bar.css" in result
