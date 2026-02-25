"""
Security Sprint Tests — SEC-003/006/007/008/009/011/013
每個修復至少 2 個 unit tests (正常路徑 + 邊界/攻擊路徑)
"""

import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Set env before any AWS calls
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
os.environ['AWS_SECURITY_TOKEN'] = 'testing'
os.environ['AWS_SESSION_TOKEN'] = 'testing'
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('ACCOUNTS_TABLE_NAME', 'bouncer-accounts')
os.environ.setdefault('TRUSTED_ACCOUNT_IDS', '111111111111,222222222222')
os.environ.setdefault('RATE_LIMIT_ENABLED', 'true')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')

from moto import mock_aws
import boto3


# ============================================================================
# Helper: create mock DynamoDB
# ============================================================================

def _create_mock_table(dynamodb):
    table = dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[
            {'AttributeName': 'request_id', 'AttributeType': 'S'},
            {'AttributeName': 'status', 'AttributeType': 'S'},
            {'AttributeName': 'created_at', 'AttributeType': 'N'},
            {'AttributeName': 'source', 'AttributeType': 'S'},
        ],
        GlobalSecondaryIndexes=[
            {
                'IndexName': 'status-created-index',
                'KeySchema': [
                    {'AttributeName': 'status', 'KeyType': 'HASH'},
                    {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            },
            {
                'IndexName': 'source-created-index',
                'KeySchema': [
                    {'AttributeName': 'source', 'KeyType': 'HASH'},
                    {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                ],
                'Projection': {'ProjectionType': 'ALL'}
            }
        ],
        BillingMode='PAY_PER_REQUEST'
    )
    table.wait_until_exists()
    return table


# ============================================================================
# SEC-003: Unicode Normalization
# ============================================================================

class TestSEC003UnicodeNormalization:
    """SEC-003: _normalize_command 單元測試 — pure function, no AWS needed"""

    @classmethod
    def setup_class(cls):
        # Import the pure function with db mocked out
        with mock_aws():
            with patch.dict(sys.modules, {
                'db': MagicMock(table=MagicMock()),
                'src.db': MagicMock(table=MagicMock()),
            }):
                import importlib
                for mod in list(sys.modules.keys()):
                    if 'mcp_execute' in mod:
                        del sys.modules[mod]
                import src.mcp_execute as mcp_exec_mod
                cls.normalize = staticmethod(mcp_exec_mod._normalize_command)

    def test_normal_command_unchanged(self):
        cmd = "aws s3 ls s3://my-bucket"
        assert self.normalize(cmd) == cmd

    def test_non_breaking_space_replaced(self):
        cmd = "aws\u00a0s3\u00a0ls"
        result = self.normalize(cmd)
        assert result == "aws s3 ls"
        assert '\u00a0' not in result

    def test_em_space_replaced(self):
        cmd = "aws\u2003ec2\u2003describe-instances"
        result = self.normalize(cmd)
        assert result == "aws ec2 describe-instances"

    def test_zero_width_space_removed(self):
        cmd = "aws\u200b s3 ls"
        result = self.normalize(cmd)
        assert '\u200b' not in result
        assert result == "aws s3 ls"

    def test_zero_width_joiner_removed(self):
        cmd = "aws iam\u200d delete-role"
        result = self.normalize(cmd)
        assert '\u200d' not in result
        assert "delete-role" in result

    def test_bom_removed(self):
        cmd = "\ufeffaws s3 ls"
        result = self.normalize(cmd)
        assert result == "aws s3 ls"

    def test_multiple_spaces_collapsed(self):
        cmd = "aws   s3    ls"
        result = self.normalize(cmd)
        assert result == "aws s3 ls"

    def test_strip_whitespace(self):
        cmd = "  aws s3 ls  "
        result = self.normalize(cmd)
        assert result == "aws s3 ls"

    def test_attack_unicode_bypass(self):
        """攻擊路徑：混合 Unicode 空白嘗試繞過"""
        attack_cmd = "aws\u00a0iam\u2003delete-role\u200b--role-name\u00a0admin"
        result = self.normalize(attack_cmd)
        assert 'iam' in result
        assert 'delete-role' in result
        assert '\u00a0' not in result
        assert '\u2003' not in result
        assert '\u200b' not in result

    def test_empty_command(self):
        assert self.normalize('') == ''

    def test_none_command(self):
        assert self.normalize(None) is None


# ============================================================================
# SEC-006: Rate Limit Fail-Close
# ============================================================================

class TestSEC006RateLimitFailClose:
    """SEC-006: check_rate_limit 在 DynamoDB 故障時應 fail-close"""

    def test_rate_limit_passes_normally(self):
        """正常路徑：rate limit 未超時不拋例外"""
        import src.rate_limit as rl_mod
        from src.rate_limit import check_rate_limit

        original = rl_mod._table
        mock_table = MagicMock()
        mock_table.query.return_value = {'Count': 0}
        rl_mod._table = mock_table
        try:
            check_rate_limit("test-source")  # should not raise
        finally:
            rl_mod._table = original

    def test_rate_limit_fail_close_on_db_error(self):
        """SEC-006: DynamoDB 故障時應 fail-close (raise RateLimitExceeded)"""
        import src.rate_limit as rl_mod
        from src.rate_limit import RateLimitExceeded

        original = rl_mod._table
        mock_table = MagicMock()
        mock_table.query.side_effect = Exception("DynamoDB connection error")
        rl_mod._table = mock_table

        try:
            with pytest.raises(RateLimitExceeded) as exc_info:
                rl_mod.check_rate_limit("test-source")
            assert "Rate limit check failed" in str(exc_info.value)
        finally:
            rl_mod._table = original

    def test_rate_limit_reraises_rate_limit_exceeded(self):
        """RateLimitExceeded 應該正常 re-raise"""
        import src.rate_limit as rl_mod
        from src.rate_limit import RateLimitExceeded

        original = rl_mod._table
        mock_table = MagicMock()
        mock_table.query.return_value = {'Count': 9999}
        rl_mod._table = mock_table

        try:
            with pytest.raises(RateLimitExceeded):
                rl_mod.check_rate_limit("test-source")
        finally:
            rl_mod._table = original

    def test_rate_limit_fail_close_preserves_cause(self):
        """fail-close 應保留原始例外作為 cause"""
        import src.rate_limit as rl_mod
        from src.rate_limit import RateLimitExceeded

        original = rl_mod._table
        original_error = Exception("timeout connecting to DynamoDB")
        mock_table = MagicMock()
        mock_table.query.side_effect = original_error
        rl_mod._table = mock_table

        try:
            with pytest.raises(RateLimitExceeded) as exc_info:
                rl_mod.check_rate_limit("test-source")
            assert exc_info.value.__cause__ is original_error
        finally:
            rl_mod._table = original


# ============================================================================
# SEC-007: Trust Session Command Count Atomicity
# ============================================================================

class TestSEC007TrustCommandCountAtomicity:
    """SEC-007: increment_trust_command_count 原子性"""

    def test_increment_count_success(self):
        """正常路徑：成功增加計數"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            trust_id = 'trust-test-001'
            table.put_item(Item={
                'request_id': trust_id,
                'type': 'trust_session',
                'command_count': 0,
                'expires_at': now + 600,
            })

            import importlib
            import src.trust as trust_mod
            importlib.reload(trust_mod)
            trust_mod._table = table

            from src.trust import increment_trust_command_count
            count = increment_trust_command_count(trust_id)
            assert count == 1

    def test_increment_returns_zero_on_conditional_failure(self):
        """邊界路徑：conditional update 失敗（超限）應 return 0"""
        import src.trust as trust_mod
        from src.trust import increment_trust_command_count

        mock_table = MagicMock()
        conditional_exc = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'Condition failed'}},
            'UpdateItem'
        )
        mock_table.update_item.side_effect = conditional_exc
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = type(conditional_exc)

        original = trust_mod._table
        trust_mod._table = mock_table
        try:
            result = increment_trust_command_count('trust-expired')
            assert result == 0
        finally:
            trust_mod._table = original

    def test_increment_requires_active_status(self):
        """攻擊路徑：過期的 trust session 不能增加計數"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            trust_id = 'trust-expired-001'
            table.put_item(Item={
                'request_id': trust_id,
                'type': 'trust_session',
                'command_count': 0,
                'expires_at': now - 60,  # 已過期
            })

            import importlib
            import src.trust as trust_mod
            importlib.reload(trust_mod)
            trust_mod._table = table

            from src.trust import increment_trust_command_count
            count = increment_trust_command_count(trust_id)
            assert count == 0  # conditional check 失敗 → 0


# ============================================================================
# SEC-008: Compliance JSON Normalize
# ============================================================================

class TestSEC008ComplianceJSONNormalize:
    """SEC-008: JSON payload 正規化防止繞過"""

    def setup_method(self):
        import importlib
        import src.compliance_checker as cc_mod
        importlib.reload(cc_mod)
        from src.compliance_checker import check_compliance, _normalize_json_payload
        self.check_compliance = check_compliance
        self.normalize_json = _normalize_json_payload

    def test_normal_compliance_still_works(self):
        """正常路徑：無 JSON 的命令仍正常合規檢查"""
        cmd = "aws lambda add-permission --function-name test --principal '*'"
        is_compliant, violation = self.check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "L1"

    def test_json_normalized_payload_blocked(self):
        """攻擊路徑：JSON 格式變化（多空白）也能被攔截"""
        cmd = 'aws kms put-key-policy --key-id alias/test --policy \'{ "Principal" :  "*" }\''
        is_compliant, violation = self.check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S2"

    def test_json_normalize_function_normalizes_whitespace(self):
        """_normalize_json_payload 應正規化 JSON 空白"""
        cmd = 'aws iam create-role --assume-role-policy-document \'{ "Principal" : "*" }\''
        result = self.normalize_json(cmd)
        assert '{"Principal":"*"}' in result

    def test_invalid_json_falls_back_to_original(self):
        """無法 parse 的 JSON 片段應 fallback 不影響原始邏輯"""
        cmd = "aws s3 ls {not-valid-json"
        result = self.normalize_json(cmd)
        assert result == cmd

    def test_safe_command_still_passes(self):
        """正常安全命令不被誤攔"""
        cmd = "aws s3 ls s3://my-bucket"
        is_compliant, violation = self.check_compliance(cmd)
        assert is_compliant
        assert violation is None


# ============================================================================
# SEC-009: Grant allow_repeat Dangerous Command Limit
# ============================================================================

class TestSEC009GrantDangerousRepeatLimit:
    """SEC-009: allow_repeat 危險命令最多 3 次"""

    def test_non_dangerous_command_not_limited(self):
        """非危險命令不受 3 次限制"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            table.put_item(Item={
                'request_id': 'grant_test_001',
                'type': 'grant_session',
                'status': 'active',
                'allow_repeat': True,
                'used_commands': {},
                'total_executions': 0,
                'max_total_executions': 50,
            })

            import importlib
            import src.grant as grant_mod
            importlib.reload(grant_mod)
            grant_mod.table = table

            from src.grant import try_use_grant_command
            with patch('src.commands.is_dangerous', return_value=False):
                result = try_use_grant_command('grant_test_001', 'aws s3 ls', allow_repeat=True)
            assert result is True

    def test_dangerous_command_blocked_after_3_times(self):
        """危險命令超過 3 次後 return False"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            dangerous_cmd = 'aws ec2 terminate-instances --instance-ids i-123'
            table.put_item(Item={
                'request_id': 'grant_dangerous_001',
                'type': 'grant_session',
                'status': 'active',
                'allow_repeat': True,
                'used_commands': {dangerous_cmd: 3},
                'total_executions': 3,
                'max_total_executions': 50,
            })

            import importlib
            import src.grant as grant_mod
            importlib.reload(grant_mod)
            grant_mod.table = table

            from src.grant import try_use_grant_command
            with patch('src.commands.is_dangerous', return_value=True):
                result = try_use_grant_command('grant_dangerous_001', dangerous_cmd, allow_repeat=True)
            assert result is False

    def test_dangerous_command_allowed_under_limit(self):
        """危險命令在 3 次以內可以執行"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            dangerous_cmd = 'aws ec2 terminate-instances --instance-ids i-456'
            table.put_item(Item={
                'request_id': 'grant_dangerous_002',
                'type': 'grant_session',
                'status': 'active',
                'allow_repeat': True,
                'used_commands': {dangerous_cmd: 1},
                'total_executions': 1,
                'max_total_executions': 50,
            })

            import importlib
            import src.grant as grant_mod
            importlib.reload(grant_mod)
            grant_mod.table = table

            from src.grant import try_use_grant_command
            with patch('src.commands.is_dangerous', return_value=True):
                result = try_use_grant_command('grant_dangerous_002', dangerous_cmd, allow_repeat=True)
            assert result is True

    def test_allow_repeat_false_not_affected_by_sec009(self):
        """allow_repeat=False 不受 SEC-009 限制（走原本一次性邏輯）"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            dangerous_cmd = 'aws rds delete-db-instance --db-instance-identifier prod'
            table.put_item(Item={
                'request_id': 'grant_no_repeat_001',
                'type': 'grant_session',
                'status': 'active',
                'allow_repeat': False,
                'used_commands': {},
                'total_executions': 0,
                'max_total_executions': 50,
            })

            import importlib
            import src.grant as grant_mod
            importlib.reload(grant_mod)
            grant_mod.table = table

            from src.grant import try_use_grant_command
            result = try_use_grant_command('grant_no_repeat_001', dangerous_cmd, allow_repeat=False)
            assert result is True


# ============================================================================
# SEC-011: REST API Compliance Check
# ============================================================================

class TestSEC011RestAPICompliance:
    """SEC-011: REST API handle_clawdbot_request 補 compliance check"""

    def _make_event(self, command: str, reason: str = 'test reason'):
        return {
            'headers': {'x-approval-secret': 'test-secret'},
            'body': json.dumps({'command': command, 'reason': reason, 'source': 'test'}),
        }

    def test_compliance_violation_returns_400(self):
        """合規違規應返回 400"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            _create_mock_table(dynamodb)

            import importlib
            import src.app as app_mod
            importlib.reload(app_mod)
            app_mod.REQUEST_SECRET = 'test-secret'
            app_mod.ENABLE_HMAC = False

            violation_cmd = "aws lambda add-permission --function-name test --principal '*' --statement-id s"
            event = self._make_event(violation_cmd)

            with patch.object(app_mod, 'table', MagicMock()):
                result = app_mod.handle_clawdbot_request(event)

            assert result['statusCode'] == 400
            body = json.loads(result['body'])
            assert body.get('error') == 'Compliance violation'
            assert 'violations' in body

    def test_safe_command_passes_compliance(self):
        """安全命令通過 compliance check 進入後續流程"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            _create_mock_table(dynamodb)

            import importlib
            import src.app as app_mod
            importlib.reload(app_mod)
            app_mod.REQUEST_SECRET = 'test-secret'
            app_mod.ENABLE_HMAC = False

            safe_cmd = "aws s3 ls"
            event = self._make_event(safe_cmd)

            mock_table = MagicMock()
            mock_table.put_item.return_value = {}

            with patch.object(app_mod, 'table', mock_table), \
                 patch.object(app_mod, 'is_auto_approve', return_value=False), \
                 patch.object(app_mod, 'get_block_reason', return_value=None), \
                 patch.object(app_mod, 'send_approval_request', return_value=True):
                result = app_mod.handle_clawdbot_request(event)

            # Should not be 400 compliance error
            assert result['statusCode'] != 400

    def test_compliance_blocks_iam_principal_star(self):
        """P-S2: IAM Principal:* 被 REST API compliance check 攔截"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            _create_mock_table(dynamodb)

            import importlib
            import src.app as app_mod
            importlib.reload(app_mod)
            app_mod.REQUEST_SECRET = 'test-secret'
            app_mod.ENABLE_HMAC = False

            violation_cmd = 'aws iam update-assume-role-policy --role-name r --policy-document \'{"Principal":"*"}\''
            event = self._make_event(violation_cmd)

            with patch.object(app_mod, 'table', MagicMock()):
                result = app_mod.handle_clawdbot_request(event)

            assert result['statusCode'] == 400
            body = json.loads(result['body'])
            assert 'Compliance' in body.get('error', '')


# ============================================================================
# SEC-013: auto_execute_pending Compliance Check
# ============================================================================

class TestSEC013AutoExecutePendingCompliance:
    """SEC-013: _auto_execute_pending_requests 補 compliance check"""

    def test_compliant_command_executed(self):
        """合規命令正常執行"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            req_id = 'pending-compliance-ok-001'
            table.put_item(Item={
                'request_id': req_id,
                'command': 'aws s3 ls',
                'reason': 'test',
                'source': 'test-agent',
                'trust_scope': 'test-scope',
                'account_id': '123456789012',
                'status': 'pending',
                'created_at': now,
                'ttl': now + 3600,
            })

            import importlib
            import src.callbacks as cb_mod
            import src.db as db_mod
            importlib.reload(cb_mod)
            db_mod.table = table
            cb_mod._db.table = table

            with patch('src.callbacks.execute_command', return_value='OK output') as mock_exec, \
                 patch('src.callbacks.store_paged_output', return_value={'result': 'OK', 'paged': False}), \
                 patch('src.trust.increment_trust_command_count', return_value=1), \
                 patch('src.utils.log_decision'), \
                 patch('src.callbacks.send_trust_auto_approve_notification'), \
                 patch('src.callbacks.emit_metric'):
                cb_mod._auto_execute_pending_requests(
                    'test-scope', '123456789012', None, 'trust-001', 'test-agent'
                )
                mock_exec.assert_called_once()

    def test_non_compliant_command_rejected(self):
        """不合規命令應被標記為 compliance_rejected 並跳過執行"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            now = int(time.time())
            req_id = 'pending-compliance-fail-001'
            table.put_item(Item={
                'request_id': req_id,
                'command': "aws lambda add-permission --function-name f --principal '*' --statement-id s",
                'reason': 'test',
                'source': 'test-agent',
                'trust_scope': 'test-scope-bad',
                'account_id': '123456789012',
                'status': 'pending',
                'created_at': now,
                'ttl': now + 3600,
            })

            import importlib
            import src.callbacks as cb_mod
            import src.db as db_mod
            importlib.reload(cb_mod)
            db_mod.table = table
            cb_mod._db.table = table

            with patch('src.callbacks.execute_command') as mock_exec, \
                 patch('src.callbacks.emit_metric'):
                cb_mod._auto_execute_pending_requests(
                    'test-scope-bad', '123456789012', None, 'trust-002', 'test-agent'
                )

                mock_exec.assert_not_called()

                item = table.get_item(Key={'request_id': req_id}).get('Item', {})
                assert item.get('status') == 'compliance_rejected'
                assert 'compliance_rule' in item

    def test_no_pending_requests_no_crash(self):
        """沒有 pending 請求時不出錯"""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import importlib
            import src.callbacks as cb_mod
            import src.db as db_mod
            importlib.reload(cb_mod)
            db_mod.table = table
            cb_mod._db.table = table

            # 應不拋例外
            cb_mod._auto_execute_pending_requests(
                'nonexistent-scope', '123456789012', None, 'trust-000'
            )
