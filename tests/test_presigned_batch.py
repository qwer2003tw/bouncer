"""
Tests for mcp_tool_request_presigned_batch (Approach A - conservative).

Covers all 10 spec test requirements:
1. 正常路徑：多個檔案，回傳正確格式
2. 空 files array → error
3. 超過 50 個檔案 → error
4. 缺少必填參數（filename/content_type/reason/source）
5. expires_in 驗證（min/max）
6. filename sanitization（path traversal）
7. DynamoDB audit record 寫入
8. S3 generate 失敗
9. Rate limit exceeded
10. 所有 s3_key 共用同一個 batch_id prefix
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Environment setup (before any boto3 / src module imports)
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
    """Live mock_aws context with DynamoDB + S3 pre-created."""
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


def _valid_files():
    return [
        {"filename": "index.html", "content_type": "text/html"},
        {"filename": "assets/app.js", "content_type": "application/javascript"},
        {"filename": "assets/style.css", "content_type": "text/css"},
    ]


def _valid_args(**overrides):
    base = {
        "files": _valid_files(),
        "reason": "ZTP Files 前端部署",
        "source": "Private Bot (deploy)",
    }
    base.update(overrides)
    return base


def _body(result: dict) -> dict:
    """Extract the JSON payload from an MCP result dict."""
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


def _mock_s3(url="https://s3.amazonaws.com/presigned-fake"):
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = url
    return s3


# ---------------------------------------------------------------------------
# TEST 1: 正常路徑 - 多個檔案，回傳正確格式
# ---------------------------------------------------------------------------


def test_happy_path_returns_ready(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-1", _valid_args())

    body = _body(result)
    assert body["status"] == "ready"
    assert "batch_id" in body
    assert body["file_count"] == 3
    assert len(body["files"]) == 3
    assert "expires_at" in body
    assert "bucket" in body
    assert body["bucket"] == "bouncer-uploads-190825685292"


def test_happy_path_file_structure(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-2", _valid_args())

    body = _body(result)
    for file_entry in body["files"]:
        assert "filename" in file_entry
        assert "presigned_url" in file_entry
        assert "s3_key" in file_entry
        assert "s3_uri" in file_entry
        assert file_entry["method"] == "PUT"
        assert "headers" in file_entry
        assert "Content-Type" in file_entry["headers"]


def test_happy_path_s3_uri_uses_correct_bucket(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-3", _valid_args())

    body = _body(result)
    for file_entry in body["files"]:
        assert file_entry["s3_uri"].startswith("s3://bouncer-uploads-190825685292/")


# ---------------------------------------------------------------------------
# TEST 2: 空 files array → error
# ---------------------------------------------------------------------------


def test_empty_files_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-4", _valid_args(files=[]))
    body = _body(result)
    assert body["status"] == "error"
    assert "files" in body["error"].lower()


def test_missing_files_key_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["files"]
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-5", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "files" in body["error"].lower()


# ---------------------------------------------------------------------------
# TEST 3: 超過 50 個檔案 → error
# ---------------------------------------------------------------------------


def test_exceeds_max_files_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": f"file{i}.txt", "content_type": "text/plain"} for i in range(51)]
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-6", _valid_args(files=files))
    body = _body(result)
    assert body["status"] == "error"
    assert "50" in body["error"]


def test_exactly_50_files_is_ok(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": f"file{i}.txt", "content_type": "text/plain"} for i in range(50)]
    mock_s3 = _mock_s3()
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-7", _valid_args(files=files))
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 50


# ---------------------------------------------------------------------------
# TEST 4: 缺少必填參數
# ---------------------------------------------------------------------------


def test_missing_reason_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["reason"]
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-8", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "reason" in body["error"]


def test_missing_source_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    args = _valid_args()
    del args["source"]
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-9", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "source" in body["error"]


def test_file_missing_filename_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"content_type": "text/html"}]  # missing filename
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-10", _valid_args(files=files))
    body = _body(result)
    assert body["status"] == "error"
    assert "filename" in body["error"]


def test_file_missing_content_type_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "index.html"}]  # missing content_type
    result = mcp_presigned.mcp_tool_request_presigned_batch("req-11", _valid_args(files=files))
    body = _body(result)
    assert body["status"] == "error"
    assert "content_type" in body["error"]


# ---------------------------------------------------------------------------
# TEST 5: expires_in 驗證（min/max）
# ---------------------------------------------------------------------------


def test_expires_in_exceeds_max_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-12", _valid_args(expires_in=3601)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "3600" in body["error"]


def test_expires_in_below_minimum_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-13", _valid_args(expires_in=30)
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "60" in body["error"]


def test_expires_in_zero_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-14", _valid_args(expires_in=0)
    )
    body = _body(result)
    assert body["status"] == "error"


def test_expires_in_non_integer_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    result = mcp_presigned.mcp_tool_request_presigned_batch(
        "req-15", _valid_args(expires_in="not-a-number")
    )
    body = _body(result)
    assert body["status"] == "error"


def test_expires_in_at_min_is_ok(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-16", _valid_args(expires_in=60)
        )
    body = _body(result)
    assert body["status"] == "ready"


def test_expires_in_at_max_is_ok(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-17", _valid_args(expires_in=3600)
        )
    body = _body(result)
    assert body["status"] == "ready"


# ---------------------------------------------------------------------------
# TEST 6: filename sanitization（path traversal）
# ---------------------------------------------------------------------------


def test_path_traversal_in_file_removed(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "../../etc/passwd", "content_type": "text/plain"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-18", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    for file_entry in body["files"]:
        assert ".." not in file_entry["s3_key"]


def test_null_byte_in_filename_removed(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "file\x00name.txt", "content_type": "text/plain"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-19", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    for file_entry in body["files"]:
        assert "\x00" not in file_entry["s3_key"]


def test_subdir_structure_preserved(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "assets/chunk-abc.js", "content_type": "application/javascript"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-20", _valid_args(files=files)
        )
    body = _body(result)
    assert "assets/chunk-abc.js" in body["files"][0]["s3_key"]


# ---------------------------------------------------------------------------
# TEST 7: DynamoDB audit record 寫入
# ---------------------------------------------------------------------------


def test_dynamodb_batch_audit_record_written(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-21", _valid_args())

    body = _body(result)
    batch_id = body["batch_id"]

    item = tbl.get_item(Key={"request_id": batch_id}).get("Item")
    assert item is not None
    assert item["action"] == "presigned_upload_batch"
    assert item["status"] == "urls_issued"
    assert isinstance(item["filenames"], list)
    assert len(item["filenames"]) == 3
    assert item["file_count"] == 3
    assert item["source"] == "Private Bot (deploy)"
    assert item["reason"] == "ZTP Files 前端部署"
    assert "expires_at" in item
    assert "created_at" in item
    assert "ttl" in item


def test_dynamodb_audit_filenames_match_input(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-22", _valid_args())

    body = _body(result)
    batch_id = body["batch_id"]

    item = tbl.get_item(Key={"request_id": batch_id}).get("Item")
    assert "index.html" in item["filenames"]
    assert "assets/app.js" in item["filenames"]
    assert "assets/style.css" in item["filenames"]


# ---------------------------------------------------------------------------
# TEST 8: S3 generate 失敗
# ---------------------------------------------------------------------------


def test_s3_generate_failure_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = Exception("S3 credentials expired")

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-23", _valid_args())

    body = _body(result)
    assert body["status"] == "error"
    assert "S3 credentials expired" in body["error"] or "Failed" in body["error"]


def test_s3_client_error_returns_detailed_message(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    from botocore.exceptions import ClientError

    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "GeneratePresignedUrl",
    )

    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-24", _valid_args())

    body = _body(result)
    assert body["status"] == "error"
    assert "AccessDenied" in body["error"] or "Access Denied" in body["error"]


# ---------------------------------------------------------------------------
# TEST 9: Rate limit exceeded
# ---------------------------------------------------------------------------


def test_rate_limit_exceeded_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    from rate_limit import RateLimitExceeded

    with patch(
        "mcp_presigned.check_rate_limit",
        side_effect=RateLimitExceeded("Rate limit exceeded"),
    ):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-25", _valid_args())

    body = _body(result)
    assert body["status"] == "error"
    assert "Rate limit" in body["error"]


def test_pending_limit_exceeded_returns_error(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    from rate_limit import PendingLimitExceeded

    with patch(
        "mcp_presigned.check_rate_limit",
        side_effect=PendingLimitExceeded("Pending limit exceeded"),
    ):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-26", _valid_args())

    body = _body(result)
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# TEST 10: 所有 s3_key 共用同一個 batch_id prefix
# ---------------------------------------------------------------------------


def test_all_s3_keys_share_batch_id(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch("req-27", _valid_args())

    body = _body(result)
    batch_id = body["batch_id"]

    for file_entry in body["files"]:
        # s3_key format: {date}/{batch_id}/{filename}
        assert batch_id in file_entry["s3_key"], (
            f"Expected batch_id '{batch_id}' in s3_key '{file_entry['s3_key']}'"
        )


def test_batch_id_prefix_is_consistent_across_files(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [
        {"filename": f"file{i}.txt", "content_type": "text/plain"} for i in range(5)
    ]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-28", _valid_args(files=files)
        )

    body = _body(result)
    batch_id = body["batch_id"]
    # Extract the second path segment from each key: {date}/{batch_id}/{filename}
    prefixes = set()
    for file_entry in body["files"]:
        parts = file_entry["s3_key"].split("/")
        # parts[0] = date, parts[1] = batch_id
        prefixes.add(parts[1])

    assert len(prefixes) == 1, f"Expected single batch_id prefix, got {prefixes}"
    assert batch_id in prefixes


# ---------------------------------------------------------------------------
# Additional: custom account
# ---------------------------------------------------------------------------


def test_custom_account_used_in_bucket(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-29", _valid_args(account="992382394211")
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert "992382394211" in body["bucket"]
    for file_entry in body["files"]:
        assert "992382394211" in file_entry["s3_uri"]


# ---------------------------------------------------------------------------
# Additional: default expires_in
# ---------------------------------------------------------------------------


def test_default_expires_in_is_900(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    mock_s3 = _mock_s3()
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        mcp_presigned.mcp_tool_request_presigned_batch("req-30", _valid_args())

    # All calls to generate_presigned_url should have ExpiresIn=900
    for call in mock_s3.generate_presigned_url.call_args_list:
        assert call[1].get("ExpiresIn") == 900


# ---------------------------------------------------------------------------
# Additional: single file batch
# ---------------------------------------------------------------------------


def test_single_file_batch(mocked_aws):
    tbl, mcp_presigned = mocked_aws
    files = [{"filename": "index.html", "content_type": "text/html"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mcp_presigned.mcp_tool_request_presigned_batch(
            "req-31", _valid_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 1
    assert len(body["files"]) == 1
