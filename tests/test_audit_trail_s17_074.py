"""
test_audit_trail_s17_074.py — Audit trail fields in DDB (#74)

Tests that when a command callback is approved via Telegram webhook,
the DynamoDB item is written with audit fields:
  - approved_by  (Telegram user_id string)
  - approved_at  (Unix timestamp int)
  - source_ip    (Telegram server IP from API GW event)
  - duration_ms  (approval duration in milliseconds)

Also tests:
  - source_ip is empty string when API GW event has no identity
  - deny path does NOT write source_ip / approved_by audit fields
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock


def _make_webhook_event(callback_data, user_id=999999999,
                        message_id=1001, source_ip='149.154.167.220'):
    body = {
        'callback_query': {
            'id': 'cbtest123',
            'from': {'id': user_id},
            'data': callback_data,
            'message': {'message_id': message_id},
        }
    }
    event = {
        'rawPath': '/webhook',
        'headers': {},
        'body': json.dumps(body),
        'requestContext': {
            'http': {'method': 'POST'},
            'identity': {'sourceIp': source_ip},
        },
    }
    return event


def _put_pending_command(table, request_id, command='aws s3 ls',
                         source='test-source', trust_scope='test-scope',
                         account_id='111111111111'):
    table.put_item(Item={
        'request_id': request_id,
        'command': command,
        'reason': 'unit test',
        'source': source,
        'trust_scope': trust_scope,
        'account_id': account_id,
        'account_name': 'Test Account',
        'status': 'pending_approval',
        'created_at': int(time.time()) - 5,
        'ttl': int(time.time()) + 600,
        'action': 'execute',
        'assume_role': None,
    })


class TestAuditTrailApprove:

    @patch('callbacks.execute_command', return_value='s3://bucket/key\n')
    @patch('callbacks.store_paged_output', return_value={'result': 'ok', 'paged': False})
    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('callbacks.send_telegram_message_silent')
    def test_approve_writes_audit_fields(
        self, mock_send, mock_update, mock_answer, mock_paged, mock_exec,
        app_module,
    ):
        """Approving a command writes approved_by, approved_at, source_ip, duration_ms."""
        request_id = 'audit-test-approve-001'
        source_ip = '149.154.167.220'

        _put_pending_command(app_module.table, request_id)

        event = _make_webhook_event('approve:' + request_id, source_ip=source_ip)
        result = app_module.lambda_handler(event, None)

        assert result['statusCode'] == 200, result

        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None, 'Item missing from DDB'

        assert item['status'] == 'approved'
        assert str(item['approved_by']) == '999999999', 'approved_by wrong: %s' % item.get('approved_by')
        assert 'approved_at' in item, 'approved_at missing'
        assert item['source_ip'] == source_ip, 'source_ip wrong: %s' % item.get('source_ip')
        assert 'duration_ms' in item, 'duration_ms missing'
        assert int(item['duration_ms']) >= 0, 'duration_ms should be >= 0: %s' % item['duration_ms']

    @patch('callbacks.execute_command', return_value='output\n')
    @patch('callbacks.store_paged_output', return_value={'result': 'ok', 'paged': False})
    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('callbacks.send_telegram_message_silent')
    def test_approve_source_ip_empty_when_missing(
        self, mock_send, mock_update, mock_answer, mock_paged, mock_exec,
        app_module,
    ):
        """source_ip is empty string when API GW event has no identity block."""
        request_id = 'audit-test-approve-noip-002'

        _put_pending_command(app_module.table, request_id)

        body = {
            'callback_query': {
                'id': 'cb-noip',
                'from': {'id': 999999999},
                'data': 'approve:' + request_id,
                'message': {'message_id': 1002},
            }
        }
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps(body),
            'requestContext': {'http': {'method': 'POST'}},
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200

        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert item.get('source_ip', '') == '', \
            'source_ip should be empty when not in event, got: %s' % item.get('source_ip')


class TestAuditTrailApprove_FunctionURL:

    @patch('callbacks.execute_command', return_value='result\n')
    @patch('callbacks.store_paged_output', return_value={'result': 'ok', 'paged': False})
    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('callbacks.send_telegram_message_silent')
    def test_approve_source_ip_function_url_format(
        self, mock_send, mock_update, mock_answer, mock_paged, mock_exec,
        app_module,
    ):
        """source_ip is read from requestContext.http.sourceIp (Function URL format)."""
        request_id = 'audit-test-fnurl-003'
        source_ip = '91.108.56.100'

        _put_pending_command(app_module.table, request_id)

        body = {
            'callback_query': {
                'id': 'cb-fnurl',
                'from': {'id': 999999999},
                'data': 'approve:' + request_id,
                'message': {'message_id': 1003},
            }
        }
        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps(body),
            'requestContext': {
                'http': {'method': 'POST', 'sourceIp': source_ip},
            },
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200

        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert item.get('source_ip') == source_ip, \
            'Expected source_ip=%s, got: %s' % (source_ip, item.get('source_ip'))


class TestAuditTrailDeny:

    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    def test_deny_does_not_write_approved_by(
        self, mock_update, mock_answer, app_module,
    ):
        """Denying a request does NOT write approved_by or source_ip audit fields."""
        request_id = 'audit-test-deny-004'

        _put_pending_command(app_module.table, request_id)

        event = _make_webhook_event('deny:' + request_id, source_ip='149.154.167.220')
        result = app_module.lambda_handler(event, None)

        assert result['statusCode'] == 200

        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert item['status'] == 'denied'
        assert 'approved_by' not in item, \
            'approved_by should not be set on denied items, got: %s' % item.get('approved_by')
        assert 'source_ip' not in item, \
            'source_ip should not be set on denied items, got: %s' % item.get('source_ip')


class TestAuditTrailDurationMs:

    @patch('callbacks.execute_command', return_value='output\n')
    @patch('callbacks.store_paged_output', return_value={'result': 'ok', 'paged': False})
    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('callbacks.send_telegram_message_silent')
    def test_duration_ms_is_positive_and_reasonable(
        self, mock_send, mock_update, mock_answer, mock_paged, mock_exec,
        app_module,
    ):
        """duration_ms >= 0 and < 3600000 (1 hour in ms)."""
        request_id = 'audit-test-duration-005'

        _put_pending_command(app_module.table, request_id)

        event = _make_webhook_event('approve:' + request_id)
        app_module.lambda_handler(event, None)

        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        duration = int(item.get('duration_ms', -1))
        assert duration >= 0, 'duration_ms should be >= 0, got %s' % duration
        assert duration < 3_600_000, 'duration_ms too large (>1h): %s' % duration


class TestHandleCommandCallbackDirectly:

    @patch('callbacks.execute_command', return_value='aws output\n')
    @patch('callbacks.store_paged_output', return_value={'result': 'ok', 'paged': False})
    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('callbacks.send_telegram_message_silent')
    def test_source_ip_stored_via_kwarg(
        self, mock_send, mock_update, mock_answer, mock_paged, mock_exec,
        app_module,
    ):
        """handle_command_callback accepts source_ip kwarg and stores it in DDB."""
        import callbacks as cb_mod

        request_id = 'audit-direct-006'
        source_ip = '91.108.4.44'

        _put_pending_command(app_module.table, request_id)
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']

        cb_mod.handle_command_callback(
            'approve', request_id, item, 9001, 'cb-direct', '999999999',
            source_ip=source_ip,
        )

        stored = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert stored is not None
        assert stored.get('source_ip') == source_ip
        assert str(stored.get('approved_by')) == '999999999'

    @patch('callbacks.execute_command', return_value='output\n')
    @patch('callbacks.store_paged_output', return_value={'result': 'ok', 'paged': False})
    @patch('callbacks.answer_callback')
    @patch('callbacks.update_message')
    @patch('callbacks.send_telegram_message_silent')
    def test_source_ip_default_empty(
        self, mock_send, mock_update, mock_answer, mock_paged, mock_exec,
        app_module,
    ):
        """handle_command_callback defaults source_ip='' when not provided."""
        import callbacks as cb_mod

        request_id = 'audit-direct-007'

        _put_pending_command(app_module.table, request_id)
        item = app_module.table.get_item(Key={'request_id': request_id})['Item']

        cb_mod.handle_command_callback(
            'approve', request_id, item, 9002, 'cb-direct2', '999999999',
        )

        stored = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert stored is not None
        assert stored.get('source_ip', '') == ''
