"""
Regression tests for bouncer_confirm_upload (Approach C).

Coverage:
  1. All files exist → verified=true, missing=[]
  2. Some files missing → verified=false, missing list populated
  3. All files missing → verified=false
  4. Over 50 files → rejected (error)
  5. Exactly 50 files → accepted (boundary)
  6. Empty files array → rejected
  7. Missing batch_id → rejected
  8. Missing s3_key in file entry → rejected
  9. files[N] not an object → rejected
 10. DynamoDB record written with correct pk and fields (verified=true)
 11. DynamoDB record written with correct fields (verified=false)
 12. S3 ClientError on list → error returned
 13. S3 generic exception on list → error returned
 14. Keys under different date prefix → still matched correctly
 15. batch_prefix derived from first key containing batch_id
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
    "mcp_confirm", "mcp_presigned", "db", "constants", "utils", "accounts",
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

ACCOUNT_ID = "190825685292"
BUCKET_NAME = f"bouncer-uploads-{ACCOUNT_ID}"
TABLE_NAME = "clawdbot-approval-requests"
BATCH_ID = "batch-db31d35b7c1e"
DATE_STR = "2026-02-25"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("STAGING_BUCKET", BUCKET_NAME)
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("ACCOUNTS_TABLE_NAME", "bouncer-accounts")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("APPROVED_CHAT_ID", "12345")
    monkeypatch.setenv("REQUEST_SECRET", "test-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture()
def mocked_aws(aws_env):
    """mock_aws context with DynamoDB table + S3 bucket pre-created."""
    with mock_aws():
        _reload_src()

        # Create DynamoDB table
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Create S3 bucket
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET_NAME)

        yield {
            "s3": s3,
            "dynamodb": dynamodb,
            "table": dynamodb.Table(TABLE_NAME),
        }


def _put_object(s3_client, key: str, body: bytes = b"content"):
    """Helper: upload a fake object to staging bucket."""
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=body)


def _make_key(filename: str) -> str:
    return f"{DATE_STR}/{BATCH_ID}/{filename}"


def _call_confirm(mocked_aws, batch_id=BATCH_ID, files=None):
    """Import and call handle_confirm_upload inside mocked context."""
    import importlib
    import mcp_confirm as mc
    importlib.reload(mc)

    params = {
        "_req_id": "req-test-001",
        "batch_id": batch_id,
        "files": files or [],
    }
    return mc.handle_confirm_upload(params)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_files_exist_verified_true(mocked_aws):
    """All requested files exist → verified=true, missing=[]."""
    s3 = mocked_aws["s3"]
    keys = [_make_key("index.html"), _make_key("assets/main.js")]
    for k in keys:
        _put_object(s3, k)

    result = _call_confirm(
        mocked_aws,
        files=[{"s3_key": k} for k in keys],
    )
    body = json.loads(result["body"])
    content = json.loads(body["result"]["content"][0]["text"])

    assert content["verified"] is True
    assert content["missing"] == []
    assert len(content["results"]) == 2
    for r in content["results"]:
        assert r["exists"] is True


def test_missing_files_verified_false(mocked_aws):
    """Some files missing → verified=false, missing list contains them."""
    s3 = mocked_aws["s3"]
    present_key = _make_key("index.html")
    missing_key = _make_key("assets/main.js")
    _put_object(s3, present_key)
    # missing_key intentionally not uploaded

    result = _call_confirm(
        mocked_aws,
        files=[{"s3_key": present_key}, {"s3_key": missing_key}],
    )
    body = json.loads(result["body"])
    content = json.loads(body["result"]["content"][0]["text"])

    assert content["verified"] is False
    assert missing_key in content["missing"]
    assert present_key not in content["missing"]

    results_map = {r["s3_key"]: r for r in content["results"]}
    assert results_map[present_key]["exists"] is True
    assert results_map[missing_key]["exists"] is False


def test_all_files_missing_verified_false(mocked_aws):
    """No files uploaded → verified=false, all keys in missing."""
    keys = [_make_key("index.html"), _make_key("assets/app.js")]

    result = _call_confirm(
        mocked_aws,
        files=[{"s3_key": k} for k in keys],
    )
    body = json.loads(result["body"])
    content = json.loads(body["result"]["content"][0]["text"])

    assert content["verified"] is False
    assert set(content["missing"]) == set(keys)


def test_over_50_files_rejected(mocked_aws):
    """51 files → error, max is 50."""
    files = [{"s3_key": _make_key(f"file_{i}.js")} for i in range(51)]

    result = _call_confirm(mocked_aws, files=files)
    body = json.loads(result["body"])
    assert body["result"]["isError"] is True
    error_text = json.loads(body["result"]["content"][0]["text"])
    assert "50" in error_text["error"]


def test_exactly_50_files_accepted(mocked_aws):
    """50 files → accepted (boundary check), all missing but no error."""
    files = [{"s3_key": _make_key(f"file_{i}.js")} for i in range(50)]

    result = _call_confirm(mocked_aws, files=files)
    body = json.loads(result["body"])
    assert "isError" not in body["result"] or body["result"].get("isError") is False
    content = json.loads(body["result"]["content"][0]["text"])
    assert content["batch_id"] == BATCH_ID
    assert len(content["results"]) == 50


def test_empty_files_array_rejected(mocked_aws):
    """Empty files array → error."""
    result = _call_confirm(mocked_aws, files=[])
    body = json.loads(result["body"])
    assert body["result"]["isError"] is True


def test_missing_batch_id_rejected(mocked_aws):
    """Missing batch_id → error."""
    import mcp_confirm as mc
    import importlib
    importlib.reload(mc)
    result = mc.handle_confirm_upload({
        "_req_id": "req-001",
        "files": [{"s3_key": _make_key("index.html")}],
        # batch_id omitted
    })
    body = json.loads(result["body"])
    assert body["result"]["isError"] is True
    error_text = json.loads(body["result"]["content"][0]["text"])
    assert "batch_id" in error_text["error"]


def test_missing_s3_key_in_file_entry_rejected(mocked_aws):
    """files[0] missing s3_key → error."""
    result = _call_confirm(mocked_aws, files=[{"not_s3_key": "something"}])
    body = json.loads(result["body"])
    assert body["result"]["isError"] is True
    error_text = json.loads(body["result"]["content"][0]["text"])
    assert "s3_key" in error_text["error"]


def test_files_entry_not_object_rejected(mocked_aws):
    """files[0] is a string, not an object → error."""
    result = _call_confirm(mocked_aws, files=["not-an-object"])
    body = json.loads(result["body"])
    assert body["result"]["isError"] is True


def test_dynamodb_record_written_verified_true(mocked_aws):
    """DynamoDB record written with pk=CONFIRM#{batch_id} and verified=true."""
    s3 = mocked_aws["s3"]
    table = mocked_aws["table"]
    key = _make_key("index.html")
    _put_object(s3, key)

    _call_confirm(mocked_aws, files=[{"s3_key": key}])

    # Read the written record
    record = table.get_item(Key={"request_id": f"CONFIRM#{BATCH_ID}"}).get("Item")
    assert record is not None
    assert record["verified"] is True
    assert record["batch_id"] == BATCH_ID
    assert record["request_type"] == "CONFIRM"
    assert record["missing"] == []
    assert "ttl" in record
    assert record["ttl"] > int(__import__("time").time())


def test_dynamodb_record_written_verified_false(mocked_aws):
    """DynamoDB record written with verified=false and correct missing list."""
    table = mocked_aws["table"]
    missing_key = _make_key("missing_file.js")

    _call_confirm(mocked_aws, files=[{"s3_key": missing_key}])

    record = table.get_item(Key={"request_id": f"CONFIRM#{BATCH_ID}"}).get("Item")
    assert record is not None
    assert record["verified"] is False
    assert missing_key in record["missing"]


def test_s3_client_error_returns_error(mocked_aws):
    """S3 ClientError from list_objects_v2 → error result."""
    import importlib
    import mcp_confirm as mc
    importlib.reload(mc)

    client_error = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"}},
        "ListObjectsV2",
    )

    with patch.object(
        boto3.client("s3").__class__,
        "list_objects_v2",
        side_effect=client_error,
    ):
        # We need to patch at module level
        with patch("mcp_confirm.boto3") as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3
            mock_s3.list_objects_v2.side_effect = client_error

            result = mc.handle_confirm_upload({
                "_req_id": "req-001",
                "batch_id": BATCH_ID,
                "files": [{"s3_key": _make_key("index.html")}],
            })

    body = json.loads(result["body"])
    assert body["result"]["isError"] is True
    error_text = json.loads(body["result"]["content"][0]["text"])
    assert "S3 error" in error_text["error"]


def test_s3_generic_exception_returns_error(mocked_aws):
    """Generic S3 exception → error result."""
    import importlib
    import mcp_confirm as mc
    importlib.reload(mc)

    with patch("mcp_confirm.boto3") as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        mock_s3.list_objects_v2.side_effect = RuntimeError("connection reset")

        result = mc.handle_confirm_upload({
            "_req_id": "req-001",
            "batch_id": BATCH_ID,
            "files": [{"s3_key": _make_key("index.html")}],
        })

    body = json.loads(result["body"])
    assert body["result"]["isError"] is True
    error_text = json.loads(body["result"]["content"][0]["text"])
    assert "Failed to list objects" in error_text["error"]


def test_keys_with_different_date_prefix_matched(mocked_aws):
    """Keys under a different date prefix are matched if batch_id is in path."""
    s3 = mocked_aws["s3"]
    alt_key = f"2026-03-01/{BATCH_ID}/index.html"
    _put_object(s3, alt_key)

    result = _call_confirm(mocked_aws, files=[{"s3_key": alt_key}])
    body = json.loads(result["body"])
    content = json.loads(body["result"]["content"][0]["text"])

    assert content["verified"] is True
    assert content["missing"] == []


def test_batch_prefix_derived_from_batch_id(mocked_aws):
    """_derive_batch_prefix returns correct prefix for standard key format."""
    import importlib
    import mcp_confirm as mc
    importlib.reload(mc)

    key = f"{DATE_STR}/{BATCH_ID}/index.html"
    prefix = mc._derive_batch_prefix(BATCH_ID, [key])
    assert prefix == f"{DATE_STR}/{BATCH_ID}/"


def test_batch_prefix_fallback_when_no_keys(mocked_aws):
    """_derive_batch_prefix falls back to batch_id/ when keys list is empty."""
    import importlib
    import mcp_confirm as mc
    importlib.reload(mc)

    prefix = mc._derive_batch_prefix(BATCH_ID, [])
    assert prefix == f"{BATCH_ID}/"


def test_invalid_batch_id_format_rejected(mocked_aws):
    """batch_id not matching batch-{12 hex chars} → error."""
    for bad_id in ["invalid", "batch-123", "batch-GGGGGGGGGGGG", "../etc/passwd", ""]:
        result = _call_confirm(
            mocked_aws,
            batch_id=bad_id,
            files=[{"s3_key": _make_key("index.html")}],
        )
        body = json.loads(result["body"])
        assert body["result"]["isError"] is True, f"Expected error for batch_id={bad_id!r}"


def test_valid_batch_id_format_accepted(mocked_aws):
    """Valid batch_id format (batch-{12 hex chars}) passes validation."""
    s3 = mocked_aws["s3"]
    key = _make_key("index.html")
    _put_object(s3, key)

    result = _call_confirm(
        mocked_aws,
        batch_id=BATCH_ID,  # "batch-db31d35b7c1e" — valid
        files=[{"s3_key": key}],
    )
    body = json.loads(result["body"])
    assert "isError" not in body["result"] or body["result"].get("isError") is False
