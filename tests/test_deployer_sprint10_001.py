"""
Sprint 10-001: deploy_status phase 不準確 — Regression Tests
Tests for:
- R1: get_deploy_status() returns {status: pending} when record not found (not error)
- R2: mcp_tool_deploy_status() does not set isError for pending status
- R3: RUNNING status returns elapsed_seconds
- R4: SUCCESS/FAILED status returns duration_seconds
- R5: Backward compat — existing status checks unaffected
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ---------------------------------------------------------------------------
# Helper: load deployer module in isolation
# ---------------------------------------------------------------------------

def _load_deployer():
    for mod in list(sys.modules.keys()):
        if 'deployer' in mod:
            del sys.modules[mod]
    import deployer as dep
    return dep


# ===========================================================================
# R1: get_deploy_status() returns pending when record not found
# ===========================================================================

class TestGetDeployStatusRecordNotFound:
    """R1: Record 不存在時回傳 pending，不回傳 error"""

    def test_returns_pending_status(self):
        """Record 不存在 → status=not_found (Sprint 12 changed from pending)"""
        dep = _load_deployer()
        with patch.object(dep, 'get_deploy_record', return_value=None):
            result = dep.get_deploy_status('deploy-nonexistent')

        assert result['status'] == 'not_found'
        assert 'error' not in result

    def test_returns_deploy_id(self):
        """回傳包含傳入的 deploy_id"""
        dep = _load_deployer()
        with patch.object(dep, 'get_deploy_record', return_value=None):
            result = dep.get_deploy_status('deploy-abc123')

        assert result['deploy_id'] == 'deploy-abc123'

    def test_returns_retry_message(self):
        """回傳包含 retry 提示訊息 (Sprint 12: hint 欄位而非 message)"""
        dep = _load_deployer()
        with patch.object(dep, 'get_deploy_record', return_value=None):
            result = dep.get_deploy_status('deploy-xyz')

        assert 'message' in result or 'hint' in result
        # Sprint 12: retry hint is in the 'hint' field (or message field)
        hint_or_msg = result.get('hint', '') + result.get('message', '')
        assert 'retry' in hint_or_msg.lower()

    def test_no_error_key(self):
        """回傳不含 error key（不能誤判為 error）"""
        dep = _load_deployer()
        with patch.object(dep, 'get_deploy_record', return_value=None):
            result = dep.get_deploy_status('deploy-xyz')

        assert 'error' not in result


# ===========================================================================
# R2: mcp_tool_deploy_status() isError fix
# ===========================================================================

class TestMCPToolDeployStatusIsError:
    """R2: mcp_tool_deploy_status() isError 只對真正錯誤設 True"""

    def _call_mcp_status(self, dep, deploy_id, record):
        """Helper: call mcp_tool_deploy_status with a mocked get_deploy_status.
        Returns the inner result dict from the Lambda body JSON."""
        with patch.object(dep, 'get_deploy_status', return_value=record):
            raw = dep.mcp_tool_deploy_status('req-1', {'deploy_id': deploy_id})
        # mcp_tool returns Lambda response: {statusCode, body: JSON string}
        body = json.loads(raw['body'])
        return body['result']

    def test_pending_is_not_error(self):
        """status=pending → isError=False"""
        dep = _load_deployer()
        record = {
            'status': 'not_found',
            'deploy_id': 'deploy-abc',
            'message': 'Deploy record not found yet, please retry',
        }
        result = self._call_mcp_status(dep, 'deploy-abc', record)
        assert result.get('isError', False) is False

    def test_running_is_not_error(self):
        """status=RUNNING → isError=False"""
        dep = _load_deployer()
        record = {
            'deploy_id': 'deploy-abc',
            'status': 'RUNNING',
            'started_at': int(time.time()) - 30,
            'elapsed_seconds': 30,
        }
        result = self._call_mcp_status(dep, 'deploy-abc', record)
        assert result.get('isError', False) is False

    def test_success_is_not_error(self):
        """status=SUCCESS → isError=False"""
        dep = _load_deployer()
        now = int(time.time())
        record = {
            'deploy_id': 'deploy-abc',
            'status': 'SUCCESS',
            'started_at': now - 120,
            'finished_at': now,
            'duration_seconds': 120,
        }
        result = self._call_mcp_status(dep, 'deploy-abc', record)
        assert result.get('isError', False) is False

    def test_failed_is_not_error(self):
        """status=FAILED → isError=False (failure is expected outcome, not a tool error)"""
        dep = _load_deployer()
        now = int(time.time())
        record = {
            'deploy_id': 'deploy-abc',
            'status': 'FAILED',
            'started_at': now - 60,
            'finished_at': now,
            'duration_seconds': 60,
        }
        result = self._call_mcp_status(dep, 'deploy-abc', record)
        assert result.get('isError', False) is False

    def test_content_json_parseable(self):
        """MCP response content[0].text is valid JSON with status=not_found (Sprint 12)"""
        dep = _load_deployer()
        record = {'status': 'not_found', 'deploy_id': 'deploy-x', 'message': 'retry'}
        result = self._call_mcp_status(dep, 'deploy-x', record)
        content_text = result['content'][0]['text']
        data = json.loads(content_text)
        assert data['status'] == 'not_found'


# ===========================================================================
# R3: RUNNING status returns elapsed_seconds
# ===========================================================================

class TestElapsedSeconds:
    """R3: RUNNING 狀態回傳 elapsed_seconds"""

    def test_running_has_elapsed_seconds(self):
        """RUNNING 狀態下，回傳包含 elapsed_seconds"""
        dep = _load_deployer()
        started = int(time.time()) - 45
        record = {
            'deploy_id': 'deploy-run-1',
            'status': 'RUNNING',
            'started_at': started,
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            with patch.object(dep, '_get_sfn_client', side_effect=Exception('no sfn')):
                result = dep.get_deploy_status('deploy-run-1')

        assert 'elapsed_seconds' in result
        # Should be approximately 45 seconds (allow +-5s for test timing)
        assert 40 <= result['elapsed_seconds'] <= 55

    def test_elapsed_seconds_not_in_pending(self):
        """pending 狀態不應有 elapsed_seconds（record 不存在）"""
        dep = _load_deployer()
        with patch.object(dep, 'get_deploy_record', return_value=None):
            result = dep.get_deploy_status('deploy-nonexistent')

        assert 'elapsed_seconds' not in result

    def test_elapsed_seconds_not_in_success(self):
        """SUCCESS 狀態不應有 elapsed_seconds（應有 duration_seconds）"""
        dep = _load_deployer()
        now = int(time.time())
        record = {
            'deploy_id': 'deploy-ok',
            'status': 'SUCCESS',
            'started_at': now - 120,
            'finished_at': now,
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            result = dep.get_deploy_status('deploy-ok')

        assert 'elapsed_seconds' not in result
        assert 'duration_seconds' in result


# ===========================================================================
# R4: SUCCESS/FAILED status returns duration_seconds
# ===========================================================================

class TestDurationSeconds:
    """R4: SUCCESS/FAILED 狀態回傳 duration_seconds"""

    def test_success_has_duration_seconds(self):
        """SUCCESS 狀態下，回傳包含 duration_seconds"""
        dep = _load_deployer()
        now = int(time.time())
        started = now - 150
        record = {
            'deploy_id': 'deploy-s1',
            'status': 'SUCCESS',
            'started_at': started,
            'finished_at': now,
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            result = dep.get_deploy_status('deploy-s1')

        assert 'duration_seconds' in result
        assert result['duration_seconds'] == 150

    def test_failed_has_duration_seconds(self):
        """FAILED 狀態下，回傳包含 duration_seconds"""
        dep = _load_deployer()
        now = int(time.time())
        started = now - 60
        record = {
            'deploy_id': 'deploy-f1',
            'status': 'FAILED',
            'started_at': started,
            'finished_at': now,
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            result = dep.get_deploy_status('deploy-f1')

        assert 'duration_seconds' in result
        assert result['duration_seconds'] == 60

    def test_duration_not_in_running(self):
        """RUNNING 狀態不應有 duration_seconds"""
        dep = _load_deployer()
        started = int(time.time()) - 30
        record = {
            'deploy_id': 'deploy-run',
            'status': 'RUNNING',
            'started_at': started,
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            with patch.object(dep, '_get_sfn_client', side_effect=Exception('no sfn')):
                result = dep.get_deploy_status('deploy-run')

        assert 'duration_seconds' not in result

    def test_no_started_at_no_duration(self):
        """無 started_at 時不報錯，只是缺少 duration_seconds"""
        dep = _load_deployer()
        record = {
            'deploy_id': 'deploy-x',
            'status': 'SUCCESS',
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            result = dep.get_deploy_status('deploy-x')

        assert 'duration_seconds' not in result
        assert result['status'] == 'SUCCESS'


# ===========================================================================
# R5: Backward compat — existing status values unaffected
# ===========================================================================

class TestBackwardCompat:
    """R5: 現有行為不受影響"""

    def test_pending_status_record_exists(self):
        """DDB record 存在且 status=PENDING → 正常回傳，不誤判"""
        dep = _load_deployer()
        record = {
            'deploy_id': 'deploy-p1',
            'status': 'PENDING',
            'project_id': 'bouncer',
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            result = dep.get_deploy_status('deploy-p1')

        assert result['status'] == 'PENDING'
        assert 'error' not in result

    def test_existing_record_fields_preserved(self):
        """Record 既有欄位（project_id, branch 等）在回傳中保留"""
        dep = _load_deployer()
        now = int(time.time())
        record = {
            'deploy_id': 'deploy-x',
            'status': 'SUCCESS',
            'project_id': 'bouncer',
            'branch': 'master',
            'started_at': now - 100,
            'finished_at': now,
        }
        with patch.object(dep, 'get_deploy_record', return_value=record):
            result = dep.get_deploy_status('deploy-x')

        assert result['project_id'] == 'bouncer'
        assert result['branch'] == 'master'
        assert result['status'] == 'SUCCESS'
        assert result['duration_seconds'] == 100
