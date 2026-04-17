"""
test_pin_unpin_deploy.py — Pin/Unpin Deploy Message Tests (Sprint 25-004)

Tests for pin_message() and unpin_message() functions and their integration
with the deploy approval and completion flow.
"""

import time
import urllib.error
import os
from unittest.mock import patch, MagicMock

import deploy_db


class TestPinMessageFunction:
    """Unit tests for pin_message() function"""

    def test_pin_message_success(self, app_module):
        """Test pin_message returns True when API call succeeds"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            # Mock successful API response
            mock_request.return_value = {'ok': True}

            result = telegram.pin_message(12345, disable_notification=True)

            assert result is True
            mock_request.assert_called_once_with('pinChatMessage', {
                'chat_id': '999999999',
                'message_id': 12345,
                'disable_notification': True,
            })

    def test_pin_message_api_failure(self, app_module):
        """Test pin_message returns False when API returns ok=False"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            # Mock API failure response (ok=False)
            mock_request.return_value = {'ok': False, 'description': 'Bad Request'}

            result = telegram.pin_message(12345)

            assert result is False

    def test_pin_message_exception(self, app_module):
        """Test pin_message returns False and logs warning when exception occurs"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            # Mock exception (use URLError which is a network error type)
            mock_request.side_effect = urllib.error.URLError('Network error')

            result = telegram.pin_message(12345)

            assert result is False

    def test_pin_message_disable_notification_default(self, app_module):
        """Test pin_message uses disable_notification=True by default"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            mock_request.return_value = {'ok': True}

            telegram.pin_message(67890)

            # Check that disable_notification defaults to True
            call_args = mock_request.call_args[0][1]
            assert call_args['disable_notification'] is True


class TestUnpinMessageFunction:
    """Unit tests for unpin_message() function"""

    def test_unpin_message_success(self, app_module):
        """Test unpin_message returns True when API call succeeds"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            # Mock successful API response
            mock_request.return_value = {'ok': True}

            result = telegram.unpin_message(12345)

            assert result is True
            mock_request.assert_called_once_with('unpinChatMessage', {
                'chat_id': '999999999',
                'message_id': 12345,
            })

    def test_unpin_message_api_failure(self, app_module):
        """Test unpin_message returns False when API returns ok=False"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            # Mock API failure response
            mock_request.return_value = {'ok': False, 'description': 'Message not found'}

            result = telegram.unpin_message(12345)

            assert result is False

    def test_unpin_message_exception(self, app_module):
        """Test unpin_message returns False and logs warning when exception occurs"""
        import telegram
        with patch.object(telegram, '_telegram_request') as mock_request:
            # Mock exception (use URLError which is a network error type)
            mock_request.side_effect = urllib.error.URLError('Message was deleted')

            result = telegram.unpin_message(12345)

            assert result is False


class TestDeployApprovalNoPin:
    """Integration test: deploy approval DOES pin (Sprint 31-003 re-adds pin in callbacks)"""

    def test_deploy_approval_calls_pin(self, app_module):
        """Test that deploy approval callback DOES call pin_message (Sprint 31-003)

        Sprint 29-004 moved pin to notifier; Sprint 31-003 adds it back to
        handle_deploy_callback as a best-effort pin right after 部署已啟動.
        """
        import callbacks
        import deployer

        with patch.object(deployer, 'start_deploy') as mock_start_deploy, \
             patch.object(callbacks, 'update_message'), \
             patch.object(callbacks, 'answer_callback'), \
             patch.object(deployer, 'update_deploy_record') as mock_update_record, \
             patch('callbacks.pin_message') as mock_pin:

            # Mock successful deploy start
            mock_start_deploy.return_value = {
                'status': 'started',
                'deploy_id': 'deploy-test123',
                'commit_short': 'abc1234',
                'commit_message': 'feat: add feature',
            }
            mock_pin.return_value = True

            # Create test request
            request_id = 'deploy_pin_test'
            item = {
                'project_id': 'test-project',
                'project_name': 'Test Project',
                'branch': 'main',
                'stack_name': 'test-stack',
                'source': 'mcp',
                'reason': 'testing pin',
                'context': '',
            }

            result = callbacks.handle_deploy_callback(
                action='approve',
                request_id=request_id,
                item=item,
                message_id=99999,
                callback_id='cb_test',
                user_id='user123'
            )

            # Verify pin_message WAS called (Sprint 31-003 adds pin back)
            mock_pin.assert_called_once_with(99999)

            # Verify deploy record was still updated with telegram_message_id
            assert mock_update_record.called
            update_call = mock_update_record.call_args_list[0]
            assert update_call[0][0] == 'deploy-test123'
            assert update_call[0][1]['telegram_message_id'] == 99999


class TestDeployCompleteCallsUnpin:
    """Integration test: deploy completion calls unpin_message"""

    def test_deploy_complete_success_calls_unpin(self, app_module):
        """Test that deploy completion (SUCCESS) calls unpin_message"""
        import deployer
        import telegram

        deploy_id = 'deploy-unpin-test'
        telegram_message_id = 77777

        # Mock DDB record with telegram_message_id
        mock_deploy_record = {
            'deploy_id': deploy_id,
            'project_id': 'test-project',
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
            'started_at': int(time.time()) - 100,
            'telegram_message_id': telegram_message_id,
        }

        # Mock SFN success
        mock_sfn_client = MagicMock()
        mock_sfn_client.describe_execution.return_value = {
            'status': 'SUCCEEDED',
        }
        mock_sfn_client.get_execution_history.return_value = {
            'events': []
        }
        mock_update_deploy_record = MagicMock()

        with patch('deployer.get_deploy_record', return_value=mock_deploy_record), \
             patch.object(deployer, '_get_sfn_client', return_value=mock_sfn_client), \
             patch('deployer.update_deploy_record', mock_update_deploy_record), \
             patch('deployer.release_lock'), \
             patch('deployer.unpin_message') as mock_unpin:

            mock_unpin.return_value = True

            # Call get_deploy_status (which triggers unpin on completion)
            result = deployer.get_deploy_status(deploy_id)

            # Verify unpin_message was called
            mock_unpin.assert_called_once_with(telegram_message_id)

    def test_deploy_complete_failure_calls_unpin(self, app_module):
        """Test that deploy completion (FAILED) calls unpin_message"""
        import deployer
        import telegram

        deploy_id = 'deploy-unpin-fail-test'
        telegram_message_id = 66666

        mock_deploy_record = {
            'deploy_id': deploy_id,
            'project_id': 'test-project',
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
            'started_at': int(time.time()) - 100,
            'telegram_message_id': telegram_message_id,
            'stack_name': 'test-stack',
        }

        # Mock SFN failure
        mock_sfn_client = MagicMock()
        mock_sfn_client.describe_execution.return_value = {
            'status': 'FAILED',
        }
        mock_sfn_client.get_execution_history.return_value = {
            'events': []
        }

        # Mock CFN client
        mock_cfn_client = MagicMock()
        mock_cfn_client.describe_stack_events.return_value = {
            'StackEvents': []
        }
        mock_update_deploy_record = MagicMock()

        with patch('deployer.get_deploy_record', return_value=mock_deploy_record), \
             patch.object(deployer, '_get_sfn_client', return_value=mock_sfn_client), \
             patch.object(deployer, '_get_cfn_client', return_value=mock_cfn_client), \
             patch('deployer.update_deploy_record', mock_update_deploy_record), \
             patch('deployer.release_lock'), \
             patch.object(deployer, 'send_deploy_failure_notification'), \
             patch('deployer.unpin_message') as mock_unpin:

            mock_unpin.return_value = True

            # Call get_deploy_status
            result = deployer.get_deploy_status(deploy_id)

            # Verify unpin_message was called
            mock_unpin.assert_called_once_with(telegram_message_id)

    def test_deploy_complete_unpin_failure_does_not_block(self, app_module):
        """Test that unpin_message failure doesn't block deploy completion"""
        import deployer
        import telegram

        deploy_id = 'deploy-unpin-error-test'

        mock_deploy_record_unpin = {
            'deploy_id': deploy_id,
            'project_id': 'test-project',
            'status': 'RUNNING',
            'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
            'started_at': int(time.time()) - 100,
            'telegram_message_id': 55555,
        }

        # Mock SFN success
        mock_sfn_client = MagicMock()
        mock_sfn_client.describe_execution.return_value = {
            'status': 'SUCCEEDED',
        }
        mock_sfn_client.get_execution_history.return_value = {
            'events': []
        }
        mock_update_record = MagicMock()

        with patch('deployer.get_deploy_record', return_value=mock_deploy_record_unpin), \
             patch.object(deployer, '_get_sfn_client', return_value=mock_sfn_client), \
             patch('deployer.update_deploy_record', mock_update_record), \
             patch('deployer.release_lock'), \
             patch('deployer.unpin_message') as mock_unpin:

            # Mock unpin failure (use ValueError which is caught in deployer.py)
            mock_unpin.side_effect = ValueError('Message was deleted')

            # Should not raise exception
            result = deployer.get_deploy_status(deploy_id)

            # Deploy status should still update to SUCCESS
            assert result['status'] == 'SUCCESS'

    def test_deploy_complete_no_telegram_message_id(self, app_module):
        """Test that unpin is skipped when telegram_message_id is missing"""
        import deployer
        import telegram

        deploy_id = 'deploy-no-msg-id'

        # Mock DDB record WITHOUT telegram_message_id
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            'Item': {
                'deploy_id': deploy_id,
                'project_id': 'test-project',
                'status': 'RUNNING',
                'execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:test',
                'started_at': int(time.time()) - 100,
                # No telegram_message_id
            }
        }
        mock_table.update_item = MagicMock()

        # Mock SFN success
        mock_sfn_client = MagicMock()
        mock_sfn_client.describe_execution.return_value = {
            'status': 'SUCCEEDED',
        }
        mock_sfn_client.get_execution_history.return_value = {
            'events': []
        }

        with patch('deployer._get_history_table', return_value=mock_table), \
             patch.object(deployer, '_get_sfn_client', return_value=mock_sfn_client), \
             patch('deployer.release_lock'), \
             patch('deployer.unpin_message') as mock_unpin:

            # Call get_deploy_status
            result = deployer.get_deploy_status(deploy_id)

            # Verify unpin_message was NOT called
            mock_unpin.assert_not_called()
