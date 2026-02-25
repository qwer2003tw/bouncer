"""
Tests for mcp_tool_request_presigned_batch (Approach B — Aggressive Abstraction).

Covers all 10 spec-required test scenarios:
1.  Normal path: multiple files, correct output format
2.  Empty files array → error
3.  More than 50 files → error
4.  Missing required params (filename / content_type / reason / source)
5.  expires_in validation (min / max)
6.  Filename sanitization (path traversal)
7.  DynamoDB batch audit record written
8.  S3 generate failure → rollback with partial result
9.  Rate limit exceeded
10. All s3_keys share the same batch_id prefix
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
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
    """Live mock_aws context with DynamoDB table + S3 bucket pre-created."""
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


def _make_files(n: int = 3) -> list:
    """Return a list of *n* valid file dicts."""
    return [
        {"filename": f"file{i}.txt", "content_type": "text/plain"}
        for i in range(n)
    ]


def _valid_args(**overrides) -> dict:
    base = {
        "files": _make_files(3),
        "reason": "ZTP Files frontend deploy",
        "source": "Private Bot (deploy)",
    }
    base.update(overrides)
    return base


def _body(result: dict) -> dict:
    """Unwrap HTTP-envelope + JSON-RPC result into the inner payload dict."""
    if "body" in result:
        outer = json.loads(result["body"])
    else:
        outer = result

    if "result" in outer:
        rpc_payload = outer["result"]
    elif "error" in outer:
        return {
            "status": "error",
            "error": outer["error"].get("message", str(outer["error"])),
        }
    else:
        rpc_payload = outer

    if "content" in rpc_payload:
        text = rpc_payload["content"][0]["text"]
        return json.loads(text)

    return rpc_payload


def _mock_s3_ok():
    """Return a mock S3 client that always succeeds."""
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/fake"
    return mock_s3


# ---------------------------------------------------------------------------
# Test 1: Normal path — multiple files, correct output format
# ---------------------------------------------------------------------------


def test_happy_path_returns_ready(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-1", _valid_args()
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert "batch_id" in body
    assert body["file_count"] == 3
    assert len(body["files"]) == 3
    assert "expires_at" in body
    assert body["bucket"] == "bouncer-uploads-190825685292"


def test_happy_path_file_fields_complete(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-2", _valid_args()
        )
    body = _body(result)
    first = body["files"][0]
    assert "filename" in first
    assert "presigned_url" in first
    assert "s3_key" in first
    assert "s3_uri" in first
    assert first["method"] == "PUT"
    assert "headers" in first
    assert "Content-Type" in first["headers"]


# ---------------------------------------------------------------------------
# Test 2: Empty files array → error
# ---------------------------------------------------------------------------


def test_empty_files_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-3", _valid_args(files=[])
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "empty" in body["error"].lower() or "files" in body["error"].lower()


# ---------------------------------------------------------------------------
# Test 3: More than 50 files → error
# ---------------------------------------------------------------------------


def test_too_many_files_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    big_list = _make_files(51)
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-4", _valid_args(files=big_list)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "50" in body["error"]


def test_exactly_50_files_succeeds(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-5", _valid_args(files=_make_files(50))
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 50


# ---------------------------------------------------------------------------
# Test 4: Missing required params
# ---------------------------------------------------------------------------


def test_missing_reason_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["reason"]
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-b-6", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "reason" in body["error"]


def test_missing_files_param_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["files"]
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-b-7", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "files" in body["error"].lower()


def test_missing_filename_in_file_entry_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    bad_files = [{"content_type": "text/plain"}]  # missing filename
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-8", _valid_args(files=bad_files)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "filename" in body["error"].lower()


def test_missing_content_type_in_file_entry_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    bad_files = [{"filename": "foo.txt"}]  # missing content_type
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-9", _valid_args(files=bad_files)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "content_type" in body["error"].lower()


# ---------------------------------------------------------------------------
# Test 5: expires_in validation (min / max)
# ---------------------------------------------------------------------------


def test_expires_in_exceeds_max_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-10", _valid_args(expires_in=3601)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "3600" in body["error"]


def test_expires_in_below_min_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-11", _valid_args(expires_in=30)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "60" in body["error"]


def test_expires_in_at_exact_max_ok(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-12", _valid_args(expires_in=3600)
        )
    body = _body(result)
    assert body["status"] == "ready"


def test_expires_in_at_exact_min_ok(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-13", _valid_args(expires_in=60)
        )
    body = _body(result)
    assert body["status"] == "ready"


def test_expires_in_non_integer_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-b-14", _valid_args(expires_in="bad")
    )
    body = _body(result)
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# Test 6: Filename sanitization / path traversal
# ---------------------------------------------------------------------------


def test_path_traversal_removed_in_s3_keys(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "../../etc/passwd", "content_type": "text/plain"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-15", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    for f in body["files"]:
        assert ".." not in f["s3_key"]


def test_windows_backslash_normalized(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": r"assets\foo.js", "content_type": "application/javascript"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-16", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert "\\" not in body["files"][0]["s3_key"]


def test_subdir_structure_preserved(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "assets/chunk-abc.js", "content_type": "application/javascript"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-17", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert "assets/chunk-abc.js" in body["files"][0]["s3_key"]


# ---------------------------------------------------------------------------
# Test 7: DynamoDB batch audit record written
# ---------------------------------------------------------------------------


def test_dynamodb_batch_audit_record_written(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-18", _valid_args()
        )
    body = _body(result)
    assert body["status"] == "ready"
    batch_id = body["batch_id"]

    item = tbl.get_item(Key={"request_id": batch_id}).get("Item")
    assert item is not None
    assert item["action"] == "presigned_upload_batch"
    assert item["status"] == "urls_issued"
    assert item["batch_id"] == batch_id
    assert item["file_count"] == 3
    assert "filenames" in item
    assert item["source"] == "Private Bot (deploy)"
    assert item["reason"] == "ZTP Files frontend deploy"
    assert "expires_at" in item
    assert "created_at" in item


def test_dynamodb_not_written_on_s3_failure(mocked_aws):
    """On S3 failure, rollback: no DynamoDB record should be written."""
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = Exception("S3 down")

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-19", _valid_args()
        )
    body = _body(result)
    assert body["status"] == "error"

    # Scan table — should be empty (no audit record written)
    scan = tbl.scan()
    assert scan["Count"] == 0


# ---------------------------------------------------------------------------
# Test 8: S3 generate failure → rollback with partial result
# ---------------------------------------------------------------------------


def test_s3_failure_returns_partial_error(mocked_aws):
    """When the second file fails, partial failure dict with succeeded/failed lists."""
    tbl, mcp_presigned = mocked_aws
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise Exception("S3 error on second file")
        return "https://presigned"

    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = side_effect

    files = _make_files(3)
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-20", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "error"
    assert "succeeded" in body
    assert "failed" in body
    # files 0 and 2 succeeded, file 1 failed
    assert len(body["succeeded"]) == 2
    assert len(body["failed"]) == 1


def test_all_files_fail_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = Exception("total S3 failure")

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-21", _valid_args()
        )
    body = _body(result)
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# Test 9: Rate limit exceeded
# ---------------------------------------------------------------------------


def test_rate_limit_exceeded_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    from rate_limit import RateLimitExceeded

    with patch(
        "mcp_presigned.check_rate_limit",
        side_effect=RateLimitExceeded("Rate limit exceeded"),
    ):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-22", _valid_args()
        )
    body = _body(result)
    assert body["status"] == "error"
    assert "Rate limit" in body["error"]


def test_pending_limit_exceeded_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    from rate_limit import PendingLimitExceeded

    with patch(
        "mcp_presigned.check_rate_limit",
        side_effect=PendingLimitExceeded("Too many pending"),
    ):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-23", _valid_args()
        )
    body = _body(result)
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# Test 10: All s3_keys share the same batch_id prefix
# ---------------------------------------------------------------------------


def test_all_s3_keys_share_batch_id_prefix(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [
        {"filename": "index.html", "content_type": "text/html"},
        {"filename": "assets/main.js", "content_type": "application/javascript"},
        {"filename": "assets/main.css", "content_type": "text/css"},
    ]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-24", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    batch_id = body["batch_id"]

    for f in body["files"]:
        # Each key: {date}/{batch_id}/{filename}
        assert batch_id in f["s3_key"]
        assert f["s3_uri"].startswith(f"s3://bouncer-uploads-190825685292/")


def test_batch_id_is_shared_across_all_files(mocked_aws):
    """Verify that batch_id segment is the same for every s3_key in the result."""
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-25", _valid_args(files=_make_files(5))
        )
    body = _body(result)
    batch_id = body["batch_id"]
    for f in body["files"]:
        parts = f["s3_key"].split("/")
        # s3_key format: {date}/{batch_id}/{filename...}
        assert parts[1] == batch_id


# ---------------------------------------------------------------------------
# Extra: account override, default expires_in
# ---------------------------------------------------------------------------


def test_custom_account_used_in_bucket(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3_ok()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-b-26", _valid_args(account="992382394211")
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert "992382394211" in body["bucket"]
    for f in body["files"]:
        assert "992382394211" in f["s3_uri"]


def test_default_expires_in_applied(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://presigned"
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        mcp_presigned.mcp_tool_request_presigned_batch("req-b-27", _valid_args())
    # Default expires_in = 900
    call_kwargs = mock_s3.generate_presigned_url.call_args[1]
    assert call_kwargs.get("ExpiresIn") == 900


# ---------------------------------------------------------------------------
# Shared helper unit tests (pure logic, no AWS)
# ---------------------------------------------------------------------------


def test_parse_common_presigned_params_valid(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    account_id, expires_in, err = mcp_presigned._parse_common_presigned_params(
        "r", {"account": "111111111111", "expires_in": 600}
    )
    assert err is None
    assert account_id == "111111111111"
    assert expires_in == 600


def test_parse_common_presigned_params_invalid_expires(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    _, _, err = mcp_presigned._parse_common_presigned_params(
        "r", {"expires_in": "bad"}
    )
    assert err is not None
    body = _body(err)
    assert body["status"] == "error"


def test_generate_presigned_url_for_file_success(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://url"
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        url, err = mcp_presigned._generate_presigned_url_for_file(
            "my-bucket", "key/file.txt", "text/plain", 900
        )
    assert err is None
    assert url == "https://url"


def test_generate_presigned_url_for_file_failure(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = Exception("boom")
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        url, err = mcp_presigned._generate_presigned_url_for_file(
            "my-bucket", "key/file.txt", "text/plain", 900
        )
    assert url is None
    assert err is not None
    assert "boom" in err


def test_check_rate_limit_for_source_no_source(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    # Empty source should not raise or call check_rate_limit
    result = mcp_presigned._check_rate_limit_for_source("r", "")
    assert result is None
