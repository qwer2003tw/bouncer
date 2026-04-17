"""
Tests for Sprint 33-004 — sendChatAction typing indicator (#61)

TC01 - handle_command_callback approve → send_chat_action('typing') called before execute
TC02 - handle_command_callback deny   → send_chat_action NOT called
TC03 - send_chat_action raises exception → execute_command still runs (best-effort)
"""
from __future__ import annotations

import os
import sys
import time

import pytest
from unittest.mock import patch
from moto import mock_aws
import boto3

pytestmark = pytest.mark.xdist_group("app_module")



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_env():
    os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
    os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')
    os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
    os.environ.setdefault('REQUEST_SECRET', 'test-secret')
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
    os.environ.setdefault('APPROVED_CHAT_ID', '999999999')
    os.environ.setdefault('MCP_MAX_WAIT', '5')


def _create_table(dynamodb):
    return dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[
            {'AttributeName': 'request_id', 'AttributeType': 'S'},
            {'AttributeName': 'status', 'AttributeType': 'S'},
            {'AttributeName': 'created_at', 'AttributeType': 'N'},
        ],
        GlobalSecondaryIndexes=[
            {
                'IndexName': 'status-created-index',
                'KeySchema': [
                    {'AttributeName': 'status', 'KeyType': 'HASH'},
                    {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                ],
                'Projection': {'ProjectionType': 'ALL'},
            }
        ],
        BillingMode='PAY_PER_REQUEST',
    )


def _reload_callbacks(table):
    """Reload callbacks module with a fresh mock table injected."""
    # Clear cached modules so imports inside callbacks are re-resolved
    for mod in list(sys.modules.keys()):
        if mod in ('callbacks', 'callbacks_command', 'db', 'telegram', 'commands', 'trust', 'paging',
                   'notifications', 'utils', 'rate_limit', 'risk_scorer',
                   'smart_approval', 'constants', 'accounts', 'template_scanner',
                   'scheduler_service', 'src.callbacks', 'src.callbacks_command', 'src.db'):
            del sys.modules[mod]

    import db as db_mod
    import callbacks as cb_mod
    import callbacks_command as cb_cmd_mod
    db_mod.table = table
    cb_mod._db.table = table
    cb_cmd_mod._db.table = table
    return cb_mod


def _make_item(request_id: str, command: str = 'aws s3 ls') -> dict:
    return {
        'request_id': request_id,
        'command': command,
        'status': 'pending',
        'source': 'test-source',
        'reason': 'typing indicator test',
        'trust_scope': 'test-scope',
        'account_id': '123456789012',
        'account_name': 'Test',
        'context': '',
        'created_at': int(time.time()),
    }


def _make_paged_output(result: str = 'ok'):
    from paging import PaginatedOutput
    return PaginatedOutput(
        paged=False, result=result,
        page=1, total_pages=1, output_length=len(result),
    )


# ---------------------------------------------------------------------------
# TC01: approve → send_chat_action('typing') called
# ---------------------------------------------------------------------------

class TestTypingOnApprove:
    """TC01 - approve 路徑必須呼叫 send_chat_action('typing')"""

    def test_approve_calls_send_chat_action_typing(self):
        _setup_env()
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_table(dynamodb)

            req_id = 'typing-approve-001'
            table.put_item(Item=_make_item(req_id))

            cb_mod = _reload_callbacks(table)

            with patch('callbacks_command.send_chat_action') as mock_typing, \
                 patch('callbacks_command.answer_callback'), \
                 patch('callbacks_command.update_message'), \
                 patch('callbacks_command.execute_command', return_value='result') as mock_exec, \
                 patch('callbacks_command.store_paged_output', return_value=_make_paged_output()), \
                 patch('callbacks_command.emit_metric'):

                cb_mod.handle_command_callback(
                    'approve', req_id,
                    _make_item(req_id),
                    999, 'cb-001', 'user-1',
                )

            # send_chat_action must have been called with 'typing'
            mock_typing.assert_called_once_with('typing')
            # execute_command must also have been called
            mock_exec.assert_called_once()

    def test_approve_trust_also_calls_send_chat_action_typing(self):
        """approve_trust 分支同樣要呼叫 send_chat_action"""
        _setup_env()
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_table(dynamodb)

            req_id = 'typing-approve-trust-001'
            table.put_item(Item=_make_item(req_id))

            cb_mod = _reload_callbacks(table)

            with patch('callbacks_command.send_chat_action') as mock_typing, \
                 patch('callbacks_command.answer_callback'), \
                 patch('callbacks_command.update_message'), \
                 patch('callbacks_command.execute_command', return_value='result') as mock_exec, \
                 patch('callbacks_command.store_paged_output', return_value=_make_paged_output()), \
                 patch('callbacks_command.emit_metric'), \
                 patch('callbacks_command._handle_trust_session', return_value=''):

                cb_mod.handle_command_callback(
                    'approve_trust', req_id,
                    _make_item(req_id),
                    999, 'cb-002', 'user-1',
                )

            mock_typing.assert_called_once_with('typing')
            mock_exec.assert_called_once()


# ---------------------------------------------------------------------------
# TC02: deny → send_chat_action NOT called
# ---------------------------------------------------------------------------

class TestNoTypingOnDeny:
    """TC02 - deny 路徑不應呼叫 send_chat_action"""

    def test_deny_does_not_call_send_chat_action(self):
        _setup_env()
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_table(dynamodb)

            req_id = 'typing-deny-001'
            table.put_item(Item=_make_item(req_id))

            cb_mod = _reload_callbacks(table)

            with patch('callbacks_command.send_chat_action') as mock_typing, \
                 patch('callbacks_command.answer_callback'), \
                 patch('callbacks_command.update_message'), \
                 patch('callbacks_command.execute_command') as mock_exec, \
                 patch('callbacks_command.emit_metric'):

                cb_mod.handle_command_callback(
                    'deny', req_id,
                    _make_item(req_id),
                    999, 'cb-003', 'user-1',
                )

            # send_chat_action must NOT have been called
            mock_typing.assert_not_called()
            # execute_command must NOT have been called on deny
            mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# TC03: send_chat_action raises → execute_command still runs (best-effort)
# ---------------------------------------------------------------------------

class TestTypingExceptionIsBestEffort:
    """TC03 - send_chat_action 拋 exception 不能阻止 execute_command 執行"""

    def test_typing_exception_does_not_block_execute(self):
        _setup_env()
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_table(dynamodb)

            req_id = 'typing-exception-001'
            table.put_item(Item=_make_item(req_id))

            cb_mod = _reload_callbacks(table)

            with patch('callbacks_command.send_chat_action', side_effect=Exception('Telegram unavailable')) as mock_typing, \
                 patch('callbacks_command.answer_callback'), \
                 patch('callbacks_command.update_message'), \
                 patch('callbacks_command.execute_command', return_value='result') as mock_exec, \
                 patch('callbacks_command.store_paged_output', return_value=_make_paged_output()), \
                 patch('callbacks_command.emit_metric'):

                # Should not raise
                cb_mod.handle_command_callback(
                    'approve', req_id,
                    _make_item(req_id),
                    999, 'cb-004', 'user-1',
                )

            # typing was attempted
            mock_typing.assert_called_once_with('typing')
            # execute_command still ran despite the exception
            mock_exec.assert_called_once()
