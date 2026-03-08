"""
Tests for sprint9-003 Phase A: mcp_deploy_frontend

Covers:
  - Input validation (index.html required, blocked extension, size limit, base64 check)
  - S3 staging (put_object called for each file)
  - DDB record written with correct fields
  - Notification called with correct args
  - Response shape for pending_approval
  - Rollback on S3 staging failure
  - Rollback on Telegram notification failure
"""
import base64
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(content: str) -> str:
    return base64.b64encode(content.encode()).decode()


def _make_files(extra=None):
    """Return a minimal valid files list with index.html."""
    files = [
        {"filename": "index.html", "content": _b64("<html/>"), "content_type": "text/html"},
    ]
    if extra:
        files.extend(extra)
    return files


def _call(arguments: dict):
    from mcp_deploy_frontend import mcp_tool_deploy_frontend
    return mcp_tool_deploy_frontend("req-test", arguments)


def _parse(result: dict) -> dict:
    body = json.loads(result["body"])
    text = body["result"]["content"][0]["text"]
    return json.loads(text)


def _is_error(result: dict) -> bool:
    """Check if the result is an error (isError lives inside the body.result)."""
    try:
        body = json.loads(result["body"])
        return bool(body.get("result", {}).get("isError"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

# ZTP Files config for tests (was hardcoded; now needs DDB mock)
_ZTP_FRONTEND_CONFIG = {
    'frontend_bucket': 'ztp-files-dev-frontendbucket-nvvimv31xp3v',
    'distribution_id': 'E176PW0SA5JF29',
    'region': 'us-east-1',
    'deploy_role_arn': 'arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role',
}


class TestValidation:
    @pytest.fixture(autouse=True)
    def mock_ztp_config(self):
        """Sprint 18: _PROJECT_CONFIG removed; patch DDB lookup for validation tests.
        Returns config only for 'ztp-files'; None for all others."""
        def _ddb_side_effect(project_id):
            return _ZTP_FRONTEND_CONFIG if project_id == 'ztp-files' else None
        with patch("mcp_deploy_frontend._get_frontend_config", side_effect=_ddb_side_effect):
            yield

    def test_missing_project(self):
        r = _call({"files": _make_files(), "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "project" in body["error"]

    def test_unknown_project(self):
        r = _call({"project": "not-a-project", "files": _make_files(), "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "Unknown project" in body["error"]
        assert "available_projects" in body

    def test_missing_index_html(self):
        files = [{"filename": "assets/foo.js", "content": _b64("console.log(1)"), "content_type": "application/javascript"}]
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "index.html" in body["error"]

    def test_blocked_extension_exe(self):
        files = _make_files([{"filename": "malware.exe", "content": _b64("MZ"), "content_type": "application/octet-stream"}])
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "blocked extension" in body["error"]

    def test_blocked_extension_sh(self):
        files = _make_files([{"filename": "run.sh", "content": _b64("#!/bin/sh"), "content_type": "text/x-sh"}])
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "blocked extension" in body["error"]

    def test_invalid_base64(self):
        files = [{"filename": "index.html", "content": "!!!notbase64!!!", "content_type": "text/html"}]
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        # Either "invalid base64" or padding error message
        assert "base64" in body["error"].lower() or "invalid" in body["error"].lower()

    def test_empty_files(self):
        r = _call({"project": "ztp-files", "files": [], "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "files" in body["error"].lower()

    def test_file_too_large(self):
        from mcp_deploy_frontend import MAX_FILE_SIZE_BYTES
        big = b"x" * (MAX_FILE_SIZE_BYTES + 1)
        files = [{"filename": "index.html", "content": base64.b64encode(big).decode(), "content_type": "text/html"}]
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "too large" in body["error"]

    def test_duplicate_filename(self):
        files = [
            {"filename": "index.html", "content": _b64("<html/>"), "content_type": "text/html"},
            {"filename": "index.html", "content": _b64("<html/>"), "content_type": "text/html"},
        ]
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "Duplicate" in body["error"]

    def test_validate_files_path_traversal_rejected(self):
        files = [
            {"filename": "../etc/passwd", "content": _b64("malicious"), "content_type": "text/plain"},
        ]
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "path traversal" in body["error"].lower()

    def test_validate_files_absolute_path_rejected(self):
        files = [
            {"filename": "/etc/shadow", "content": _b64("malicious"), "content_type": "text/plain"},
        ]
        r = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})
        body = _parse(r)
        assert _is_error(r)
        assert "path traversal" in body["error"].lower()


# ---------------------------------------------------------------------------
# Cache-Control and Content-Type helpers
# ---------------------------------------------------------------------------

class TestHeaderHelpers:
    def test_cache_control_index(self):
        from mcp_deploy_frontend import _get_cache_control
        assert "no-store" in _get_cache_control("index.html")

    def test_cache_control_assets(self):
        from mcp_deploy_frontend import _get_cache_control
        assert "immutable" in _get_cache_control("assets/index-abc123.js")

    def test_cache_control_other(self):
        from mcp_deploy_frontend import _get_cache_control
        cc = _get_cache_control("favicon.ico")
        assert cc == "no-cache"

    def test_content_type_provided(self):
        from mcp_deploy_frontend import _get_content_type
        assert _get_content_type("foo.unknown", "application/json") == "application/json"

    def test_content_type_guessed(self):
        from mcp_deploy_frontend import _get_content_type
        ct = _get_content_type("index.html", None)
        assert "html" in ct

    def test_content_type_fallback(self):
        from mcp_deploy_frontend import _get_content_type
        ct = _get_content_type("weirdfile.xyzabc", None)
        assert ct == "application/octet-stream"

    def test_blocked_extension_py(self):
        from mcp_deploy_frontend import _has_blocked_extension
        assert _has_blocked_extension("evil.py")

    def test_allowed_extension_js(self):
        from mcp_deploy_frontend import _has_blocked_extension
        assert not _has_blocked_extension("bundle.js")


# ---------------------------------------------------------------------------
# S3 staging + DDB write + Notification (happy path)
# ---------------------------------------------------------------------------

class TestHappyPath:
    def _run_happy(self, extra_files=None):
        """Run tool with mocked S3, DDB, and Telegram. Returns (result, mocks)."""
        files = _make_files(extra_files)

        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_notif_result = MagicMock()
        mock_notif_result.ok = True
        mock_notif_result.message_id = 12345

        with patch("mcp_deploy_frontend.boto3") as mock_boto3, \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("mcp_deploy_frontend.send_deploy_frontend_notification", return_value=mock_notif_result) as mock_notif, \
             patch("notifications.post_notification_setup") as mock_pns:
            mock_boto3.client.return_value = mock_s3
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Sprint 9 deploy",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
            })
            return result, mock_s3, mock_table, mock_notif, mock_pns

    def test_returns_pending_approval(self):
        result, *_ = self._run_happy()
        body = _parse(result)
        assert body["status"] == "pending_approval"
        assert "request_id" in body
        assert body["file_count"] == 1

    def test_s3_put_called_for_each_file(self):
        extra = [{"filename": "assets/app.js", "content": _b64("console.log()"), "content_type": "application/javascript"}]
        result, mock_s3, *_ = self._run_happy(extra_files=extra)
        # 2 files = 2 put_object calls
        assert mock_s3.put_object.call_count == 2

    def test_s3_staging_key_prefix(self):
        result, mock_s3, *_ = self._run_happy()
        call_args = mock_s3.put_object.call_args_list[0]
        key = call_args[1].get("Key") or call_args[0][1]
        # key should start with "pending/"
        assert "pending/" in key

    def test_ddb_put_item_called(self):
        result, _, mock_table, *_ = self._run_happy()
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["action"] == "deploy_frontend"
        assert item["status"] == "pending_approval"
        assert item["project"] == "ztp-files"
        assert "files" in item
        assert item["file_count"] == 1

    def test_ddb_item_has_frontend_bucket(self):
        result, _, mock_table, *_ = self._run_happy()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["frontend_bucket"] == "ztp-files-dev-frontendbucket-nvvimv31xp3v"
        assert item["distribution_id"] == "E176PW0SA5JF29"

    def test_ddb_files_manifest_is_json_string(self):
        result, _, mock_table, *_ = self._run_happy()
        item = mock_table.put_item.call_args[1]["Item"]
        manifest = json.loads(item["files"])
        assert isinstance(manifest, list)
        assert len(manifest) == 1
        f0 = manifest[0]
        assert f0["filename"] == "index.html"
        assert "s3_key" in f0
        assert "cache_control" in f0
        assert "content_type" in f0
        assert "no-store" in f0["cache_control"]

    def test_notification_called_with_correct_args(self):
        result, _, _, mock_notif, _ = self._run_happy()
        mock_notif.assert_called_once()
        kwargs = mock_notif.call_args[1]
        assert kwargs["project"] == "ztp-files"
        assert kwargs["reason"] == "Sprint 9 deploy"
        assert kwargs["source"] == "Private Bot"
        assert "frontend_bucket" in kwargs["target_info"]

    def test_post_notification_setup_called(self):
        result, _, _, _, mock_pns = self._run_happy()
        mock_pns.assert_called_once()

    def test_assets_cache_control_immutable(self):
        extra = [{"filename": "assets/app-hash.css", "content": _b64("body{}"), "content_type": "text/css"}]
        result, _, mock_table, *_ = self._run_happy(extra_files=extra)
        item = mock_table.put_item.call_args[1]["Item"]
        manifest = json.loads(item["files"])
        css_entry = next(f for f in manifest if "css" in f["filename"])
        assert "immutable" in css_entry["cache_control"]


# ---------------------------------------------------------------------------
# Rollback tests
# ---------------------------------------------------------------------------

class TestRollback:
    def test_s3_staging_failure_returns_error(self):
        files = _make_files()
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = Exception("S3 error")
        mock_table = MagicMock()

        with patch("mcp_deploy_frontend.boto3") as mock_boto3, \
             patch("mcp_deploy_frontend.table", mock_table):
            mock_boto3.client.return_value = mock_s3
            result = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})

        body = _parse(result)
        assert _is_error(result)
        assert "S3" in body["error"] or "stage" in body["error"].lower()
        # DDB should NOT have been written
        mock_table.put_item.assert_not_called()

    def test_telegram_failure_rolls_back_ddb(self):
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_notif_result = MagicMock()
        mock_notif_result.ok = False
        mock_notif_result.message_id = None

        with patch("mcp_deploy_frontend.boto3") as mock_boto3, \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend.send_deploy_frontend_notification", return_value=mock_notif_result):
            mock_boto3.client.return_value = mock_s3
            result = _call({"project": "ztp-files", "files": files, "source": "bot", "trust_scope": "ts"})

        body = _parse(result)
        assert _is_error(result)
        assert "Telegram" in body["error"]
        # DDB cleanup should have been called
        mock_table.delete_item.assert_called_once()


# ---------------------------------------------------------------------------
# Notification function
# ---------------------------------------------------------------------------

class TestSendDeployFrontendNotification:
    def test_notification_function_ok(self):
        from notifications import send_deploy_frontend_notification

        files_summary = [
            {"filename": "index.html", "size": 1024, "cache_control": "no-cache, no-store, must-revalidate", "content_type": "text/html"},
            {"filename": "assets/app.js", "size": 51200, "cache_control": "max-age=31536000, immutable", "content_type": "application/javascript"},
        ]
        target_info = {
            "frontend_bucket": "ztp-files-dev-frontendbucket-nvvimv31xp3v",
            "distribution_id": "E176PW0SA5JF29",
            "region": "us-east-1",
        }

        mock_result = {"ok": True, "result": {"message_id": 999}}
        with patch("notifications._send_message", return_value=mock_result) as mock_send:
            result = send_deploy_frontend_notification(
                request_id="req-abc",
                files_summary=files_summary,
                target_info=target_info,
                project="ztp-files",
                reason="Test deploy",
                source="Private Bot",
            )

        assert result.ok is True
        assert result.message_id == 999
        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][0]
        assert "ztp-files" in msg_text
        assert "index.html" in msg_text
        assert "req-abc" in msg_text

    def test_notification_function_telegram_failure(self):
        from notifications import send_deploy_frontend_notification

        files_summary = [{"filename": "index.html", "size": 100, "cache_control": "no-cache", "content_type": "text/html"}]
        target_info = {"frontend_bucket": "bucket", "distribution_id": "dist", "region": "us-east-1"}

        with patch("notifications._send_message", return_value={"ok": False}):
            result = send_deploy_frontend_notification("req-x", files_summary, target_info)

        assert result.ok is False
        assert result.message_id is None

    def test_notification_function_exception(self):
        from notifications import send_deploy_frontend_notification

        files_summary = [{"filename": "index.html", "size": 100, "cache_control": "no-cache", "content_type": "text/html"}]
        target_info = {"frontend_bucket": "b", "distribution_id": "d", "region": "us-east-1"}

        with patch("notifications._send_message", side_effect=RuntimeError("boom")):
            result = send_deploy_frontend_notification("req-y", files_summary, target_info)

        assert result.ok is False


# ---------------------------------------------------------------------------
# Sprint 11-000: deploy_role_arn in DDB (Phase A)
# ---------------------------------------------------------------------------

class TestDeployRoleArnPhaseA:
    """Verify that deploy_role_arn is stored in the DDB pending record (Phase A)."""

    def _run_happy(self, extra_files=None):
        files = _make_files(extra_files)
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_notif_result = MagicMock()
        mock_notif_result.ok = True
        mock_notif_result.message_id = 12345

        with patch("mcp_deploy_frontend.boto3") as mock_boto3, \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("mcp_deploy_frontend.send_deploy_frontend_notification", return_value=mock_notif_result), \
             patch("notifications.post_notification_setup"):
            mock_boto3.client.return_value = mock_s3
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Sprint 11 deploy",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
            })
        return result, mock_table

    def test_ddb_item_contains_deploy_role_arn(self):
        """deploy_role_arn from _PROJECT_CONFIG must be present in the DDB item."""
        result, mock_table = self._run_happy()
        item = mock_table.put_item.call_args[1]["Item"]
        assert "deploy_role_arn" in item
        assert item["deploy_role_arn"] == "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role"

    def test_ddb_item_deploy_role_arn_is_none_when_not_configured(self):
        """When a project has no deploy_role_arn in DDB config, DDB item must have None (not absent)."""
        # Sprint 18: _PROJECT_CONFIG removed; use DDB mock with missing deploy_role_arn
        config_without_role = {
            "frontend_bucket": "test-bucket",
            "distribution_id": "TESTDIST123",
            "region": "us-east-1",
            "deploy_role_arn": None,  # explicitly None
        }
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_notif_result = MagicMock()
        mock_notif_result.ok = True
        mock_notif_result.message_id = 42

        with patch("mcp_deploy_frontend.boto3") as mock_boto3, \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=config_without_role), \
             patch("mcp_deploy_frontend.send_deploy_frontend_notification", return_value=mock_notif_result), \
             patch("notifications.post_notification_setup"):
            mock_boto3.client.return_value = mock_s3
            _call({
                "project": "test-project",
                "files": files,
                "reason": "Backward compat test",
                "source": "Bot",
                "trust_scope": "ts",
            })

        item = mock_table.put_item.call_args[1]["Item"]
        # Key must be present; value must be None (backward compat)
        assert "deploy_role_arn" in item
        assert item["deploy_role_arn"] is None
