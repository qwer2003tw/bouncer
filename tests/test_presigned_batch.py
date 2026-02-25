"""
Tests for mcp_tool_request_presigned_batch (Approach C - test-driven).

Coverage targets:
 1.  Happy path — 1 file
 2.  Happy path — 10 files
 3.  Happy path — 50 files (boundary)
 4.  Empty files array → error
 5.  51 files → error (over limit)
 6.  File missing filename → error
 7.  File missing content_type → error
 8.  expires_in < 60 → error
 9.  expires_in > 3600 → error
10.  expires_in = 60 → ok (boundary)
11.  expires_in = 3600 → ok (boundary)
12.  expires_in non-integer → error
13.  expires_in = 0 → error
14.  missing reason → error
15.  path traversal in filename → sanitized, status=ready
16.  Windows backslash → normalized
17.  null byte in filename → sanitized
18.  all s3_keys share same batch_id prefix
19.  DynamoDB audit record contains all filenames
20.  S3 generate_presigned_url fails on one file → error returned
21.  S3 ClientError → detailed error message
22.  rate limit exceeded → error
23.  duplicate filenames → both keys present (deduped)
24.  default expires_in = 900 when omitted
25.  custom account → bucket uses account id
26.  source is optional (omitted) — no crash
27.  file_count in response matches input
28.  bucket field in response is correct
29.  expires_at field present and is ISO string
30.  batch_id starts with "batch-"
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

# ---------------------------------------------------------------------------
# Bootstrap: set AWS env vars before any boto3 import happens inside src/
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


def _make_files(n: int) -> list:
    return [
        {"filename": f"assets/file_{i}.js", "content_type": "application/javascript"}
        for i in range(n)
    ]


def _valid_batch_args(**overrides) -> dict:
    base = {
        "files": _make_files(3),
        "reason": "deploy ZTP frontend",
        "source": "Private Bot (deploy)",
    }
    base.update(overrides)
    return base


def _mock_s3(n_files: int = 100) -> MagicMock:
    """Return a mock s3 client that returns unique presigned URLs."""
    mock = MagicMock()
    mock.generate_presigned_url.side_effect = [
        f"https://s3.amazonaws.com/fake/file_{i}" for i in range(n_files)
    ]
    return mock


def _body(result: dict) -> dict:
    """Unwrap HTTP envelope → JSON-RPC → inner payload dict."""
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


# ===========================================================================
# 1. Happy path — 1 file
# ===========================================================================


def test_happy_path_single_file(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b1",
            _valid_batch_args(files=[{"filename": "index.html", "content_type": "text/html"}]),
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 1
    assert len(body["files"]) == 1
    f = body["files"][0]
    assert f["filename"] == "index.html"
    assert f["method"] == "PUT"
    assert f["headers"]["Content-Type"] == "text/html"
    assert "presigned_url" in f
    assert "s3_key" in f
    assert "s3_uri" in f


# ===========================================================================
# 2. Happy path — 10 files
# ===========================================================================


def test_happy_path_10_files(mocked_aws):
    tbl, mp = mocked_aws
    files = _make_files(10)
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(10)):
        result = mp.mcp_tool_request_presigned_batch("req-b2", _valid_batch_args(files=files))
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 10
    assert len(body["files"]) == 10


# ===========================================================================
# 3. Happy path — 50 files (upper boundary)
# ===========================================================================


def test_happy_path_50_files(mocked_aws):
    tbl, mp = mocked_aws
    files = _make_files(50)
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(50)):
        result = mp.mcp_tool_request_presigned_batch("req-b3", _valid_batch_args(files=files))
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 50
    assert len(body["files"]) == 50


# ===========================================================================
# 4. Empty files array → error
# ===========================================================================


def test_empty_files_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch("req-b4", _valid_batch_args(files=[]))
    body = _body(result)
    assert body["status"] == "error"
    assert "non-empty" in body["error"] or "files" in body["error"]


# ===========================================================================
# 5. 51 files → error (over limit)
# ===========================================================================


def test_51_files_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch("req-b5", _valid_batch_args(files=_make_files(51)))
    body = _body(result)
    assert body["status"] == "error"
    assert "50" in body["error"]


# ===========================================================================
# 6. File missing filename → error
# ===========================================================================


def test_file_missing_filename_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    files = [{"content_type": "text/html"}]
    result = mp.mcp_tool_request_presigned_batch("req-b6", _valid_batch_args(files=files))
    body = _body(result)
    assert body["status"] == "error"
    assert "filename" in body["error"]


# ===========================================================================
# 7. File missing content_type → error
# ===========================================================================


def test_file_missing_content_type_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    files = [{"filename": "index.html"}]
    result = mp.mcp_tool_request_presigned_batch("req-b7", _valid_batch_args(files=files))
    body = _body(result)
    assert body["status"] == "error"
    assert "content_type" in body["error"]


# ===========================================================================
# 8. expires_in < 60 → error
# ===========================================================================


def test_expires_in_below_min_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch("req-b8", _valid_batch_args(expires_in=30))
    body = _body(result)
    assert body["status"] == "error"
    assert "60" in body["error"]


# ===========================================================================
# 9. expires_in > 3600 → error
# ===========================================================================


def test_expires_in_exceeds_max_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch("req-b9", _valid_batch_args(expires_in=3601))
    body = _body(result)
    assert body["status"] == "error"
    assert "3600" in body["error"]


# ===========================================================================
# 10. expires_in = 60 → ok (minimum boundary)
# ===========================================================================


def test_expires_in_at_minimum_is_ok(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b10", _valid_batch_args(expires_in=60))
    body = _body(result)
    assert body["status"] == "ready"


# ===========================================================================
# 11. expires_in = 3600 → ok (maximum boundary)
# ===========================================================================


def test_expires_in_at_maximum_is_ok(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b11", _valid_batch_args(expires_in=3600))
    body = _body(result)
    assert body["status"] == "ready"


# ===========================================================================
# 12. expires_in non-integer → error
# ===========================================================================


def test_expires_in_non_integer_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch(
        "req-b12", _valid_batch_args(expires_in="not-a-number")
    )
    body = _body(result)
    assert body["status"] == "error"
    assert "integer" in body["error"]


# ===========================================================================
# 13. expires_in = 0 → error
# ===========================================================================


def test_expires_in_zero_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch("req-b13", _valid_batch_args(expires_in=0))
    body = _body(result)
    assert body["status"] == "error"


# ===========================================================================
# 14. missing reason → error
# ===========================================================================


def test_missing_reason_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    args = _valid_batch_args()
    del args["reason"]
    result = mp.mcp_tool_request_presigned_batch("req-b14", args)
    body = _body(result)
    assert body["status"] == "error"
    assert "reason" in body["error"]


# ===========================================================================
# 15. Path traversal → sanitized, status = ready
# ===========================================================================


def test_path_traversal_sanitized(mocked_aws):
    tbl, mp = mocked_aws
    files = [{"filename": "../../etc/passwd", "content_type": "text/plain"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b15", _valid_batch_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    key = body["files"][0]["s3_key"]
    assert ".." not in key


# ===========================================================================
# 16. Windows backslash → normalized
# ===========================================================================


def test_windows_backslash_normalized(mocked_aws):
    tbl, mp = mocked_aws
    files = [{"filename": r"assets\foo.js", "content_type": "application/javascript"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b16", _valid_batch_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    key = body["files"][0]["s3_key"]
    assert "\\" not in key


# ===========================================================================
# 17. Null byte in filename → sanitized
# ===========================================================================


def test_null_byte_in_filename_sanitized(mocked_aws):
    tbl, mp = mocked_aws
    files = [{"filename": "file\x00name.txt", "content_type": "text/plain"}]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b17", _valid_batch_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    key = body["files"][0]["s3_key"]
    assert "\x00" not in key


# ===========================================================================
# 18. All s3_keys share the same batch_id prefix
# ===========================================================================


def test_all_s3_keys_share_batch_id_prefix(mocked_aws):
    tbl, mp = mocked_aws
    files = _make_files(5)
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(5)):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b18", _valid_batch_args(files=files)
        )
    body = _body(result)
    batch_id = body["batch_id"]
    for f in body["files"]:
        assert batch_id in f["s3_key"], f"batch_id not in s3_key: {f['s3_key']}"


# ===========================================================================
# 19. DynamoDB audit record contains all filenames
# ===========================================================================


def test_dynamodb_audit_record_contains_all_filenames(mocked_aws):
    tbl, mp = mocked_aws
    filenames = ["index.html", "main.js", "style.css"]
    files = [{"filename": fn, "content_type": "text/plain"} for fn in filenames]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(3)):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b19", _valid_batch_args(files=files)
        )
    body = _body(result)
    assert body["status"] == "ready"
    batch_id = body["batch_id"]
    item = tbl.get_item(Key={"request_id": batch_id}).get("Item")
    assert item is not None
    assert item["action"] == "presigned_upload_batch"
    assert item["status"] == "urls_issued"
    recorded = item["filenames"]
    for fn in filenames:
        assert fn in recorded, f"{fn} not in audit filenames: {recorded}"
    assert item["file_count"] == 3


# ===========================================================================
# 20. S3 generate failure on a file → error
# ===========================================================================


def test_s3_generate_failure_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = Exception("S3 credentials expired")
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mp.mcp_tool_request_presigned_batch("req-b20", _valid_batch_args())
    body = _body(result)
    assert body["status"] == "error"
    assert "S3 credentials expired" in body["error"] or "Failed" in body["error"]


# ===========================================================================
# 21. S3 ClientError → detailed error message
# ===========================================================================


def test_s3_client_error_returns_detailed_message(mocked_aws):
    tbl, mp = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "GeneratePresignedUrl",
    )
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mp.mcp_tool_request_presigned_batch("req-b21", _valid_batch_args())
    body = _body(result)
    assert body["status"] == "error"
    assert "AccessDenied" in body["error"] or "Access Denied" in body["error"]


# ===========================================================================
# 22. Rate limit exceeded → error
# ===========================================================================


def test_rate_limit_exceeded_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    from rate_limit import RateLimitExceeded

    with patch("mcp_presigned.check_rate_limit", side_effect=RateLimitExceeded("Rate limit exceeded")):
        result = mp.mcp_tool_request_presigned_batch("req-b22", _valid_batch_args())
    body = _body(result)
    assert body["status"] == "error"
    assert "Rate limit" in body["error"]


# ===========================================================================
# 23. Duplicate filenames → both keys present (deduped)
# ===========================================================================


def test_duplicate_filenames_deduped(mocked_aws):
    tbl, mp = mocked_aws
    files = [
        {"filename": "index.html", "content_type": "text/html"},
        {"filename": "index.html", "content_type": "text/html"},
    ]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(2)):
        result = mp.mcp_tool_request_presigned_batch("req-b23", _valid_batch_args(files=files))
    body = _body(result)
    assert body["status"] == "ready"
    assert body["file_count"] == 2
    keys = [f["s3_key"] for f in body["files"]]
    # Both s3_keys must be unique (deduplication happened)
    assert len(set(keys)) == 2, f"Duplicate s3_keys found: {keys}"


# ===========================================================================
# 24. Default expires_in = 900 when omitted
# ===========================================================================


def test_default_expires_in_is_900(mocked_aws):
    tbl, mp = mocked_aws
    mock_s3 = _mock_s3()
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        mp.mcp_tool_request_presigned_batch("req-b24", _valid_batch_args())
    # Verify ExpiresIn passed to boto3
    call_kwargs = mock_s3.generate_presigned_url.call_args[1]
    assert call_kwargs.get("ExpiresIn") == 900


# ===========================================================================
# 25. Custom account → bucket uses account id
# ===========================================================================


def test_custom_account_in_bucket(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch(
            "req-b25", _valid_batch_args(account="992382394211")
        )
    body = _body(result)
    assert body["status"] == "ready"
    assert "992382394211" in body["bucket"]
    for f in body["files"]:
        assert "992382394211" in f["s3_uri"]


# ===========================================================================
# 26. Source optional — omitted → no crash
# ===========================================================================


def test_source_optional_no_crash(mocked_aws):
    tbl, mp = mocked_aws
    args = _valid_batch_args()
    del args["source"]
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b26", args)
    body = _body(result)
    assert body["status"] == "ready"


# ===========================================================================
# 27. file_count in response matches input
# ===========================================================================


def test_file_count_matches_input(mocked_aws):
    tbl, mp = mocked_aws
    files = _make_files(7)
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(7)):
        result = mp.mcp_tool_request_presigned_batch("req-b27", _valid_batch_args(files=files))
    body = _body(result)
    assert body["file_count"] == 7
    assert len(body["files"]) == 7


# ===========================================================================
# 28. bucket field in response is correct
# ===========================================================================


def test_bucket_field_correct(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b28", _valid_batch_args())
    body = _body(result)
    assert body["bucket"] == "bouncer-uploads-190825685292"


# ===========================================================================
# 29. expires_at field present and is ISO string
# ===========================================================================


def test_expires_at_is_iso_string(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b29", _valid_batch_args())
    body = _body(result)
    assert "expires_at" in body
    ea = body["expires_at"]
    # Simple format check: YYYY-MM-DDTHH:MM:SSZ
    assert "T" in ea and ea.endswith("Z"), f"Unexpected expires_at format: {ea}"


# ===========================================================================
# 30. batch_id starts with "batch-"
# ===========================================================================


def test_batch_id_starts_with_batch_prefix(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b30", _valid_batch_args())
    body = _body(result)
    assert body["batch_id"].startswith("batch-"), f"batch_id = {body['batch_id']}"


# ===========================================================================
# Extra: files=None (not a list) → error
# ===========================================================================


def test_files_not_a_list_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch(
        "req-b31", _valid_batch_args(files="not-a-list")
    )
    body = _body(result)
    assert body["status"] == "error"


# ===========================================================================
# Extra: PendingLimitExceeded → error
# ===========================================================================


def test_pending_limit_exceeded_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    from rate_limit import PendingLimitExceeded

    with patch(
        "mcp_presigned.check_rate_limit",
        side_effect=PendingLimitExceeded("Pending limit exceeded"),
    ):
        result = mp.mcp_tool_request_presigned_batch("req-b32", _valid_batch_args())
    body = _body(result)
    assert body["status"] == "error"
    assert "Pending" in body["error"]


# ===========================================================================
# Extra: s3_uri format check
# ===========================================================================


def test_s3_uri_format(mocked_aws):
    tbl, mp = mocked_aws
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3()):
        result = mp.mcp_tool_request_presigned_batch("req-b33", _valid_batch_args())
    body = _body(result)
    for f in body["files"]:
        assert f["s3_uri"].startswith("s3://bouncer-uploads-190825685292/")


# ===========================================================================
# Extra: expires_in negative → error
# ===========================================================================


def test_expires_in_negative_returns_error(mocked_aws):
    tbl, mp = mocked_aws
    result = mp.mcp_tool_request_presigned_batch("req-b34", _valid_batch_args(expires_in=-1))
    body = _body(result)
    assert body["status"] == "error"


# ===========================================================================
# Extra: DynamoDB audit file_count field
# ===========================================================================


def test_dynamodb_audit_file_count(mocked_aws):
    tbl, mp = mocked_aws
    files = _make_files(5)
    with patch("mcp_presigned.boto3.client", return_value=_mock_s3(5)):
        result = mp.mcp_tool_request_presigned_batch("req-b35", _valid_batch_args(files=files))
    body = _body(result)
    batch_id = body["batch_id"]
    item = tbl.get_item(Key={"request_id": batch_id}).get("Item")
    assert item is not None
    assert int(item["file_count"]) == 5


# ===========================================================================
# Extra: second file S3 error (first succeeds, second fails)
# ===========================================================================


def test_s3_failure_on_second_file(mocked_aws):
    tbl, mp = mocked_aws
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.side_effect = [
        "https://s3.amazonaws.com/fake/first",
        Exception("Network timeout"),
    ]
    files = _make_files(2)
    with patch("mcp_presigned.boto3.client", return_value=mock_s3):
        result = mp.mcp_tool_request_presigned_batch("req-b36", _valid_batch_args(files=files))
    body = _body(result)
    assert body["status"] == "error"
