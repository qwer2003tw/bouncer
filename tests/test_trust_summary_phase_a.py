"""
test_trust_summary_phase_a.py — Sprint 9-007 Phase A 測試

覆蓋範圍：
1. trust.track_command_executed() — 命令追蹤 DDB list_append
2. notifications.send_trust_session_summary() — Telegram 摘要格式
3. app.py revoke_trust callback — 整合：revoke 時觸發摘要
"""

import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws
import boto3

# Ensure src/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope='module')
def dynamodb_table():
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='bouncer-test-trust-summary',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        table.wait_until_exists()
        yield table


@pytest.fixture(autouse=True)
def clear_table(dynamodb_table):
    yield
    scan = dynamodb_table.scan()
    for item in scan.get('Items', []):
        dynamodb_table.delete_item(Key={'request_id': item['request_id']})


@pytest.fixture
def trust_mod(dynamodb_table):
    for mod in list(sys.modules.keys()):
        if mod in ('trust', 'db'):
            del sys.modules[mod]
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('TABLE_NAME', 'bouncer-test-trust-summary')
    import trust
    import db
    trust._table = dynamodb_table
    db.table = dynamodb_table
    yield trust


# ===========================================================================
# 1. track_command_executed
# ===========================================================================

class TestTrackCommandExecuted:

    def test_first_command_creates_list(self, trust_mod, dynamodb_table):
        trust_id = 'trust-test-001'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'created_at': int(time.time()),
        })

        trust_mod.track_command_executed(trust_id, 'aws s3 ls', True)

        item = dynamodb_table.get_item(Key={'request_id': trust_id})['Item']
        assert 'commands_executed' in item
        cmds = item['commands_executed']
        assert len(cmds) == 1
        assert cmds[0]['cmd'] == 'aws s3 ls'
        assert cmds[0]['success'] is True
        assert 'ts' in cmds[0]

    def test_subsequent_commands_append(self, trust_mod, dynamodb_table):
        trust_id = 'trust-test-002'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'created_at': int(time.time()),
        })

        trust_mod.track_command_executed(trust_id, 'aws s3 ls', True)
        trust_mod.track_command_executed(trust_id, 'aws ec2 describe-instances', False)
        trust_mod.track_command_executed(trust_id, 'aws cloudformation list-stacks', True)

        item = dynamodb_table.get_item(Key={'request_id': trust_id})['Item']
        cmds = item['commands_executed']
        assert len(cmds) == 3
        assert cmds[0]['cmd'] == 'aws s3 ls'
        assert cmds[0]['success'] is True
        assert cmds[1]['cmd'] == 'aws ec2 describe-instances'
        assert cmds[1]['success'] is False

    def test_command_truncated_to_100_chars(self, trust_mod, dynamodb_table):
        trust_id = 'trust-test-003'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'created_at': int(time.time()),
        })
        long_cmd = 'aws s3 cp ' + 'x' * 200

        trust_mod.track_command_executed(trust_id, long_cmd, True)

        item = dynamodb_table.get_item(Key={'request_id': trust_id})['Item']
        stored_cmd = item['commands_executed'][0]['cmd']
        assert len(stored_cmd) <= 100

    def test_error_is_swallowed(self, trust_mod):
        mock_table = MagicMock()
        mock_table.update_item.side_effect = Exception('DDB error')
        original_table = trust_mod._table
        trust_mod._table = mock_table
        try:
            trust_mod.track_command_executed('trust-x', 'aws s3 ls', True)
        finally:
            trust_mod._table = original_table


# ===========================================================================
# 2. send_trust_session_summary
# ===========================================================================

class TestSendTrustSessionSummary:

    @pytest.fixture(autouse=True)
    def reload_notifications(self):
        for mod in list(sys.modules.keys()):
            if mod == 'notifications':
                del sys.modules[mod]

    def _make_trust_item(self, commands=None, created_offset_secs=300):
        now = int(time.time())
        return {
            'request_id': 'trust-abc123def456',
            'type': 'trust_session',
            'created_at': now - created_offset_secs,
            'expires_at': now + 300,
            'commands_executed': commands or [],
        }

    def test_no_commands_sends_simple_message(self):
        with patch('telegram.send_message_with_entities') as mock_silent:
            import notifications
            trust_item = self._make_trust_item(commands=[])
            notifications.send_trust_session_summary(trust_item)
            mock_silent.assert_called_once()
            msg = mock_silent.call_args[0][0]
            assert '信任時段結束' in msg
            assert '無命令執行' in msg

    def test_all_success_shows_checkmark(self):
        now = int(time.time())
        cmds = [
            {'cmd': 'aws s3 ls', 'ts': now - 100, 'success': True},
            {'cmd': 'aws ec2 describe-instances', 'ts': now - 80, 'success': True},
        ]
        with patch('telegram.send_message_with_entities') as mock_silent:
            import notifications
            trust_item = self._make_trust_item(commands=cmds)
            notifications.send_trust_session_summary(trust_item)
            msg = mock_silent.call_args[0][0]
            assert '全部成功' in msg
            assert '執行了 2 個命令' in msg

    def test_partial_failure_shows_warning(self):
        now = int(time.time())
        cmds = [
            {'cmd': 'aws s3 ls', 'ts': now - 100, 'success': True},
            {'cmd': 'aws ec2 terminate-instances', 'ts': now - 80, 'success': False},
            {'cmd': 'aws s3 cp file s3://bucket/', 'ts': now - 60, 'success': False},
        ]
        with patch('telegram.send_message_with_entities') as mock_silent:
            import notifications
            trust_item = self._make_trust_item(commands=cmds)
            notifications.send_trust_session_summary(trust_item)
            msg = mock_silent.call_args[0][0]
            assert '2 個失敗' in msg
            assert '執行了 3 個命令' in msg

    def test_duration_in_message(self):
        cmds = [{'cmd': 'aws s3 ls', 'ts': int(time.time()) - 30, 'success': True}]
        with patch('telegram.send_message_with_entities') as mock_silent:
            import notifications
            trust_item = self._make_trust_item(commands=cmds, created_offset_secs=330)
            notifications.send_trust_session_summary(trust_item)
            msg = mock_silent.call_args[0][0]
            assert '分' in msg

    def test_truncates_at_10_commands(self):
        now = int(time.time())
        cmds = [
            {'cmd': f'aws s3 ls s3://bucket-{i}', 'ts': now - i * 5, 'success': True}
            for i in range(15)
        ]
        with patch('telegram.send_message_with_entities') as mock_silent:
            import notifications
            trust_item = self._make_trust_item(commands=cmds)
            notifications.send_trust_session_summary(trust_item)
            msg = mock_silent.call_args[0][0]
            assert '執行了 15 個命令' in msg
            assert '還有 5 個命令' in msg

    def test_exception_does_not_raise(self):
        with patch('telegram.send_message_with_entities', side_effect=Exception('tg error')):
            import notifications
            cmds = [{'cmd': 'aws s3 ls', 'ts': int(time.time()), 'success': True}]
            trust_item = self._make_trust_item(commands=cmds)
            notifications.send_trust_session_summary(trust_item)


# ===========================================================================
# 3. revoke_trust integration
# ===========================================================================

class TestRevokeTrustCallbackSummary:

    _MODS_TO_CLEAR = [
        'app', 'db', 'trust', 'notifications', 'callbacks',
        'mcp_execute', 'mcp_tools', 'telegram', 'commands',
        'mcp_upload', 'mcp_admin', 'mcp_history', 'mcp_confirm',
        'mcp_presigned', 'accounts', 'rate_limit', 'utils',
        'paging', 'smart_approval', 'risk_scorer', 'template_scanner',
        'scheduler_service', 'compliance_checker', 'grant', 'deployer',
        'constants', 'metrics', 'sequence_analyzer', 'help_command',
        'tool_schema',
    ]

    @pytest.fixture
    def app_mod(self, dynamodb_table):
        for mod in list(sys.modules.keys()):
            if mod in self._MODS_TO_CLEAR:
                del sys.modules[mod]
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        os.environ['TABLE_NAME'] = 'bouncer-test-trust-summary'
        os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
        os.environ['APPROVED_CHAT_ID'] = '999999999'
        os.environ['REQUEST_SECRET'] = 'test-secret'
        os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')

        import app
        import db
        app.table = dynamodb_table
        db.table = dynamodb_table

        import trust
        trust._table = dynamodb_table

        yield app

    def _make_revoke_event(self, trust_id):
        import json
        return {
            'rawPath': '/webhook',
            'headers': {},
            'requestContext': {'http': {'method': 'POST'}},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-001',
                    'from': {'id': 999999999},
                    'data': f'revoke_trust:{trust_id}',
                    'message': {
                        'message_id': 42,
                        'chat': {'id': -1001234567890},
                    },
                }
            }),
        }

    def test_revoke_sends_summary_with_commands(self, app_mod, dynamodb_table):
        now = int(time.time())
        trust_id = 'trust-revoke-test-001'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'trust_scope': 'test-scope',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': now - 300,
            'expires_at': now + 300,
            'command_count': 2,
            'commands_executed': [
                {'cmd': 'aws s3 ls', 'ts': now - 200, 'success': True},
                {'cmd': 'aws ec2 describe-instances', 'ts': now - 100, 'success': True},
            ],
        })

        with patch('telegram.send_telegram_message') as mock_send, \
             patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.update_message'), \
             patch('telegram.answer_callback'), \
             patch('scheduler_service.get_trust_expiry_notifier') as mock_notifier:
            mock_notifier.return_value = MagicMock()
            result = app_mod.lambda_handler(self._make_revoke_event(trust_id), {})

        assert result['statusCode'] == 200
        mock_silent.assert_called()
        msg = mock_silent.call_args[0][0]
        assert '信任時段結束' in msg
        assert '執行了 2 個命令' in msg
        assert '全部成功' in msg

    def test_revoke_no_commands_sends_empty_summary(self, app_mod, dynamodb_table):
        now = int(time.time())
        trust_id = 'trust-revoke-test-002'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'trust_scope': 'test-scope-2',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': now - 60,
            'expires_at': now + 300,
            'command_count': 0,
        })

        with patch('telegram.send_telegram_message'), \
             patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.update_message'), \
             patch('telegram.answer_callback'), \
             patch('scheduler_service.get_trust_expiry_notifier') as mock_notifier:
            mock_notifier.return_value = MagicMock()
            result = app_mod.lambda_handler(self._make_revoke_event(trust_id), {})

        assert result['statusCode'] == 200
        mock_silent.assert_called()
        msg = mock_silent.call_args[0][0]
        assert '無命令執行' in msg
