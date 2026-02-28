"""
test_expires_at_display.py — Sprint 6-002: expires_at 顯示測試

覆蓋：
1. send_batch_upload_notification 通知含「後過期」
2. send_grant_request_notification 通知含「審批期限」
3. 過期 grant callback 回「⏰ 此請求已過期」
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock, call


# ============================================================================
# 1. send_batch_upload_notification — 含過期時間
# ============================================================================

class TestBatchUploadNotificationExpiry:
    """send_batch_upload_notification 應顯示「後過期」"""

    @patch('telegram.send_telegram_message')
    def test_default_timeout_shows_5_minutes(self, mock_send, app_module):
        """預設 UPLOAD_TIMEOUT (300s) 顯示「5 分鐘後過期」"""
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_batch_upload_notification(
            batch_id='batch-001',
            file_count=3,
            total_size=1024,
            ext_counts={'HTML': 2, 'CSS': 1},
            reason='test reason',
            source='TestBot',
        )

        assert 'text' in captured
        assert '後過期' in captured['text'], f"Expected '後過期' in: {captured['text']}"
        assert '5 分鐘' in captured['text'], f"Expected '5 分鐘' in: {captured['text']}"

    @patch('telegram.send_telegram_message')
    def test_explicit_timeout_120s_shows_2_minutes(self, mock_send, app_module):
        """明確傳入 timeout=120 顯示「2 分鐘後過期」"""
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_batch_upload_notification(
            batch_id='batch-002',
            file_count=1,
            total_size=512,
            ext_counts={'TXT': 1},
            reason='test',
            source='TestBot',
            timeout=120,
        )

        assert '2 分鐘後過期' in captured['text'], f"Got: {captured['text']}"

    @patch('telegram.send_telegram_message')
    def test_timeout_seconds_under_60(self, mock_send, app_module):
        """timeout < 60 秒時顯示秒數"""
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_batch_upload_notification(
            batch_id='batch-003',
            file_count=1,
            total_size=100,
            ext_counts={'JS': 1},
            reason='test',
            source='TestBot',
            timeout=30,
        )

        assert '30 秒後過期' in captured['text'], f"Got: {captured['text']}"

    @patch('telegram.send_telegram_message')
    def test_batch_id_still_present(self, mock_send, app_module):
        """加入過期時間後，batch_id 仍存在於通知中"""
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_batch_upload_notification(
            batch_id='batch-check-id',
            file_count=2,
            total_size=2048,
            ext_counts={'PNG': 2},
            reason='test',
            source='TestBot',
        )

        assert 'batch-check-id' in captured['text']
        assert '後過期' in captured['text']


# ============================================================================
# 2. send_grant_request_notification — 含審批期限
# ============================================================================

class TestGrantNotificationExpiry:
    """send_grant_request_notification 應顯示「審批期限：5 分鐘」"""

    @patch('telegram.send_telegram_message')
    def test_grant_notification_contains_approval_deadline(self, mock_send, app_module):
        """grant 審批通知含「審批期限」"""
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_grant_request_notification(
            grant_id='grant-test-001',
            commands_detail=[
                {'command': 'aws s3 ls', 'category': 'grantable'},
            ],
            reason='test reason',
            source='TestAgent',
            account_id='111111111111',
            ttl_minutes=30,
        )

        assert 'text' in captured, "No text captured"
        assert '審批期限' in captured['text'], f"Expected '審批期限' in: {captured['text']}"

    @patch('telegram.send_telegram_message')
    def test_grant_notification_shows_5_minutes(self, mock_send, app_module):
        """grant 審批期限為 GRANT_APPROVAL_TIMEOUT (300s = 5 分鐘)"""
        from constants import GRANT_APPROVAL_TIMEOUT
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_grant_request_notification(
            grant_id='grant-test-002',
            commands_detail=[
                {'command': 'aws s3 ls', 'category': 'grantable'},
                {'command': 'aws s3 rm s3://bucket/key', 'category': 'requires_individual'},
            ],
            reason='batch ops',
            source='Agent',
            account_id='111111111111',
            ttl_minutes=60,
        )

        expected_minutes = GRANT_APPROVAL_TIMEOUT // 60
        assert str(expected_minutes) in captured['text'], f"Expected '{expected_minutes}' in: {captured['text']}"
        assert '審批期限' in captured['text']

    @patch('telegram.send_telegram_message')
    def test_grant_id_still_present(self, mock_send, app_module):
        """加入審批期限後，grant_id 仍存在"""
        captured = {}

        def capture(text, keyboard=None):
            captured['text'] = text
            return {'ok': True}

        mock_send.side_effect = capture

        import notifications as notif
        notif.send_grant_request_notification(
            grant_id='grant-id-check',
            commands_detail=[
                {'command': 'aws s3 ls', 'category': 'grantable'},
            ],
            reason='test',
            source='Agent',
            account_id='111111111111',
            ttl_minutes=30,
        )

        assert 'grant-id-check' in captured['text']
        assert '審批期限' in captured['text']


# ============================================================================
# 3. 過期 grant callback — 回「⏰ 此請求已過期」
# ============================================================================

class TestGrantExpiredCallback:
    """過期 grant 點擊按鈕應回「⏰ 此請求已過期」"""

    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_expired_grant_approve_all_returns_expired(
        self, mock_update, mock_answer, app_module
    ):
        """過期 grant 點 approve_all → ⏰ 此請求已過期"""
        grant_id = 'grant-expired-001'
        app_module.table.put_item(Item={
            'request_id': grant_id,
            'type': 'grant_session',
            'action': 'grant_session',
            'status': 'pending_approval',
            'source': 'test-agent',
            'account_id': '111111111111',
            'reason': 'test',
            'ttl': int(time.time()) - 100,  # 已過期
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-grant-exp-001',
                    'from': {'id': 999999999},
                    'data': f'grant_approve_all:{grant_id}',
                    'message': {'message_id': 100},
                }
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called_with('cb-grant-exp-001', '⏰ 此請求已過期')

    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_expired_grant_approve_safe_returns_expired(
        self, mock_update, mock_answer, app_module
    ):
        """過期 grant 點 approve_safe → ⏰ 此請求已過期"""
        grant_id = 'grant-expired-002'
        app_module.table.put_item(Item={
            'request_id': grant_id,
            'type': 'grant_session',
            'action': 'grant_session',
            'status': 'pending_approval',
            'source': 'test-agent',
            'account_id': '111111111111',
            'reason': 'test',
            'ttl': int(time.time()) - 200,  # 已過期
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-grant-exp-002',
                    'from': {'id': 999999999},
                    'data': f'grant_approve_safe:{grant_id}',
                    'message': {'message_id': 101},
                }
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called_with('cb-grant-exp-002', '⏰ 此請求已過期')

    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_expired_grant_deny_returns_expired(
        self, mock_update, mock_answer, app_module
    ):
        """過期 grant 點 deny → ⏰ 此請求已過期"""
        grant_id = 'grant-expired-003'
        app_module.table.put_item(Item={
            'request_id': grant_id,
            'type': 'grant_session',
            'action': 'grant_session',
            'status': 'pending_approval',
            'source': 'test-agent',
            'account_id': '111111111111',
            'reason': 'test',
            'ttl': int(time.time()) - 50,  # 已過期
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-grant-exp-003',
                    'from': {'id': 999999999},
                    'data': f'grant_deny:{grant_id}',
                    'message': {'message_id': 102},
                }
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_answer.assert_called_with('cb-grant-exp-003', '⏰ 此請求已過期')

    @patch('app.handle_grant_approve_all')
    @patch('app.answer_callback')
    def test_non_expired_grant_proceeds_to_handler(
        self, mock_answer, mock_handler, app_module
    ):
        """未過期 grant → 正常進入 handle_grant_approve_all"""
        from utils import response as _response
        mock_handler.return_value = _response(200, {'ok': True, 'handled': True})

        grant_id = 'grant-valid-001'
        app_module.table.put_item(Item={
            'request_id': grant_id,
            'type': 'grant_session',
            'action': 'grant_session',
            'status': 'pending_approval',
            'source': 'test-agent',
            'account_id': '111111111111',
            'reason': 'test',
            'ttl': int(time.time()) + 300,  # 未過期
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-grant-valid-001',
                    'from': {'id': 999999999},
                    'data': f'grant_approve_all:{grant_id}',
                    'message': {'message_id': 200},
                }
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        # Should NOT have called the expired toast
        for c in mock_answer.call_args_list:
            assert '⏰ 此請求已過期' not in str(c), "Should not have sent expiry message for valid grant"
        # Handler should have been called
        mock_handler.assert_called_once()

    @patch('app.answer_callback')
    @patch('app.update_message')
    def test_expired_grant_updates_message(
        self, mock_update, mock_answer, app_module
    ):
        """過期 grant → update_message 被呼叫且移除按鈕"""
        grant_id = 'grant-expired-msg-001'
        app_module.table.put_item(Item={
            'request_id': grant_id,
            'type': 'grant_session',
            'action': 'grant_session',
            'status': 'pending_approval',
            'source': 'test-agent',
            'account_id': '111111111111',
            'reason': 'test',
            'ttl': int(time.time()) - 10,
        })

        event = {
            'rawPath': '/webhook',
            'headers': {},
            'body': json.dumps({
                'callback_query': {
                    'id': 'cb-grant-msg-001',
                    'from': {'id': 999999999},
                    'data': f'grant_approve_all:{grant_id}',
                    'message': {'message_id': 300},
                }
            }),
            'requestContext': {'http': {'method': 'POST'}},
        }

        result = app_module.lambda_handler(event, None)
        assert result['statusCode'] == 200
        mock_update.assert_called_once()
        # Verify remove_buttons=True
        _, kwargs = mock_update.call_args
        assert kwargs.get('remove_buttons') is True or (
            len(mock_update.call_args[0]) >= 3 and mock_update.call_args[0][2] is True
        )
