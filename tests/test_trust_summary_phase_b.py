"""
test_trust_summary_phase_b.py — Sprint 9-007 Phase B 測試

覆蓋範圍：
1. send_trust_session_summary() end_reason='expiry' — 標頭顯示「自動到期」
2. send_trust_session_summary() end_reason='revoke' (default) — 標頭顯示「手動撤銷」
3. handle_trust_expiry() — 呼叫 summary + 標記 summary_sent=True
4. handle_trust_expiry() — summary_sent=True 時跳過（防重複）
5. revoke_trust callback — summary_sent=True 時跳過（防重複）
"""

import sys
import os
import json
import time
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws
import boto3

# Ensure src/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Sprint 58 s58-001: Use centralized module list from _module_list (not conftest — xdist compat)
from _module_list import BOUNCER_MODS


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope='module')
def dynamodb_table():
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='bouncer-test-trust-phase-b',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'source', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'source-created-index',
                    'KeySchema': [
                        {'AttributeName': 'source', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                }
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
def app_mod(dynamodb_table):
    # Sprint 58 s58-001: Use centralized BOUNCER_MODS from conftest
    for mod in list(sys.modules.keys()):
        if mod in BOUNCER_MODS:
            del sys.modules[mod]
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['TABLE_NAME'] = 'bouncer-test-trust-phase-b'
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

    # Reset trust module cache to avoid moto isolation issues
    try:
        import trust as _trust_mod
        _trust_mod._table = None
    except Exception:
        pass


@pytest.fixture
def notifications_mod():
    for mod in list(sys.modules.keys()):
        if mod == 'notifications':
            del sys.modules[mod]
    import notifications
    yield notifications


# ===========================================================================
# 1. send_trust_session_summary — end_reason param
# ===========================================================================

class TestSendTrustSessionSummaryEndReason:

    def _make_trust_item(self, commands=None, created_offset_secs=300):
        now = int(time.time())
        return {
            'request_id': 'trust-phase-b-test-001',
            'type': 'trust_session',
            'created_at': now - created_offset_secs,
            'expires_at': now + 300,
            'commands_executed': commands or [],
        }

    def test_expiry_header_no_commands(self, notifications_mod):
        with patch('telegram.send_message_with_entities') as mock_silent:
            trust_item = self._make_trust_item(commands=[])
            notifications_mod.send_trust_session_summary(trust_item, end_reason='expiry')
            mock_silent.assert_called_once()
            msg = mock_silent.call_args[0][0]
            assert '\u81ea\u52d5\u5230\u671f' in msg  # 自動到期
            assert '\u624b\u52d5\u64a4\u92b7' not in msg  # not 手動撤銷

    def test_revoke_header_no_commands(self, notifications_mod):
        with patch('telegram.send_message_with_entities') as mock_silent:
            trust_item = self._make_trust_item(commands=[])
            notifications_mod.send_trust_session_summary(trust_item, end_reason='revoke')
            mock_silent.assert_called_once()
            msg = mock_silent.call_args[0][0]
            assert '\u624b\u52d5\u64a4\u92b7' in msg  # 手動撤銷
            assert '\u81ea\u52d5\u5230\u671f' not in msg  # not 自動到期

    def test_default_end_reason_is_revoke(self, notifications_mod):
        with patch('telegram.send_message_with_entities') as mock_silent:
            trust_item = self._make_trust_item(commands=[])
            notifications_mod.send_trust_session_summary(trust_item)
            mock_silent.assert_called_once()
            msg = mock_silent.call_args[0][0]
            assert '\u624b\u52d5\u64a4\u92b7' in msg  # 手動撤銷 (default)

    def test_expiry_header_with_commands(self, notifications_mod):
        now = int(time.time())
        cmds = [{'cmd': 'aws s3 ls', 'ts': now - 100, 'success': True}]
        with patch('telegram.send_message_with_entities') as mock_silent:
            trust_item = self._make_trust_item(commands=cmds)
            notifications_mod.send_trust_session_summary(trust_item, end_reason='expiry')
            mock_silent.assert_called_once()
            msg = mock_silent.call_args[0][0]
            assert '\u81ea\u52d5\u5230\u671f' in msg  # 自動到期
            assert '\u57f7\u884c\u4e861\u500b\u547d\u4ee4' in msg or '\u57f7\u884c\u4e86 1 \u500b\u547d\u4ee4' in msg  # 執行了 1 個命令

    def test_revoke_header_with_commands(self, notifications_mod):
        now = int(time.time())
        cmds = [{'cmd': 'aws ec2 describe-instances', 'ts': now - 50, 'success': False}]
        with patch('telegram.send_message_with_entities') as mock_silent:
            trust_item = self._make_trust_item(commands=cmds)
            notifications_mod.send_trust_session_summary(trust_item, end_reason='revoke')
            mock_silent.assert_called_once()
            msg = mock_silent.call_args[0][0]
            assert '\u624b\u52d5\u64a4\u92b7' in msg  # 手動撤銷
            assert '1 \u500b\u5931\u6557' in msg  # 1 個失敗


# ===========================================================================
# 2. handle_trust_expiry — calls summary + marks summary_sent
# ===========================================================================

class TestHandleTrustExpiry:

    def _make_expiry_event(self, trust_id):
        return {
            'source': 'bouncer-scheduler',
            'action': 'trust_expiry',
            'trust_id': trust_id,
        }

    def _seed_trust(self, dynamodb_table, trust_id, commands=None, summary_sent=False):
        now = int(time.time())
        item = {
            'request_id': trust_id,
            'type': 'trust_session',
            'trust_scope': 'test-scope',
            'source': 'Test Bot',
            'bound_source': 'Test Bot',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': now - 300,
            'expires_at': now - 1,  # already expired
            'command_count': len(commands or []),
        }
        if commands:
            item['commands_executed'] = commands
        if summary_sent:
            item['summary_sent'] = True
        dynamodb_table.put_item(Item=item)

    def test_expiry_sends_summary_with_expiry_header(self, app_mod, dynamodb_table):
        trust_id = 'trust-expiry-send-001'
        now = int(time.time())
        self._seed_trust(dynamodb_table, trust_id, commands=[
            {'cmd': 'aws s3 ls', 'ts': now - 200, 'success': True},
            {'cmd': 'aws ec2 describe-instances', 'ts': now - 100, 'success': True},
        ])

        with patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.send_telegram_message'), \
             patch('metrics.emit_metric'):
            result = app_mod.lambda_handler(self._make_expiry_event(trust_id), {})

        assert result['statusCode'] == 200
        # At least one call must contain the expiry header
        summary_calls = [
            c for c in mock_silent.call_args_list
            if '\u81ea\u52d5\u5230\u671f' in (c[0][0] if c[0] else '')
        ]
        assert len(summary_calls) >= 1, "Expected summary with '自動到期' header"

    def test_expiry_marks_summary_sent_in_ddb(self, app_mod, dynamodb_table):
        trust_id = 'trust-expiry-mark-001'
        self._seed_trust(dynamodb_table, trust_id)

        with patch('telegram.send_message_with_entities'), \
             patch('telegram.send_telegram_message'), \
             patch('metrics.emit_metric'):
            app_mod.lambda_handler(self._make_expiry_event(trust_id), {})

        item = dynamodb_table.get_item(Key={'request_id': trust_id}).get('Item')
        assert item is not None
        assert item.get('summary_sent') is True

    def test_expiry_skips_when_summary_already_sent(self, app_mod, dynamodb_table):
        trust_id = 'trust-expiry-skip-001'
        self._seed_trust(dynamodb_table, trust_id, summary_sent=True)

        with patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.send_telegram_message'), \
             patch('metrics.emit_metric'):
            result = app_mod.lambda_handler(self._make_expiry_event(trust_id), {})

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('skipped') is True
        assert body.get('reason') == 'summary_already_sent'
        mock_silent.assert_not_called()

    def test_expiry_no_commands_sends_empty_summary(self, app_mod, dynamodb_table):
        trust_id = 'trust-expiry-empty-001'
        self._seed_trust(dynamodb_table, trust_id, commands=[])

        with patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.send_telegram_message'), \
             patch('metrics.emit_metric'):
            result = app_mod.lambda_handler(self._make_expiry_event(trust_id), {})

        assert result['statusCode'] == 200
        summary_calls = [
            c for c in mock_silent.call_args_list
            if '\u81ea\u52d5\u5230\u671f' in (c[0][0] if c[0] else '')
        ]
        assert len(summary_calls) >= 1

    def test_expiry_missing_trust_id_skips_gracefully(self, app_mod):
        event = {'source': 'bouncer-scheduler', 'action': 'trust_expiry'}
        result = app_mod.lambda_handler(event, {})
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('skipped') is True

    def test_expiry_not_found_skips_gracefully(self, app_mod):
        event = self._make_expiry_event('trust-does-not-exist-9999')
        result = app_mod.lambda_handler(event, {})
        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body.get('skipped') is True


# ===========================================================================
# 3. revoke_trust callback — dedup via summary_sent
# ===========================================================================

class TestRevokeTrustSummaryDedup:

    def _make_revoke_event(self, trust_id):
        return {
            'rawPath': '/webhook',
            'headers': {},
            'requestContext': {'http': {'method': 'POST'}},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-phase-b-001',
                    'from': {'id': 999999999},
                    'data': f'revoke_trust:{trust_id}',
                    'message': {
                        'message_id': 99,
                        'chat': {'id': -1001234567890},
                    },
                }
            }),
        }

    def test_revoke_skips_summary_when_already_sent(self, app_mod, dynamodb_table):
        """If expiry already sent the summary, revoke must not send it again."""
        now = int(time.time())
        trust_id = 'trust-revoke-dedup-001'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'trust_scope': 'test-scope',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': now - 300,
            'expires_at': now + 300,
            'command_count': 1,
            'commands_executed': [
                {'cmd': 'aws s3 ls', 'ts': now - 100, 'success': True},
            ],
            'summary_sent': True,  # expiry already sent the summary
        })

        with patch('telegram.send_telegram_message'), \
             patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.update_message'), \
             patch('telegram.answer_callback'), \
             patch('scheduler_service.get_trust_expiry_notifier') as mock_notifier:
            mock_notifier.return_value = MagicMock()
            result = app_mod.lambda_handler(self._make_revoke_event(trust_id), {})

        assert result['statusCode'] == 200
        # summary already sent → send_trust_session_summary must NOT be called
        mock_silent.assert_not_called()

    def test_revoke_sends_summary_when_not_yet_sent(self, app_mod, dynamodb_table):
        """Normal revoke without prior expiry summary → should still send."""
        now = int(time.time())
        trust_id = 'trust-revoke-normal-001'
        dynamodb_table.put_item(Item={
            'request_id': trust_id,
            'type': 'trust_session',
            'trust_scope': 'test-scope-normal',
            'account_id': '111111111111',
            'approved_by': '999999999',
            'created_at': now - 300,
            'expires_at': now + 300,
            'command_count': 1,
            'commands_executed': [
                {'cmd': 'aws s3 ls', 'ts': now - 100, 'success': True},
            ],
            # summary_sent NOT set
        })

        with patch('telegram.send_telegram_message'), \
             patch('telegram.send_message_with_entities') as mock_silent, \
             patch('telegram.update_message'), \
             patch('telegram.answer_callback'), \
             patch('scheduler_service.get_trust_expiry_notifier') as mock_notifier:
            mock_notifier.return_value = MagicMock()
            result = app_mod.lambda_handler(self._make_revoke_event(trust_id), {})

        assert result['statusCode'] == 200
        mock_silent.assert_called_once()
        msg = mock_silent.call_args[0][0]
        assert '\u624b\u52d5\u64a4\u92b7' in msg  # 手動撤銷
        assert '\u57f7\u884c\u4e86 1 \u500b\u547d\u4ee4' in msg  # 執行了 1 個命令
