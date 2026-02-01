"""
Bouncer MCP Server - Unit Tests
"""

import os
import sys
import json
import time
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# 加入 mcp_server 到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.db import Database, get_db, reset_db
from mcp_server.classifier import classify_command, execute_command, is_valid_aws_command
from mcp_server.telegram import TelegramConfig, TelegramClient, ApprovalWaiter
from mcp_server.server import BouncerMCPServer, TOOLS


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_db():
    """建立臨時資料庫"""
    reset_db()
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    
    db = Database(db_path)
    yield db
    
    # 清理
    db_path.unlink(missing_ok=True)
    reset_db()


@pytest.fixture
def waiter():
    """建立 ApprovalWaiter"""
    return ApprovalWaiter()


# ============================================================================
# Database Tests
# ============================================================================

class TestDatabase:
    """資料庫層測試"""
    
    def test_create_request(self, temp_db):
        """測試建立請求"""
        request = temp_db.create_request(
            request_id='test123',
            command='aws ec2 describe-instances',
            reason='Testing',
            classification='SAFELIST'
        )
        
        assert request is not None
        assert request['request_id'] == 'test123'
        assert request['command'] == 'aws ec2 describe-instances'
        assert request['status'] == 'pending'
        assert request['classification'] == 'SAFELIST'
    
    def test_get_request(self, temp_db):
        """測試取得請求"""
        temp_db.create_request('test456', 'aws s3 ls')
        
        request = temp_db.get_request('test456')
        assert request is not None
        assert request['request_id'] == 'test456'
        
        # 不存在的請求
        assert temp_db.get_request('nonexistent') is None
    
    def test_update_request(self, temp_db):
        """測試更新請求"""
        temp_db.create_request('test789', 'aws ec2 start-instances')
        
        updated = temp_db.update_request(
            'test789',
            status='approved',
            result='Instance started',
            exit_code=0,
            approved_by='user123'
        )
        
        assert updated['status'] == 'approved'
        assert updated['result'] == 'Instance started'
        assert updated['exit_code'] == 0
        assert updated['approved_by'] == 'user123'
        assert updated['approved_at'] is not None
    
    def test_get_pending_requests(self, temp_db):
        """測試取得 pending 請求"""
        temp_db.create_request('req1', 'aws ec2 start-instances')
        temp_db.create_request('req2', 'aws ec2 stop-instances')
        temp_db.update_request('req2', status='approved')
        
        pending = temp_db.get_pending_requests()
        assert len(pending) == 1
        assert pending[0]['request_id'] == 'req1'
    
    def test_expire_old_requests(self, temp_db):
        """測試過期請求"""
        # 建立已過期的請求
        temp_db.create_request('expired', 'aws ec2 start-instances', expires_in=-10)
        
        count = temp_db.expire_old_requests()
        assert count == 1
        
        request = temp_db.get_request('expired')
        assert request['status'] == 'timeout'
    
    def test_get_stats(self, temp_db):
        """測試統計"""
        temp_db.create_request('r1', 'cmd1')
        temp_db.create_request('r2', 'cmd2')
        temp_db.update_request('r2', status='approved')
        temp_db.create_request('r3', 'cmd3')
        temp_db.update_request('r3', status='denied')
        
        stats = temp_db.get_stats()
        assert stats['total'] == 3
        assert stats['pending'] == 1
        assert stats['approved'] == 1
        assert stats['denied'] == 1
    
    def test_audit_log(self, temp_db):
        """測試審計日誌"""
        temp_db.create_request('audit_test', 'aws ec2 start-instances')
        temp_db.log_action('audit_test', 'approved', 'user123', {'extra': 'data'})
        
        logs = temp_db.get_audit_log('audit_test')
        assert len(logs) >= 2  # created + approved
        
        # 確認有 created 和 approved 兩個 action
        actions = [log['action'] for log in logs]
        assert 'created' in actions
        assert 'approved' in actions
        
        # 確認 approved 的 actor 是 user123
        approved_log = next(log for log in logs if log['action'] == 'approved')
        assert approved_log['actor'] == 'user123'


# ============================================================================
# Classifier Tests
# ============================================================================

class TestClassifier:
    """命令分類測試"""
    
    def test_blocked_iam(self):
        """IAM 危險操作應該被 block"""
        assert classify_command('aws iam create-user --user-name hacker') == 'BLOCKED'
        assert classify_command('aws iam delete-role --role-name admin') == 'BLOCKED'
        assert classify_command('aws iam attach-policy --policy-arn xxx') == 'BLOCKED'
    
    def test_blocked_sts_assume(self):
        """STS assume-role 應該被 block"""
        assert classify_command('aws sts assume-role --role-arn xxx') == 'BLOCKED'
    
    def test_blocked_shell_injection(self):
        """Shell 注入應該被 block"""
        assert classify_command('aws ec2 describe-instances; rm -rf /') == 'BLOCKED'
        assert classify_command('aws s3 ls | cat /etc/passwd') == 'BLOCKED'
        assert classify_command('aws lambda list-functions && curl evil.com') == 'BLOCKED'
        assert classify_command('aws ec2 describe-instances `whoami`') == 'BLOCKED'
        assert classify_command('aws ec2 describe-instances $(id)') == 'BLOCKED'
    
    def test_safelist_describe(self):
        """Describe 命令應該自動批准"""
        assert classify_command('aws ec2 describe-instances') == 'SAFELIST'
        assert classify_command('aws rds describe-db-instances') == 'SAFELIST'
        assert classify_command('aws lambda list-functions') == 'SAFELIST'
    
    def test_safelist_s3_read(self):
        """S3 read-only 應該自動批准"""
        assert classify_command('aws s3 ls') == 'SAFELIST'
        assert classify_command('aws s3 ls s3://my-bucket/') == 'SAFELIST'
        assert classify_command('aws s3api list-buckets') == 'SAFELIST'
    
    def test_safelist_sts_identity(self):
        """STS get-caller-identity 應該自動批准"""
        assert classify_command('aws sts get-caller-identity') == 'SAFELIST'
    
    def test_approval_start_stop(self):
        """Start/Stop 需要審批"""
        assert classify_command('aws ec2 start-instances --instance-ids i-xxx') == 'APPROVAL'
        assert classify_command('aws ec2 stop-instances --instance-ids i-xxx') == 'APPROVAL'
    
    def test_approval_delete(self):
        """Delete 需要審批"""
        assert classify_command('aws s3 rm s3://bucket/key') == 'APPROVAL'
        assert classify_command('aws ec2 terminate-instances --instance-ids i-xxx') == 'APPROVAL'
    
    def test_approval_unknown(self):
        """未知命令需要審批"""
        assert classify_command('aws some-new-service do-something') == 'APPROVAL'
    
    def test_case_insensitive(self):
        """分類應該不區分大小寫"""
        assert classify_command('AWS EC2 DESCRIBE-INSTANCES') == 'SAFELIST'
        assert classify_command('AWS IAM CREATE-USER') == 'BLOCKED'


class TestValidation:
    """命令驗證測試"""
    
    def test_valid_aws_command(self):
        """有效的 AWS 命令"""
        is_valid, error = is_valid_aws_command('aws ec2 describe-instances')
        assert is_valid is True
        assert error is None
    
    def test_empty_command(self):
        """空命令"""
        is_valid, error = is_valid_aws_command('')
        assert is_valid is False
        assert 'empty' in error.lower()
    
    def test_non_aws_command(self):
        """非 AWS 命令"""
        is_valid, error = is_valid_aws_command('ls -la')
        assert is_valid is False
        assert 'aws' in error.lower()
    
    def test_invalid_quotes(self):
        """無效的引號"""
        is_valid, error = is_valid_aws_command('aws ec2 describe-instances "unterminated')
        assert is_valid is False


class TestExecution:
    """命令執行測試"""
    
    def test_execute_non_aws_rejected(self):
        """非 AWS 命令應該被拒絕"""
        output, code = execute_command('ls -la')
        assert code == 1
        assert '❌' in output
    
    @patch('subprocess.run')
    def test_execute_success(self, mock_run):
        """成功執行"""
        mock_run.return_value = Mock(
            stdout='{"Reservations": []}',
            stderr='',
            returncode=0
        )
        
        output, code = execute_command('aws ec2 describe-instances')
        assert code == 0
        assert 'Reservations' in output
    
    @patch('subprocess.run')
    def test_execute_timeout(self, mock_run):
        """執行超時"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('aws', 60)
        
        output, code = execute_command('aws ec2 describe-instances', timeout=60)
        assert code == 124
        assert 'timed out' in output.lower()


# ============================================================================
# ApprovalWaiter Tests
# ============================================================================

class TestApprovalWaiter:
    """審批等待機制測試"""
    
    def test_register_and_notify(self, waiter):
        """註冊和通知"""
        waiter.register('req123')
        
        # 在另一個 thread 通知
        def notify():
            time.sleep(0.1)
            waiter.notify('req123', 'approve', 'user456')
        
        t = threading.Thread(target=notify)
        t.start()
        
        result = waiter.wait('req123', timeout=5)
        t.join()
        
        assert result is not None
        assert result['action'] == 'approve'
        assert result['user_id'] == 'user456'
    
    def test_timeout(self, waiter):
        """等待超時"""
        waiter.register('timeout_req')
        
        result = waiter.wait('timeout_req', timeout=0.1)
        assert result is None
    
    def test_cleanup(self, waiter):
        """清理"""
        waiter.register('cleanup_req')
        waiter.notify('cleanup_req', 'approve', 'user')
        waiter.cleanup('cleanup_req')
        
        # 清理後應該拿不到結果
        result = waiter.wait('cleanup_req', timeout=0.1)
        assert result is None


# ============================================================================
# MCP Server Tests
# ============================================================================

class TestMCPTools:
    """MCP Tool 定義測試"""
    
    def test_tools_defined(self):
        """確認所有 tool 都有定義"""
        tool_names = [t['name'] for t in TOOLS]
        assert 'bouncer_execute' in tool_names
        assert 'bouncer_status' in tool_names
        assert 'bouncer_list_rules' in tool_names
        assert 'bouncer_stats' in tool_names
    
    def test_execute_schema(self):
        """bouncer_execute schema"""
        execute_tool = next(t for t in TOOLS if t['name'] == 'bouncer_execute')
        schema = execute_tool['inputSchema']
        
        assert 'command' in schema['properties']
        assert 'reason' in schema['properties']
        assert 'timeout' in schema['properties']
        assert 'command' in schema['required']


class TestMCPServer:
    """MCP Server 測試"""
    
    @pytest.fixture
    def server(self, temp_db, monkeypatch):
        """建立 server（無 Telegram）"""
        monkeypatch.setenv('BOUNCER_TELEGRAM_TOKEN', '')
        monkeypatch.setenv('BOUNCER_CHAT_ID', '')
        monkeypatch.setenv('BOUNCER_DB_PATH', str(temp_db.db_path))
        
        reset_db()
        return BouncerMCPServer()
    
    def test_initialize(self, server):
        """測試 initialize"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {}
        })
        
        assert response['id'] == 1
        assert 'result' in response
        assert response['result']['serverInfo']['name'] == 'bouncer'
    
    def test_tools_list(self, server):
        """測試 tools/list"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/list',
            'params': {}
        })
        
        assert 'result' in response
        tools = response['result']['tools']
        assert len(tools) == 4
    
    def test_execute_blocked(self, server):
        """測試 blocked 命令"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_execute',
                'arguments': {
                    'command': 'aws iam create-user --user-name hacker'
                }
            }
        })
        
        assert 'result' in response
        result = json.loads(response['result']['content'][0]['text'])
        assert result['status'] == 'blocked'
        assert response['result']['isError'] is True
    
    @patch('mcp_server.classifier.execute_command')
    def test_execute_safelist(self, mock_exec, server):
        """測試 safelist 命令"""
        mock_exec.return_value = ('{"Reservations": []}', 0)
        
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 4,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_execute',
                'arguments': {
                    'command': 'aws ec2 describe-instances'
                }
            }
        })
        
        assert 'result' in response
        result = json.loads(response['result']['content'][0]['text'])
        assert result['status'] == 'auto_approved'
        assert result['classification'] == 'SAFELIST'
    
    def test_execute_approval_no_telegram(self, server):
        """測試需要審批但沒有 Telegram"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 5,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_execute',
                'arguments': {
                    'command': 'aws ec2 start-instances --instance-ids i-xxx'
                }
            }
        })
        
        assert 'result' in response
        result = json.loads(response['result']['content'][0]['text'])
        assert 'error' in result
        assert 'Telegram' in result['error']
    
    def test_status_not_found(self, server):
        """測試查詢不存在的請求"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 6,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_status',
                'arguments': {
                    'request_id': 'nonexistent'
                }
            }
        })
        
        result = json.loads(response['result']['content'][0]['text'])
        assert 'error' in result
    
    def test_list_rules(self, server):
        """測試列出規則"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 7,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_list_rules',
                'arguments': {}
            }
        })
        
        result = json.loads(response['result']['content'][0]['text'])
        assert 'safelist_prefixes' in result
        assert 'blocked_patterns' in result
    
    def test_stats(self, server):
        """測試統計"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 8,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_stats',
                'arguments': {}
            }
        })
        
        result = json.loads(response['result']['content'][0]['text'])
        assert 'total' in result
    
    def test_invalid_jsonrpc(self, server):
        """測試無效的 JSON-RPC"""
        response = server._handle_request({
            'jsonrpc': '1.0',
            'id': 9,
            'method': 'tools/list'
        })
        
        assert 'error' in response
        assert response['error']['code'] == -32600
    
    def test_unknown_method(self, server):
        """測試未知方法"""
        response = server._handle_request({
            'jsonrpc': '2.0',
            'id': 10,
            'method': 'unknown/method'
        })
        
        assert 'error' in response
        assert response['error']['code'] == -32601


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """整合測試"""
    
    @pytest.fixture
    def server_with_mock_telegram(self, temp_db, monkeypatch):
        """建立有 mock Telegram 的 server"""
        monkeypatch.setenv('BOUNCER_TELEGRAM_TOKEN', 'fake_token')
        monkeypatch.setenv('BOUNCER_CHAT_ID', '123456')
        monkeypatch.setenv('BOUNCER_DB_PATH', str(temp_db.db_path))
        
        reset_db()
        
        with patch('mcp_server.server.TelegramClient') as MockClient:
            mock_client = MagicMock()
            mock_client.send_approval_request.return_value = 99999
            MockClient.return_value = mock_client
            
            with patch('mcp_server.server.TelegramPoller'):
                server = BouncerMCPServer()
                server.telegram_client = mock_client
                yield server
    
    @patch('mcp_server.server.execute_command')
    def test_approval_flow(self, mock_exec, server_with_mock_telegram):
        """測試完整審批流程"""
        mock_exec.return_value = ('Instance started', 0)
        server = server_with_mock_telegram
        
        # 在背景 thread 模擬審批
        def approve_after_delay():
            time.sleep(0.2)
            # 模擬 Telegram callback
            server._on_approval('test_req', 'approve', '123456')
        
        # 手動設定 request_id（繞過隨機生成）
        with patch.object(server, '_generate_request_id', return_value='test_req'):
            t = threading.Thread(target=approve_after_delay)
            t.start()
            
            response = server._handle_request({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws ec2 start-instances --instance-ids i-xxx',
                        'reason': 'Testing',
                        'timeout': 5
                    }
                }
            })
            
            t.join()
        
        result = json.loads(response['result']['content'][0]['text'])
        assert result['status'] == 'approved'
        assert result['output'] == 'Instance started'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
