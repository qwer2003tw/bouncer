"""
Tests for sprint29-001: Trust Session support in mcp_deploy_frontend

Covers:
  - Trust session approval → direct deployment execution
  - Trust session denial → falls back to manual approval flow
  - Project validation (deploy_role_arn presence)
  - CloudFront invalidation in trust flow
  - Audit logging with trust_bypass=True
  - Trust command count increment
"""
import base64
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock, call
from botocore.exceptions import ClientError

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


# ZTP Files config for tests
_ZTP_FRONTEND_CONFIG = {
    'frontend_bucket': 'ztp-files-dev-frontendbucket-nvvimv31xp3v',
    'distribution_id': 'E176PW0SA5JF29',
    'region': 'us-east-1',
    'deploy_role_arn': 'arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role',
}


# ---------------------------------------------------------------------------
# Trust Session Tests
# ---------------------------------------------------------------------------

class TestTrustSessionApproval:
    """Test trust session approval flow."""

    def test_trust_approved_executes_deployment(self):
        """When trust session is approved, deployment executes immediately."""
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_history = MagicMock()
        mock_cf = MagicMock()

        # Mock trust session
        trust_session = {
            "request_id": "trust-123",
            "expires_at": 9999999999,
            "trust_scope": "ts-ztp",
        }

        # Mock deployer_projects_table for validation
        mock_projects_table = MagicMock()
        mock_projects_table.get_item.return_value = {
            "Item": {
                "project_id": "ztp-files",
                "frontend_deploy_role_arn": _ZTP_FRONTEND_CONFIG["deploy_role_arn"],
            }
        }

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(True, trust_session, "trust approved")), \
             patch("mcp_deploy_frontend.deployer_projects_table", mock_projects_table), \
             patch("mcp_deploy_frontend.deployer_history_table", mock_history), \
             patch("aws_clients.get_cloudfront_client", return_value=mock_cf), \
             patch("trust.increment_trust_command_count", return_value=5), \
             patch("notifications.send_trust_auto_approve_notification"), \
             patch("utils.log_decision"), \
             patch("mcp_deploy_frontend.DEFAULT_ACCOUNT_ID", "190825685292"):
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Trust session deploy",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
                "account_id": "190825685292",
            })

        parsed = _parse(result)
        assert parsed["status"] in ["success", "partial_success"]
        assert parsed["deployed"] == 1
        assert parsed["failed"] == 0
        assert parsed["trust_session"] == "trust-123"
        assert "ztp-files" in parsed["frontend_bucket"]

        # Verify S3 staging call
        assert mock_s3.put_object.called
        staging_call = mock_s3.put_object.call_args
        assert "bouncer-uploads-190825685292" in str(staging_call)
        assert "pending/" in str(staging_call)

        # Verify S3 copy call
        assert mock_s3.copy_object.called
        copy_call = mock_s3.copy_object.call_args
        assert copy_call[1]["Bucket"] == _ZTP_FRONTEND_CONFIG["frontend_bucket"]
        assert copy_call[1]["Key"] == "index.html"

        # Verify CloudFront invalidation
        assert mock_cf.create_invalidation.called
        cf_call = mock_cf.create_invalidation.call_args
        assert cf_call[1]["DistributionId"] == _ZTP_FRONTEND_CONFIG["distribution_id"]

        # Verify deploy history written
        assert mock_history.put_item.called
        history_item = mock_history.put_item.call_args[1]["Item"]
        assert history_item["trust_bypass"] is True
        assert history_item["trust_scope"] == "ts-ztp"
        assert history_item["status"] == "completed"

    def test_trust_denied_falls_back_to_manual_approval(self):
        """When trust session is denied, falls back to manual approval flow."""
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_notif = MagicMock()
        mock_notif.ok = True
        mock_notif.message_id = 12345

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(False, None, "trust scope not found")), \
             patch("mcp_deploy_frontend.send_deploy_frontend_notification", return_value=mock_notif), \
             patch("notifications.post_notification_setup"):
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Manual approval needed",
                "source": "Private Bot",
                "trust_scope": "invalid-scope",
                "account_id": "190825685292",
            })

        parsed = _parse(result)
        # Should go through manual approval flow
        assert parsed["status"] == "pending_approval"
        assert "request_id" in parsed

        # Verify DDB pending record written
        assert mock_table.put_item.called
        ddb_item = mock_table.put_item.call_args[1]["Item"]
        assert ddb_item["status"] == "pending_approval"
        assert ddb_item["trust_scope"] == "invalid-scope"

    def test_trust_approved_but_no_deploy_role_arn_denies(self):
        """Trust session approved but project lacks deploy_role_arn → deny."""
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_notif = MagicMock()
        mock_notif.ok = True
        mock_notif.message_id = 99

        trust_session = {"request_id": "trust-456", "expires_at": 9999999999}

        # Mock deployer_projects_table returns item without deploy_role_arn
        mock_projects_table = MagicMock()
        mock_projects_table.get_item.return_value = {
            "Item": {
                "project_id": "ztp-files",
                # Missing frontend_deploy_role_arn
            }
        }

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(True, trust_session, "trust approved")), \
             patch("mcp_deploy_frontend.deployer_projects_table", mock_projects_table), \
             patch("mcp_deploy_frontend.send_deploy_frontend_notification", return_value=mock_notif), \
             patch("notifications.post_notification_setup"):
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Deploy should be denied",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
                "account_id": "190825685292",
            })

        parsed = _parse(result)
        # Trust check should fail, fall back to manual approval
        assert parsed["status"] == "pending_approval"
        assert mock_table.put_item.called

    def test_trust_approved_cloudfront_invalidation_failure(self):
        """Trust approved, files deployed, but CloudFront invalidation fails."""
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_history = MagicMock()
        mock_cf = MagicMock()
        mock_cf.create_invalidation.side_effect = ClientError({'Error': {'Code': 'TooManyInvalidationsInProgress', 'Message': 'CF API error'}}, 'CreateInvalidation')

        trust_session = {"request_id": "trust-789", "expires_at": 9999999999}

        mock_projects_table = MagicMock()
        mock_projects_table.get_item.return_value = {
            "Item": {
                "project_id": "ztp-files",
                "frontend_deploy_role_arn": _ZTP_FRONTEND_CONFIG["deploy_role_arn"],
            }
        }

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(True, trust_session, "trust approved")), \
             patch("mcp_deploy_frontend.deployer_projects_table", mock_projects_table), \
             patch("mcp_deploy_frontend.deployer_history_table", mock_history), \
             patch("aws_clients.get_cloudfront_client", return_value=mock_cf), \
             patch("trust.increment_trust_command_count", return_value=1), \
             patch("notifications.send_trust_auto_approve_notification"), \
             patch("utils.log_decision"):
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "CF failure test",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
                "account_id": "190825685292",
            })

        parsed = _parse(result)
        # Deployment should partially succeed (files deployed, CF failed)
        assert parsed["status"] == "partial_success"
        assert parsed["deployed"] == 1
        assert parsed["cloudfront_invalidation"] == "failed"

        # History should record CF failure
        history_item = mock_history.put_item.call_args[1]["Item"]
        assert history_item["cf_invalidation_failed"] is True
        assert history_item["status"] == "failed"

    def test_trust_approved_s3_staging_failure_rollback(self):
        """Trust approved but S3 staging fails → rollback and error."""
        files = _make_files([
            {"filename": "app.js", "content": _b64("console.log('hi')"), "content_type": "application/javascript"}
        ])
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = [None, ClientError({'Error': {'Code': 'ServiceUnavailable', 'Message': 'S3 staging error'}}, 'PutObject')]

        trust_session = {"request_id": "trust-999", "expires_at": 9999999999}

        mock_projects_table = MagicMock()
        mock_projects_table.get_item.return_value = {
            "Item": {
                "project_id": "ztp-files",
                "frontend_deploy_role_arn": _ZTP_FRONTEND_CONFIG["deploy_role_arn"],
            }
        }

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(True, trust_session, "trust approved")), \
             patch("mcp_deploy_frontend.deployer_projects_table", mock_projects_table):
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "S3 staging failure",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
                "account_id": "190825685292",
            })

        parsed = _parse(result)
        assert parsed["status"] == "error"
        assert "stage" in parsed["error"].lower() or "s3" in parsed["error"].lower()

        # Verify rollback: delete_object should be called for already-staged files
        assert mock_s3.delete_object.called

    def test_trust_approved_s3_deployment_partial_failure(self):
        """Trust approved, some files deploy successfully, others fail."""
        files = _make_files([
            {"filename": "app.js", "content": _b64("console.log('ok')"), "content_type": "application/javascript"}
        ])
        mock_s3 = MagicMock()
        # Staging succeeds for both files
        mock_s3.put_object.return_value = None
        # Copy succeeds for first, fails for second
        mock_s3.copy_object.side_effect = [None, ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'Copy failed for app.js'}}, 'CopyObject')]

        mock_table = MagicMock()
        mock_history = MagicMock()
        mock_cf = MagicMock()

        trust_session = {"request_id": "trust-partial", "expires_at": 9999999999}

        mock_projects_table = MagicMock()
        mock_projects_table.get_item.return_value = {
            "Item": {
                "project_id": "ztp-files",
                "frontend_deploy_role_arn": _ZTP_FRONTEND_CONFIG["deploy_role_arn"],
            }
        }

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(True, trust_session, "trust approved")), \
             patch("mcp_deploy_frontend.deployer_projects_table", mock_projects_table), \
             patch("mcp_deploy_frontend.deployer_history_table", mock_history), \
             patch("aws_clients.get_cloudfront_client", return_value=mock_cf), \
             patch("trust.increment_trust_command_count", return_value=2), \
             patch("notifications.send_trust_auto_approve_notification"), \
             patch("utils.log_decision"):
            result = _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Partial failure test",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
                "account_id": "190825685292",
            })

        parsed = _parse(result)
        assert parsed["status"] == "partial_success"
        assert parsed["deployed"] == 1  # index.html succeeded
        assert parsed["failed"] == 1    # app.js failed

        # History should record partial failure
        history_item = mock_history.put_item.call_args[1]["Item"]
        assert len(history_item["deployed_files"]) == 1
        assert len(history_item["failed_files"]) == 1
        assert history_item["status"] == "failed"


class TestTrustSessionAuditLogging:
    """Test audit logging for trust session deployments."""

    def test_trust_bypass_logged_in_decision(self):
        """Trust session deployments should log with trust_bypass=True."""
        files = _make_files()
        mock_s3 = MagicMock()
        mock_table = MagicMock()
        mock_history = MagicMock()
        mock_cf = MagicMock()
        mock_log_decision = MagicMock()

        trust_session = {"request_id": "trust-audit", "expires_at": 9999999999}

        mock_projects_table = MagicMock()
        mock_projects_table.get_item.return_value = {
            "Item": {
                "project_id": "ztp-files",
                "frontend_deploy_role_arn": _ZTP_FRONTEND_CONFIG["deploy_role_arn"],
            }
        }

        with patch("mcp_deploy_frontend.get_s3_client", return_value=mock_s3), \
             patch("mcp_deploy_frontend.table", mock_table), \
             patch("mcp_deploy_frontend._get_frontend_config", return_value=_ZTP_FRONTEND_CONFIG), \
             patch("trust.should_trust_approve", return_value=(True, trust_session, "trust approved")), \
             patch("mcp_deploy_frontend.deployer_projects_table", mock_projects_table), \
             patch("mcp_deploy_frontend.deployer_history_table", mock_history), \
             patch("aws_clients.get_cloudfront_client", return_value=mock_cf), \
             patch("trust.increment_trust_command_count", return_value=1), \
             patch("notifications.send_trust_auto_approve_notification"), \
             patch("utils.log_decision", mock_log_decision):
            _call({
                "project": "ztp-files",
                "files": files,
                "reason": "Audit log test",
                "source": "Private Bot",
                "trust_scope": "ts-ztp",
                "account_id": "190825685292",
            })

        # Verify log_decision was called with trust_bypass=True
        assert mock_log_decision.called
        log_call = mock_log_decision.call_args
        kwargs = log_call[1]
        assert kwargs["trust_bypass"] is True
        assert kwargs["trust_scope"] == "ts-ztp"
        assert kwargs["decision_type"] == "trust_approved"
        assert "bouncer_deploy_frontend" in kwargs["command"]
