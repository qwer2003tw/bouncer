"""
Tests for bouncer-sec-007: Telegram notification on presigned URL generation.

Coverage:
  Single-file (mcp_tool_request_presigned):
  1. Success path → send_presigned_notification called once
  2. S3 error path → send_presigned_notification NOT called
  3. Notification content does NOT contain presigned URL or X-Amz-Signature

  Batch (mcp_tool_request_presigned_batch):
  4. Success path → send_presigned_batch_notification called once
  5. S3 error path → send_presigned_batch_notification NOT called
  6. Notification content does NOT contain presigned URL or X-Amz-Signature

  Resilience:
  7. Notification failure does NOT break the main flow (returns status=ready)
"""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch, call

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

# ---------------------------------------------------------------------------
# Bootstrap
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
    with mock_aws():
        _reload_src()
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        tbl = dynamodb.create_table(
            TableName="clawdbot-approval-requests",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "request_id", "AttributeType": "S"},
                {"AttributeName": "source", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "source-created-index",
                    "KeySchema": [
                        {"AttributeName": "source", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
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
        "filename": "assets/report.pdf",
        "content_type": "application/pdf",
        "reason": "upload report",
        "source": "Private Bot (test)",
    }
    base.update(overrides)
    return base


def _make_files(n: int) -> list:
    return [
        {"filename": f"file_{i}.js", "content_type": "application/javascript"}
        for i in range(n)
    ]


def _valid_batch_args(**overrides) -> dict:
    base = {
        "files": _make_files(3),
        "reason": "deploy frontend",
        "source": "Private Bot (test)",
    }
    base.update(overrides)
    return base


def _mock_s3(n_files: int = 100) -> MagicMock:
    mock = MagicMock()
    mock.generate_presigned_url.side_effect = [
        f"https://s3.amazonaws.com/fake/file_{i}?X-Amz-Signature=FAKESIG{i}"
        for i in range(n_files)
    ]
    return mock


def _s3_client_error() -> MagicMock:
    mock = MagicMock()
    error_response = {"Error": {"Code": "NoSuchBucket", "Message": "bucket not found"}}
    mock.generate_presigned_url.side_effect = ClientError(error_response, "GeneratePresignedUrl")
    return mock


def _body(result: dict) -> dict:
    if "body" in result:
        outer = json.loads(result["body"])
    else:
        outer = result
    if "result" in outer:
        rpc_payload = outer["result"]
    elif "error" in outer:
        return {"status": "error", "error": outer["error"].get("message", str(outer["error"]))}
    else:
        rpc_payload = outer
    if "content" in rpc_payload:
        text = rpc_payload["content"][0]["text"]
        return json.loads(text)
    return rpc_payload


# ===========================================================================
# Single-file tests
# ===========================================================================

def test_single_success_notify_called_once(mocked_aws):
    """Success path → send_presigned_notification called exactly once."""
    tbl, mcp_presigned = mocked_aws

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()), \
         patch("mcp_presigned.send_presigned_notification") as mock_notify:
        result = mcp_presigned.mcp_tool_request_presigned("req-n1", _valid_args())

    body = _body(result)
    assert body["status"] == "ready", f"unexpected body: {body}"
    mock_notify.assert_called_once()


def test_single_s3_error_notify_not_called(mocked_aws):
    """S3 failure path → send_presigned_notification must NOT be called."""
    tbl, mcp_presigned = mocked_aws

    with patch("mcp_presigned.boto3.client", return_value=_s3_client_error()), \
         patch("mcp_presigned.send_presigned_notification") as mock_notify:
        result = mcp_presigned.mcp_tool_request_presigned("req-n2", _valid_args())

    body = _body(result)
    assert body["status"] == "error"
    mock_notify.assert_not_called()


def test_single_notify_args_no_presigned_url(mocked_aws):
    """Notification call args must NOT contain the presigned URL or X-Amz-Signature."""
    tbl, mcp_presigned = mocked_aws
    captured_kwargs = {}

    def capture_notify(**kwargs):
        captured_kwargs.update(kwargs)

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()), \
         patch("mcp_presigned.send_presigned_notification", side_effect=capture_notify):
        mcp_presigned.mcp_tool_request_presigned("req-n3", _valid_args())

    # Must have been called (sanity)
    assert captured_kwargs, "send_presigned_notification was never called"

    # No kwarg value should contain the presigned URL or signature fragment
    for key, val in captured_kwargs.items():
        val_str = str(val)
        assert "X-Amz-Signature" not in val_str, f"kwarg '{key}' contains X-Amz-Signature"
        assert "https://s3.amazonaws.com/fake" not in val_str, \
            f"kwarg '{key}' contains presigned URL"


def test_single_notify_failure_does_not_break_flow(mocked_aws):
    """If send_presigned_notification raises, the tool must still return status=ready."""
    tbl, mcp_presigned = mocked_aws

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()), \
         patch("mcp_presigned.send_presigned_notification",
               side_effect=RuntimeError("telegram down")):
        result = mcp_presigned.mcp_tool_request_presigned("req-n4", _valid_args())

    body = _body(result)
    assert body["status"] == "ready", f"notification failure broke main flow: {body}"


def test_single_notify_receives_correct_fields(mocked_aws):
    """send_presigned_notification receives filename, source, account_id, expires_at."""
    tbl, mcp_presigned = mocked_aws
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()), \
         patch("mcp_presigned.send_presigned_notification", side_effect=capture):
        mcp_presigned.mcp_tool_request_presigned(
            "req-n5",
            _valid_args(filename="test.pdf", source="MyBot", account="190825685292"),
        )

    assert captured.get("filename") == "test.pdf"
    assert captured.get("source") == "MyBot"
    assert captured.get("account_id") == "190825685292"
    assert "expires_at" in captured
    # expires_at is an ISO 8601 string
    assert "T" in captured["expires_at"]


# ===========================================================================
# Batch tests
# ===========================================================================

def test_batch_success_notify_called_once(mocked_aws):
    """Batch success path → send_presigned_batch_notification called exactly once."""
    tbl, mcp_presigned = mocked_aws

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(10)), \
         patch("mcp_presigned.send_presigned_batch_notification") as mock_notify:
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-bn1", _valid_batch_args()
        )

    body = _body(result)
    assert body["status"] == "ready", f"unexpected body: {body}"
    mock_notify.assert_called_once()


def test_batch_s3_error_notify_not_called(mocked_aws):
    """Batch S3 error path → send_presigned_batch_notification must NOT be called."""
    tbl, mcp_presigned = mocked_aws

    with patch("mcp_presigned.boto3.client", return_value=_s3_client_error()), \
         patch("mcp_presigned.send_presigned_batch_notification") as mock_notify:
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-bn2", _valid_batch_args()
        )

    body = _body(result)
    assert body["status"] == "error"
    mock_notify.assert_not_called()


def test_batch_notify_args_no_presigned_url(mocked_aws):
    """Batch notification call args must NOT contain presigned URLs or X-Amz-Signature."""
    tbl, mcp_presigned = mocked_aws
    captured_kwargs = {}

    def capture_notify(**kwargs):
        captured_kwargs.update(kwargs)

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(10)), \
         patch("mcp_presigned.send_presigned_batch_notification", side_effect=capture_notify):
        mcp_presigned.mcp_tool_request_presigned_batch("req-bn3", _valid_batch_args())

    assert captured_kwargs, "send_presigned_batch_notification was never called"

    for key, val in captured_kwargs.items():
        val_str = str(val)
        assert "X-Amz-Signature" not in val_str, f"kwarg '{key}' contains X-Amz-Signature"
        assert "FAKESIG" not in val_str, f"kwarg '{key}' contains fake presigned URL"


def test_batch_notify_failure_does_not_break_flow(mocked_aws):
    """If send_presigned_batch_notification raises, batch tool still returns status=ready."""
    tbl, mcp_presigned = mocked_aws

    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(10)), \
         patch("mcp_presigned.send_presigned_batch_notification",
               side_effect=RuntimeError("telegram down")):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-bn4", _valid_batch_args()
        )

    body = _body(result)
    assert body["status"] == "ready", f"notification failure broke batch flow: {body}"


def test_batch_notify_receives_correct_count(mocked_aws):
    """send_presigned_batch_notification receives correct file count."""
    tbl, mcp_presigned = mocked_aws
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)

    files = _make_files(7)
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(10)), \
         patch("mcp_presigned.send_presigned_batch_notification", side_effect=capture):
        mcp_presigned.mcp_tool_request_presigned_batch(
            "req-bn5",
            _valid_batch_args(files=files, source="BatchBot", account="190825685292"),
        )

    assert captured.get("count") == 7
    assert captured.get("source") == "BatchBot"
    assert captured.get("account_id") == "190825685292"
    assert "expires_at" in captured
    assert "T" in captured["expires_at"]
