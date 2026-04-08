"""
Regression test for #228: deploy approval 永不過期

Bug: deploy request 的 ttl 設為 7 天（DDB retention），但 callback
檢查 ttl 來判斷審批是否過期，導致審批永不過期。

Fix: 分離 ttl（DDB retention）和 approval_expiry（審批到期），
callback 優先檢查 approval_expiry。
"""
import time
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestRegressionDeployApprovalExpiry:
    """#228: deploy approval should expire after APPROVAL_TIMEOUT_DEFAULT, not 7 days."""

    @patch('callbacks.start_deploy')
    @patch('callbacks.update_message')
    @patch('callbacks.answer_callback')
    @patch('callbacks._update_request_status')
    @patch('callbacks._get_table')
    def test_deploy_callback_rejects_expired_approval(
        self, mock_table, mock_update_status, mock_answer, mock_update_msg, mock_start
    ):
        """approval_expiry 已過期 → callback 應拒絕"""
        from callbacks import handle_deploy_callback

        now = int(time.time())
        item = {
            'request_id': 'req-deploy-001',
            'action': 'deploy',
            'project_id': 'bouncer',
            'project_name': 'Bouncer',
            'branch': 'master',
            'stack_name': 'bouncer-stack',
            'source': 'test',
            'reason': 'test deploy',
            'context': '',
            'status': 'pending_approval',
            'created_at': now - 700,
            'ttl': now + 7 * 24 * 3600,  # DDB retention: 7 days (NOT expired)
            'approval_expiry': now - 100,  # Approval: EXPIRED
        }

        handle_deploy_callback(
            action='approve',
            request_id='req-deploy-001',
            item=item,
            message_id=12345,
            callback_id='cb-001',
            user_id='user-001',
        )

        # Should reject with expired message
        mock_answer.assert_called_once_with('cb-001', '❌ 審批已過期，請重新發起部署')
        # Should NOT start deploy
        mock_start.assert_not_called()
        mock_update_status.assert_not_called()

    @patch('callbacks.start_deploy', return_value={'deploy_id': 'deploy-001', 'status': 'started'})
    @patch('callbacks.update_message')
    @patch('callbacks.answer_callback')
    @patch('callbacks._update_request_status')
    @patch('callbacks._get_table')
    @patch('callbacks.pin_message')
    @patch('callbacks.update_deploy_record')
    def test_deploy_callback_allows_non_expired_approval(
        self, mock_update_deploy, mock_pin, mock_table, mock_update_status,
        mock_answer, mock_update_msg, mock_start
    ):
        """approval_expiry 未過期 → callback 應正常處理"""
        from callbacks import handle_deploy_callback

        now = int(time.time())
        item = {
            'request_id': 'req-deploy-002',
            'action': 'deploy',
            'project_id': 'bouncer',
            'project_name': 'Bouncer',
            'branch': 'master',
            'stack_name': 'bouncer-stack',
            'source': 'test',
            'reason': 'test deploy',
            'context': '',
            'status': 'pending_approval',
            'created_at': now - 60,
            'ttl': now + 7 * 24 * 3600,
            'approval_expiry': now + 500,  # Approval: NOT expired
        }

        handle_deploy_callback(
            action='approve',
            request_id='req-deploy-002',
            item=item,
            message_id=12345,
            callback_id='cb-002',
            user_id='user-001',
        )

        # Should proceed with deploy
        mock_answer.assert_called_once_with('cb-002', '🚀 啟動部署中...')
        mock_update_status.assert_called_once()
        mock_start.assert_called_once()

    @patch('callbacks.update_message')
    @patch('callbacks.answer_callback')
    @patch('callbacks._update_request_status')
    @patch('callbacks._get_table')
    def test_deploy_callback_fallback_to_ttl_when_no_approval_expiry(
        self, mock_table, mock_update_status, mock_answer, mock_update_msg
    ):
        """無 approval_expiry 欄位時 → fallback 到 ttl（向後相容）"""
        from callbacks import handle_deploy_callback

        now = int(time.time())
        item = {
            'request_id': 'req-deploy-003',
            'action': 'deploy',
            'project_id': 'bouncer',
            'project_name': 'Bouncer',
            'branch': 'master',
            'stack_name': 'bouncer-stack',
            'source': 'test',
            'reason': 'test deploy',
            'context': '',
            'status': 'pending_approval',
            'created_at': now - 700,
            'ttl': now - 100,  # TTL expired (no approval_expiry field)
        }

        handle_deploy_callback(
            action='approve',
            request_id='req-deploy-003',
            item=item,
            message_id=12345,
            callback_id='cb-003',
            user_id='user-001',
        )

        # Should reject (fallback to ttl)
        mock_answer.assert_called_once_with('cb-003', '❌ 審批已過期，請重新發起部署')
