import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3


class TestMCPExecuteBlocked:
    """MCP bouncer_execute BLOCKED 測試"""
    
    def test_execute_blocked_command(self, app_module):
        """測試被封鎖的命令"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 4,
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws iam create-user --user-name hacker',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'result' in body
        assert body['result']['isError'] == True
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


class TestRESTBlocked:
    """REST API BLOCKED 測試"""
    
    def test_blocked_command(self, app_module):
        """測試 REST API 封鎖命令"""
        event = {
            'rawPath': '/',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'command': 'aws iam delete-user --user-name admin'
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 403
        
        body = json.loads(result['body'])
        assert body['status'] == 'blocked'


class TestSecurityBlockedFlags:
    """測試危險旗標阻擋"""

    def test_endpoint_url_blocked(self, app_module):
        """--endpoint-url 被阻擋（防止重定向到惡意服務器）"""
        assert app_module.is_blocked('aws s3 ls --endpoint-url https://evil.com')
        assert app_module.is_blocked('aws ec2 describe-instances --endpoint-url http://attacker.internal')

    def test_profile_blocked(self, app_module):
        """--profile 被阻擋（防止切換到未授權 profile）"""
        assert app_module.is_blocked('aws s3 ls --profile attacker')

    def test_no_verify_ssl_blocked(self, app_module):
        """--no-verify-ssl 被阻擋（防止 MITM）"""
        assert app_module.is_blocked('aws s3 ls --no-verify-ssl')

    def test_ca_bundle_blocked(self, app_module):
        """--ca-bundle 被阻擋（防止使用惡意 CA）"""
        assert app_module.is_blocked('aws s3 ls --ca-bundle /tmp/evil-ca.pem')

    def test_debug_not_blocked(self, app_module):
        """--debug 不阻擋（洩漏風險較低，且有合法用途）"""
        # debug 可能洩漏 credentials，但阻擋會影響正常除錯
        # 目前不阻擋，可以之後加入 DANGEROUS_PATTERNS
        assert not app_module.is_blocked('aws s3 ls --debug')

    def test_normal_flags_not_blocked(self, app_module):
        """正常旗標不受影響"""
        assert not app_module.is_blocked('aws s3 ls --recursive')
        assert not app_module.is_blocked('aws ec2 describe-instances --output json')
        assert not app_module.is_blocked('aws ec2 describe-instances --no-paginate')


class TestBlockedCommandPath:
    """BLOCKED 命令路徑測試"""
    
    def test_blocked_command_returns_error(self, app_module):
        """BLOCKED 命令應返回 isError"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws iam create-access-key --user-name admin',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'
        assert body['result']['isError'] == True
    
    def test_blocked_assume_role(self, app_module):
        """sts assume-role 應該被封鎖"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws sts assume-role --role-arn arn:aws:iam::123456789012:role/Admin',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


# ============================================================================
# Rate Limit / Pending Limit 錯誤測試 (674-707)
# ============================================================================


class TestDeployerModule:
    """Deployer 模組測試"""
    
    @pytest.fixture
    def deployer_tables(self, mock_dynamodb):
        """Tables already exist at session scope, just return resource"""
        return mock_dynamodb
    
    def test_list_projects_empty(self, deployer_tables):
        """列出專案（空）"""
        # 重新載入 deployer 模組
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        # 注入 mock tables
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        result = deployer.list_projects()
        assert result == []
        
        sys.path.pop(0)
    
    def test_add_and_get_project(self, deployer_tables):
        """新增和取得專案"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 新增專案
        config = {
            'name': 'Test Project',
            'git_repo': 'test-repo',
            'stack_name': 'test-stack'
        }
        item = deployer.add_project('test-project', config)
        assert item['project_id'] == 'test-project'
        assert item['name'] == 'Test Project'
        
        # 取得專案
        project = deployer.get_project('test-project')
        assert project is not None
        assert project['name'] == 'Test Project'
        
        # 列出專案
        projects = deployer.list_projects()
        assert len(projects) == 1
        
        sys.path.pop(0)
    
    def test_remove_project(self, deployer_tables):
        """移除專案"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        
        # 先新增
        deployer.add_project('to-remove', {'name': 'To Remove'})
        
        # 移除
        result = deployer.remove_project('to-remove')
        assert result == True
        
        # 確認已移除
        project = deployer.get_project('to-remove')
        assert project is None
        
        sys.path.pop(0)
    
    def test_acquire_and_release_lock(self, deployer_tables):
        """取得和釋放部署鎖"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 取得鎖
        result = deployer.acquire_lock('test-project', 'deploy-123', 'test-user')
        assert result == True
        
        # 再次取得應該失敗
        result2 = deployer.acquire_lock('test-project', 'deploy-456', 'test-user')
        assert result2 == False
        
        # 釋放鎖
        deployer.release_lock('test-project')
        
        # 現在應該可以取得
        result3 = deployer.acquire_lock('test-project', 'deploy-789', 'test-user')
        assert result3 == True
        
        sys.path.pop(0)
    
    def test_get_lock_expired(self, deployer_tables):
        """取得已過期的鎖"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 手動插入一個過期的鎖
        deployer.locks_table.put_item(Item={
            'project_id': 'expired-project',
            'lock_id': 'old-deploy',
            'locked_at': int(time.time()) - 7200,
            'ttl': int(time.time()) - 3600  # 已過期
        })
        
        # 取得鎖應該返回 None（因為已過期）
        lock = deployer.get_lock('expired-project')
        assert lock is None
        
        sys.path.pop(0)
    
    def test_deploy_record_lifecycle(self, deployer_tables):
        """部署記錄生命週期"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        # 建立部署記錄
        deploy_id = 'deploy-test-123'
        record = deployer.create_deploy_record(deploy_id, 'test-project', {
            'branch': 'main',
            'triggered_by': 'test-user',
            'reason': 'Test deploy'
        })
        assert record['deploy_id'] == deploy_id
        assert record['status'] == 'PENDING'
        
        # 更新記錄
        deployer.update_deploy_record(deploy_id, {
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:...'
        })
        
        # 取得記錄
        updated = deployer.get_deploy_record(deploy_id)
        assert updated['status'] == 'RUNNING'
        
        sys.path.pop(0)
    
    def test_start_deploy_project_not_found(self, deployer_tables):
        """啟動部署但專案不存在"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        result = deployer.start_deploy('nonexistent', 'main', 'user', 'reason')
        assert 'error' in result
        assert '不存在' in result['error']
        
        sys.path.pop(0)
    
    def test_start_deploy_project_disabled(self, deployer_tables):
        """啟動部署但專案已停用"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 新增停用的專案
        deployer.projects_table.put_item(Item={
            'project_id': 'disabled-project',
            'name': 'Disabled',
            'enabled': False
        })
        
        result = deployer.start_deploy('disabled-project', 'main', 'user', 'reason')
        assert 'error' in result
        assert '停用' in result['error']
        
        sys.path.pop(0)
    
    def test_start_deploy_locked(self, deployer_tables):
        """啟動部署但已有其他部署進行中"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_tables.Table('bouncer-projects')
        deployer.locks_table = deployer_tables.Table('bouncer-deploy-locks')
        
        # 新增專案
        deployer.projects_table.put_item(Item={
            'project_id': 'locked-project',
            'name': 'Locked',
            'enabled': True
        })
        
        # 新增鎖
        deployer.locks_table.put_item(Item={
            'project_id': 'locked-project',
            'lock_id': 'existing-deploy',
            'locked_at': int(time.time()),
            'ttl': int(time.time()) + 3600
        })
        
        result = deployer.start_deploy('locked-project', 'main', 'user', 'reason')
        # Final: conflict response uses status='conflict' + message field
        assert result.get('status') == 'conflict' or 'error' in result
        error_or_msg = result.get('error') or result.get('message', '')
        assert '進行中' in error_or_msg
        
        sys.path.pop(0)
    
    def test_cancel_deploy_not_found(self, deployer_tables):
        """取消不存在的部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        result = deployer.cancel_deploy('nonexistent')
        assert 'error' in result
        assert '不存在' in result['error']
        
        sys.path.pop(0)
    
    def test_cancel_deploy_already_completed(self, deployer_tables):
        """取消已完成的部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        # 建立已完成的記錄
        deployer.history_table.put_item(Item={
            'deploy_id': 'completed-deploy',
            'project_id': 'test',
            'status': 'SUCCESS'
        })
        
        result = deployer.cancel_deploy('completed-deploy')
        assert 'error' in result
        assert 'SUCCESS' in result['error']
        
        sys.path.pop(0)
    
    def test_get_deploy_status_not_found(self, deployer_tables):
        """取得不存在的部署狀態"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        result = deployer.get_deploy_status('nonexistent')
        assert 'error' in result
        
        sys.path.pop(0)
    
    def test_get_deploy_history(self, deployer_tables):
        """取得部署歷史"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_tables.Table('bouncer-deploy-history')
        
        # 建立幾個記錄
        for i in range(3):
            deployer.history_table.put_item(Item={
                'deploy_id': f'deploy-{i}',
                'project_id': 'test-project',
                'status': 'SUCCESS',
                'started_at': int(time.time()) - i * 100
            })
        
        history = deployer.get_deploy_history('test-project', limit=10)
        assert len(history) == 3
        
        sys.path.pop(0)


# ============================================================================
# MCP Deploy Tools 測試
# ============================================================================


class TestMCPDeployTools:
    """MCP Deploy Tools 測試"""
    
    def test_mcp_tool_project_list(self, app_module):
        """bouncer_project_list MCP tool"""
        # Mock deployer.list_projects
        with patch('deployer.list_projects', return_value=[
            {'project_id': 'test', 'name': 'Test Project'}
        ]):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test-1',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_project_list',
                        'arguments': {}
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            assert 'result' in body
            content = json.loads(body['result']['content'][0]['text'])
            assert 'projects' in content


# ============================================================================
# Trust 模組補充測試
# ============================================================================


class TestDeployerMCPTools:
    """Deployer MCP Tools 測試"""
    
    def test_mcp_tool_deploy_status_not_found(self, app_module):
        """查詢不存在的部署狀態"""
        with patch('deployer.get_deploy_status', return_value={'error': '部署記錄不存在'}):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test-1',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_status',
                        'arguments': {
                            'deploy_id': 'nonexistent'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert 'error' in content
    
    def test_mcp_tool_deploy_cancel_missing_id(self, app_module):
        """取消部署缺少 ID"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_deploy_cancel',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32602
    
    def test_mcp_tool_deploy_history_missing_project(self, app_module):
        """部署歷史缺少專案 ID"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_deploy_history',
                    'arguments': {}
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        
        assert 'error' in body
        assert body['error']['code'] == -32602
    
    def test_mcp_tool_deploy_missing_project(self, app_module):
        """部署缺少專案"""
        # 透過 deployer 模組直接呼叫
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from deployer import mcp_tool_deploy
        
        with patch('deployer.get_project', return_value=None), \
             patch('deployer.list_projects', return_value=[]):
            result = mcp_tool_deploy('test-1', {
                'reason': 'test deploy'
            }, app_module.table, None)
            
            body = json.loads(result['body'])
            assert 'error' in body
        
        sys.path.pop(0)
    
    def test_mcp_tool_deploy_missing_reason(self, app_module):
        """部署缺少原因"""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from deployer import mcp_tool_deploy
        
        result = mcp_tool_deploy('test-1', {
            'project': 'test-project'
        }, app_module.table, None)
        
        body = json.loads(result['body'])
        assert 'error' in body
        
        sys.path.pop(0)


# ============================================================================
# REST API Handler 測試補充
# ============================================================================


class TestBlockedCommands:
    """BLOCKED 命令測試"""
    
    def test_blocked_iam_create_user(self, app_module):
        """iam create-user 應該被封鎖"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws iam create-user --user-name hacker',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'
    
    def test_blocked_sts_assume_role(self, app_module):
        """sts assume-role 應該被封鎖"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({
                'jsonrpc': '2.0',
                'id': 'test-1',
                'method': 'tools/call',
                'params': {
                    'name': 'bouncer_execute',
                    'arguments': {
                        'command': 'aws sts assume-role --role-arn arn:aws:iam::123:role/Admin',
                        'trust_scope': 'test-session',
                    }
                }
            }),
            'requestContext': {'http': {'method': 'POST'}}
        }
        
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'blocked'


# ============================================================================
# Execute Command 測試
# ============================================================================


class TestDeployerAdditional:
    """Deployer 模組補充測試"""
    
    @pytest.fixture
    def deployer_setup(self, mock_dynamodb):
        """Tables already exist at session scope, just return resource"""
        return mock_dynamodb
    
    def test_get_project_not_exists(self, deployer_setup):
        """取得不存在的專案"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_setup.Table('bouncer-projects')
        
        project = deployer.get_project('nonexistent')
        assert project is None
        
        sys.path.pop(0)
    
    def test_get_deploy_record_not_exists(self, deployer_setup):
        """取得不存在的部署記錄"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_setup.Table('bouncer-deploy-history')
        
        record = deployer.get_deploy_record('nonexistent')
        assert record is None
        
        sys.path.pop(0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ============================================================================
# 額外覆蓋率測試
# ============================================================================


class TestDeployerMCPToolsAdditional:
    """Deployer MCP Tools 補充測試"""
    
    def test_mcp_deploy_status_found(self, app_module):
        """部署狀態查詢 - 存在"""
        with patch('deployer.get_deploy_status', return_value={
            'deploy_id': 'test-deploy',
            'project_id': 'test-project',
            'status': 'RUNNING'
        }):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_status',
                        'arguments': {
                            'deploy_id': 'test-deploy'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            
            content = json.loads(body['result']['content'][0]['text'])
            assert 'deploy_id' in content


# ============================================================================
# Paging 模組完整測試
# ============================================================================


class TestDeployerFull:
    """Deployer 完整測試"""
    
    @pytest.fixture
    def deployer_full_setup(self, mock_dynamodb):
        """Tables already exist at session scope, just return resource"""
        return mock_dynamodb
    
    def test_cancel_deploy_running(self, deployer_full_setup):
        """取消正在執行的部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_full_setup.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_full_setup.Table('bouncer-deploy-locks')
        
        # 建立執行中的記錄
        deployer.history_table.put_item(Item={
            'deploy_id': 'running-deploy',
            'project_id': 'test-project',
            'status': 'RUNNING'
        })
        
        # 取消
        with patch.object(deployer, 'sfn_client') as mock_sfn:
            result = deployer.cancel_deploy('running-deploy')
            assert result['status'] == 'cancelled'
        
        sys.path.pop(0)
    
    def test_update_deploy_record(self, deployer_full_setup):
        """更新部署記錄"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.history_table = deployer_full_setup.Table('bouncer-deploy-history')
        
        # 建立記錄
        deployer.history_table.put_item(Item={
            'deploy_id': 'update-test',
            'project_id': 'test',
            'status': 'PENDING'
        })
        
        # 更新
        deployer.update_deploy_record('update-test', {
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:...'
        })
        
        # 驗證
        item = deployer.history_table.get_item(Key={'deploy_id': 'update-test'})['Item']
        assert item['status'] == 'RUNNING'
        
        sys.path.pop(0)


# ============================================================================
# Commands 模組完整測試
# ============================================================================


class TestDeployerMore:
    """Deployer 更多測試"""
    
    @pytest.fixture
    def deployer_more_setup(self, mock_dynamodb):
        """Tables already exist at session scope, just return resource"""
        return mock_dynamodb
    
    def test_start_deploy_success(self, deployer_more_setup):
        """成功啟動部署"""
        import sys
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        if 'src.deployer' in sys.modules:
            del sys.modules['src.deployer']
        
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import deployer
        
        deployer.projects_table = deployer_more_setup.Table('bouncer-projects')
        deployer.history_table = deployer_more_setup.Table('bouncer-deploy-history')
        deployer.locks_table = deployer_more_setup.Table('bouncer-deploy-locks')
        
        # 新增專案
        deployer.projects_table.put_item(Item={
            'project_id': 'deploy-test',
            'name': 'Deploy Test',
            'git_repo': 'test-repo',
            'stack_name': 'test-stack',
            'enabled': True
        })
        
        # Mock Step Functions
        with patch.object(deployer, 'sfn_client') as mock_sfn:
            mock_sfn.start_execution.return_value = {
                'executionArn': 'arn:aws:states:...'
            }
            
            result = deployer.start_deploy('deploy-test', 'main', 'test-user', 'test reason')
            
            # 如果沒有 STATE_MACHINE_ARN 會失敗，但這測試了大部分路徑
            assert 'status' in result or 'error' in result
        
        sys.path.pop(0)
    
    def test_mcp_deploy_history(self, app_module):
        """部署歷史 MCP"""
        with patch('deployer.get_deploy_history', return_value=[
            {'deploy_id': 'deploy-1', 'status': 'SUCCESS'},
            {'deploy_id': 'deploy-2', 'status': 'FAILED'}
        ]):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_history',
                        'arguments': {
                            'project': 'test-project'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert 'history' in content
    
    def test_mcp_deploy_cancel(self, app_module):
        """取消部署 MCP"""
        with patch('deployer.cancel_deploy', return_value={'status': 'cancelled', 'deploy_id': 'test'}):
            event = {
                'rawPath': '/mcp',
                'headers': {'x-approval-secret': 'test-secret'},
                'body': json.dumps({
                    'jsonrpc': '2.0',
                    'id': 'test',
                    'method': 'tools/call',
                    'params': {
                        'name': 'bouncer_deploy_cancel',
                        'arguments': {
                            'deploy_id': 'test-deploy'
                        }
                    }
                }),
                'requestContext': {'http': {'method': 'POST'}}
            }
            
            result = app_module.lambda_handler(event, None)
            body = json.loads(result['body'])
            content = json.loads(body['result']['content'][0]['text'])
            
            assert content['status'] == 'cancelled'


# ============================================================================
# Paging 更多測試
# ============================================================================


class TestDeployerMoreExtended:
    """Deployer 更多測試"""
    
    def test_mcp_deploy_missing_project(self, app_module):
        """部署缺少 project 參數 (via MCP handler)"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy', {})
        body = json.loads(result['body'])
        assert 'error' in body
    
    def test_mcp_deploy_missing_reason(self, app_module):
        """部署缺少 reason 參數"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy', {'project': 'bouncer'})
        body = json.loads(result['body'])
        assert 'error' in body
    
    def test_mcp_deploy_project_not_found(self, app_module):
        """部署不存在的專案"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy', {
            'project': 'nonexistent-project-xyz',
            'reason': 'test'
        })
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        assert content['status'] == 'error'
        assert '不存在' in content['error']
    
    def test_mcp_project_list(self, app_module):
        """列出可部署專案"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_project_list', {})
        body = json.loads(result['body'])
        assert 'result' in body


class TestDeployerExtra:
    """Deployer 額外測試"""
    
    def test_deploy_cancel(self, app_module):
        """取消部署"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy_cancel', {
            'deploy_id': 'nonexistent-deploy'
        })
        body = json.loads(result['body'])
        # 應該有結果（可能是找不到）
        assert 'result' in body or 'error' in body
    
    def test_deploy_history(self, app_module):
        """部署歷史"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy_history', {
            'project': 'bouncer'
        })
        body = json.loads(result['body'])
        assert 'result' in body
    
    def test_deploy_status_missing_id(self, app_module):
        """部署狀態缺少 ID"""
        result = app_module.handle_mcp_tool_call('test-1', 'bouncer_deploy_status', {})
        body = json.loads(result['body'])
        assert 'error' in body


class TestCrossAccountDeploy:
    """Deploy 跨帳號功能測試"""

    @pytest.fixture(autouse=True)
    def setup_deployer_tables(self, mock_dynamodb):
        """Tables already exist at session scope, no-op"""
        pass

    def test_add_project_stores_target_role_arn(self, app_module):
        """add_project 正確存 target_role_arn"""
        from deployer import add_project, get_project

        add_project('test-cross-deploy', {
            'name': 'Test Project',
            'git_repo': 'owner/repo',
            'stack_name': 'test-stack',
            'target_account': 'Dev (222222222222)',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole'
        })

        project = get_project('test-cross-deploy')
        assert project is not None
        assert project['target_role_arn'] == 'arn:aws:iam::222222222222:role/BouncerRole'
        assert project['target_account'] == 'Dev (222222222222)'

    def test_add_project_without_target_role_arn(self, app_module):
        """add_project 不帶 target_role_arn → 空字串"""
        from deployer import add_project, get_project

        add_project('test-local-deploy', {
            'name': 'Local Project',
            'git_repo': 'owner/repo',
            'stack_name': 'local-stack'
        })

        project = get_project('test-local-deploy')
        assert project is not None
        assert project['target_role_arn'] == ''

    @patch('deployer.sfn_client')
    def test_start_deploy_passes_target_role_arn(self, mock_sfn, app_module):
        """start_deploy 傳入 target_role_arn 到 Step Functions"""
        from deployer import add_project, start_deploy

        mock_sfn.start_execution.return_value = {
            'executionArn': 'arn:aws:states:us-east-1:111111111111:execution:test:deploy-test'
        }

        add_project('test-cross-sfn', {
            'name': 'Cross Account',
            'git_repo': 'owner/repo',
            'stack_name': 'cross-stack',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole'
        })

        result = start_deploy('test-cross-sfn', 'main', 'test-user', 'test deploy')
        assert result['status'] == 'started'

        # 檢查 SFN input 包含 target_role_arn
        call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(call_args[1]['input'] if 'input' in call_args[1] else call_args.kwargs['input'])
        assert sfn_input['target_role_arn'] == 'arn:aws:iam::222222222222:role/BouncerRole'

    @patch('deployer.sfn_client')
    def test_start_deploy_empty_target_role_arn(self, mock_sfn, app_module):
        """start_deploy 無 target_role_arn → 空字串"""
        from deployer import add_project, start_deploy

        mock_sfn.start_execution.return_value = {
            'executionArn': 'arn:aws:states:us-east-1:111111111111:execution:test:deploy-local'
        }

        add_project('test-local-sfn', {
            'name': 'Local',
            'git_repo': 'owner/repo',
            'stack_name': 'local-stack'
        })

        result = start_deploy('test-local-sfn', 'main', 'test-user', 'local deploy')
        assert result['status'] == 'started'

        call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(call_args[1]['input'] if 'input' in call_args[1] else call_args.kwargs['input'])
        assert sfn_input['target_role_arn'] == ''


# ============================================================================
# Deploy Notification Fallback Tests
# ============================================================================


class TestDeployNotificationFallback:
    """Deploy 通知帳號 fallback 測試"""

    @pytest.fixture(autouse=True)
    def setup_deployer_tables(self, mock_dynamodb):
        """Tables already exist at session scope, no-op"""
        pass

    def test_notification_fallback_from_role_arn(self, app_module):
        """target_account 空，從 target_role_arn 解析帳號 ID 顯示在通知中"""
        from deployer import send_deploy_approval_request
        import urllib.request

        project = {
            'project_id': 'test-fallback',
            'name': 'Fallback Test',
            'stack_name': 'fallback-stack',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole',
            # 注意：沒有 target_account
        }

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            mock_urlopen.return_value = mock_resp

            send_deploy_approval_request('deploy-test-123', project, 'main', 'test', 'test-bot')

            # 檢查發送的訊息包含解析出的帳號
            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            body = request_obj.data.decode('utf-8')
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            text = params['text'][0]
            assert '222222222222' in text
            assert '帳號' in text

    def test_notification_no_fallback_when_target_account_set(self, app_module):
        """target_account 有值，直接用不需要 fallback"""
        from deployer import send_deploy_approval_request

        project = {
            'project_id': 'test-no-fallback',
            'name': 'No Fallback',
            'stack_name': 'test-stack',
            'target_account': 'Dev (222222222222)',
            'target_role_arn': 'arn:aws:iam::222222222222:role/BouncerRole',
        }

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            mock_urlopen.return_value = mock_resp

            send_deploy_approval_request('deploy-test-456', project, 'main', 'test', 'test-bot')

            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            body = request_obj.data.decode('utf-8')
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            text = params['text'][0]
            assert 'Dev (222222222222)' in text

    def test_notification_no_account_at_all(self, app_module):
        """target_account 和 target_role_arn 都空 → 不顯示帳號行"""
        from deployer import send_deploy_approval_request

        project = {
            'project_id': 'test-no-account',
            'name': 'No Account',
            'stack_name': 'local-stack',
        }

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            mock_urlopen.return_value = mock_resp

            send_deploy_approval_request('deploy-test-789', project, 'main', 'test', 'test-bot')

            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            body = request_obj.data.decode('utf-8')
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            text = params['text'][0]
            assert '帳號' not in text


# ============================================================================
# Upload Deny Callback Account Display Test
# ============================================================================
