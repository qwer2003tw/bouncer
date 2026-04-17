"""
Regression tests for sprint12-002: deploy_status expired vs pending distinction.
TTL check happens in mcp_tool_deploy_status, not get_deploy_status.
"""
import pytest, time, json
from unittest.mock import patch, MagicMock
import sys, os

def call_mcp_deploy_status(deploy_id, record):
    import deployer
    with patch('deployer.get_deploy_status', return_value=record):
        result = deployer.mcp_tool_deploy_status('req-id', {'deploy_id': deploy_id})
    body = json.loads(result.get('body', '{}'))
    content = body.get('result', {}).get('content', [{}])[0].get('text', '{}')
    return json.loads(content)

class TestDeployStatusExpired:
    def test_expired_when_pending_and_ttl_passed(self):
        record = {'status': 'pending_approval', 'deploy_id': 'deploy-test', 'ttl': int(time.time()) - 100}
        result = call_mcp_deploy_status('deploy-test', record)
        assert result.get('status') == 'expired'

    def test_pending_when_ttl_not_passed(self):
        record = {'status': 'pending_approval', 'deploy_id': 'deploy-test', 'ttl': int(time.time()) + 300}
        result = call_mcp_deploy_status('deploy-test', record)
        assert result.get('status') == 'pending_approval'

    def test_success_unchanged(self):
        record = {'status': 'SUCCESS', 'deploy_id': 'deploy-test', 'ttl': int(time.time()) - 100}
        result = call_mcp_deploy_status('deploy-test', record)
        assert result.get('status') == 'SUCCESS'

    def test_not_found_returns_informational(self):
        record = {'status': 'not_found', 'message': 'not found'}
        result = call_mcp_deploy_status('nonexistent', record)
        assert result.get('status') == 'not_found'

    def test_regression_deploy_status_pending_string_mismatch(self):
        """
        Regression test for #69: TTL expiry check must use 'pending_approval' not 'pending'.
        When a record has status='pending_approval' (as created in L936) and TTL has expired,
        bouncer_deploy_status should return status='expired', not the original 'pending_approval'.
        """
        record = {'status': 'pending_approval', 'deploy_id': 'deploy-test-69', 'ttl': int(time.time()) - 100}
        result = call_mcp_deploy_status('deploy-test-69', record)
        assert result.get('status') == 'expired', "Expected 'expired' for pending_approval with expired TTL"
