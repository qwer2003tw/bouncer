"""
Regression tests for sprint12-002: deploy_status expired vs pending distinction.
TTL check happens in mcp_tool_deploy_status, not get_deploy_status.
"""
import pytest, time, json
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

def call_mcp_deploy_status(deploy_id, record):
    import deployer
    with patch('deployer.get_deploy_status', return_value=record):
        result = deployer.mcp_tool_deploy_status('req-id', {'deploy_id': deploy_id})
    body = json.loads(result.get('body', '{}'))
    content = body.get('result', {}).get('content', [{}])[0].get('text', '{}')
    return json.loads(content)

class TestDeployStatusExpired:
    def test_expired_when_pending_and_ttl_passed(self):
        record = {'status': 'pending', 'deploy_id': 'deploy-test', 'ttl': int(time.time()) - 100}
        result = call_mcp_deploy_status('deploy-test', record)
        assert result.get('status') == 'expired'

    def test_pending_when_ttl_not_passed(self):
        record = {'status': 'pending', 'deploy_id': 'deploy-test', 'ttl': int(time.time()) + 300}
        result = call_mcp_deploy_status('deploy-test', record)
        assert result.get('status') in ('pending_approval', 'pending')

    def test_success_unchanged(self):
        record = {'status': 'SUCCESS', 'deploy_id': 'deploy-test', 'ttl': int(time.time()) - 100}
        result = call_mcp_deploy_status('deploy-test', record)
        assert result.get('status') == 'SUCCESS'

    def test_not_found_returns_informational(self):
        record = {'status': 'not_found', 'message': 'not found'}
        result = call_mcp_deploy_status('nonexistent', record)
        assert result.get('status') == 'not_found'
