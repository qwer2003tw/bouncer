"""Regression tests for Sprint 56 bug fixes."""

import os
import sys
import json
import time
from unittest.mock import patch

import pytest
import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'  # Must override any existing value
os.environ.setdefault('OTP_RISK_THRESHOLD', '70')

pytestmark = pytest.mark.xdist_group("sprint56")


def _create_mock_table(dynamodb):
    table = dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    table.wait_until_exists()
    return table


class TestS56_001_OTPRiskScoreRecalculation:
    """Bug s56-001: OTP should recalculate risk_score in callback instead of using DDB value."""

    def test_otp_triggered_despite_zero_risk_score_in_ddb(self):
        """Test that OTP is triggered even if DDB has risk_score=0 (e.g., from shadow mode)."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            # Create a high-risk command request with risk_score=0 in DDB (simulating shadow mode)
            high_risk_cmd = 'aws lambda update-function-configuration --function-name prod-api --environment Variables={API_KEY=secret123}'
            table.put_item(Item={
                'request_id': 's56-001-test',
                'command': high_risk_cmd,
                'status': 'pending_approval',
                'created_at': int(time.time()),
                'user_id': '123456789',
                'risk_score': 0,  # Bug scenario: DDB has 0 despite high-risk command
            })

            from callbacks_command import handle_command_callback
            import src.db as db_mod
            db_mod.table = table

            # Mock calculate_risk to return high score (>= 70) to trigger OTP
            from unittest.mock import MagicMock
            mock_risk_result = MagicMock()
            mock_risk_result.score = 85

            with patch('callbacks_command.answer_callback') as mock_answer, \
                 patch('callbacks_command.update_message'), \
                 patch('callbacks_command.send_telegram_message_to') as mock_send_otp, \
                 patch('risk_scorer.calculate_risk', return_value=mock_risk_result), \
                 patch('otp.generate_otp', return_value='123456'), \
                 patch('otp.create_otp_record'):

                # Simulate approve callback
                handle_command_callback(
                    action='approve',
                    request_id='s56-001-test',
                    item={'request_id': 's56-001-test', 'command': high_risk_cmd, 'risk_score': 0, 'status': 'pending_approval'},
                    message_id=100,
                    callback_id='callback-1',
                    user_id='123456789',
                    source_ip='127.0.0.1'
                )

                # Should trigger OTP despite risk_score=0 in DDB
                # because the callback recalculates the risk_score
                assert mock_send_otp.called, "OTP should be sent for high-risk command"
                assert 'OTP 已發送至 DM' in mock_answer.call_args[0][1]


class TestS56_002_IAMDeleteRoleCompliance:
    """Bug s56-002: 'iam delete-role' should require approval, not be blocked."""

    def test_iam_delete_role_not_in_blocked_patterns(self):
        """Test that 'iam delete-role' is not in BLOCKED_PATTERNS."""
        from constants import BLOCKED_PATTERNS
        assert 'iam delete-role' not in BLOCKED_PATTERNS, "iam delete-role should not be blocked"

    def test_iam_delete_role_gets_pending_approval(self):
        """Test that 'iam delete-role' command passes compliance check (not blocked)."""
        from compliance_checker import check_compliance

        cmd = 'aws iam delete-role --role-name test-role'
        is_compliant, violation = check_compliance(cmd)

        # Should pass compliance check (not be blocked by compliance rules)
        assert is_compliant, f"iam delete-role should pass compliance check but got violation: {violation}"


class TestS56_003_UpdateMessageErrorHandling:
    """Bug s56-003: update_message calls should have error handling to avoid 400 errors."""

    def test_approve_callback_handles_update_message_400_error(self):
        """Test that 400 error from update_message doesn't crash approval flow."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            table.put_item(Item={
                'request_id': 's56-003-test',
                'command': 'aws s3 ls',
                'status': 'pending_approval',
                'created_at': int(time.time()),
                'user_id': '123456789',
                'risk_score': 10,
            })

            from callbacks_command import handle_command_callback
            import src.db as db_mod
            db_mod.table = table

            with patch('callbacks_command.answer_callback'), \
                 patch('callbacks_command.execute_command', return_value='success'), \
                 patch('callbacks_command.send_telegram_message_silent'), \
                 patch('callbacks_command.send_chat_action'), \
                 patch('callbacks_command.update_message', side_effect=Exception("Bad Request: message to edit not found")):

                # Should not raise exception despite update_message failing
                result = handle_command_callback(
                    action='approve',
                    request_id='s56-003-test',
                    item={'request_id': 's56-003-test', 'command': 'aws s3 ls', 'status': 'pending_approval', 'created_at': int(time.time())},
                    message_id=100,
                    callback_id='callback-1',
                    user_id='123456789',
                    source_ip='127.0.0.1'
                )

                # Should return 200 despite update_message failure
                assert result['statusCode'] == 200

    def test_deny_callback_handles_update_message_400_error(self):
        """Test that 400 error from update_message in deny flow doesn't crash."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            table.put_item(Item={
                'request_id': 's56-003-deny',
                'command': 'aws s3 ls',
                'status': 'pending_approval',
                'created_at': int(time.time()),
                'user_id': '123456789',
            })

            from callbacks_command import handle_command_callback
            import src.db as db_mod
            db_mod.table = table

            with patch('callbacks_command.answer_callback'), \
                 patch('callbacks_command.update_message', side_effect=Exception("Bad Request: message not found")):

                # Should not raise exception
                result = handle_command_callback(
                    action='deny',
                    request_id='s56-003-deny',
                    item={'request_id': 's56-003-deny', 'command': 'aws s3 ls', 'status': 'pending_approval', 'created_at': int(time.time())},
                    message_id=100,
                    callback_id='callback-1',
                    user_id='123456789',
                    source_ip='127.0.0.1'
                )

                assert result['statusCode'] == 200


class TestS56_004_ExpiryWarningStatusCheck:
    """Bug s56-004: Expiry warning should check DDB status before sending."""

    def test_expiry_warning_skipped_for_approved_request(self):
        """Test that expiry warning is skipped if request is already approved."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            # Create an already-approved request
            table.put_item(Item={
                'request_id': 's56-004-approved',
                'command': 'aws s3 ls',
                'status': 'approved',  # Already approved
                'created_at': int(time.time()),
                'user_id': '123456789',
            })

            import src.db as db_mod
            db_mod.table = table

            with patch('notifications.send_expiry_warning_notification') as mock_send:
                # Ensure src/app.py is imported (xdist isolation fix)
                import sys
                import os
                src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
                if src_path in sys.path:
                    sys.path.remove(src_path)
                sys.path.insert(0, src_path)
                if 'app' in sys.modules:
                    app_file = getattr(sys.modules['app'], '__file__', '')
                    if 'deployer' in app_file:
                        del sys.modules['app']
                        import app  # Re-import from src/
                from app import lambda_handler

                event = {
                    'source': 'bouncer-scheduler',
                    'action': 'expiry_warning',
                    'request_id': 's56-004-approved',
                    'command_preview': 'aws s3 ls',
                    'source_field': 'test',
                }

                result = lambda_handler(event, {})

                # Should not send warning for already-approved request
                assert not mock_send.called, "Warning should not be sent for approved request"
                assert result['statusCode'] == 200
                body = json.loads(result['body'])
                assert body['status'] == 'skipped'

    def test_expiry_warning_skipped_for_denied_request(self):
        """Test that expiry warning is skipped if request is already denied."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            table.put_item(Item={
                'request_id': 's56-004-denied',
                'command': 'aws s3 ls',
                'status': 'denied',  # Already denied
                'created_at': int(time.time()),
                'user_id': '123456789',
            })

            import src.db as db_mod
            db_mod.table = table

            with patch('notifications.send_expiry_warning_notification') as mock_send:
                # Ensure src/app.py is imported (xdist isolation fix)
                import sys
                import os
                src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
                if src_path in sys.path:
                    sys.path.remove(src_path)
                sys.path.insert(0, src_path)
                if 'app' in sys.modules:
                    app_file = getattr(sys.modules['app'], '__file__', '')
                    if 'deployer' in app_file:
                        del sys.modules['app']
                        import app  # Re-import from src/
                from app import lambda_handler

                event = {
                    'source': 'bouncer-scheduler',
                    'action': 'expiry_warning',
                    'request_id': 's56-004-denied',
                    'command_preview': 'aws s3 ls',
                }

                result = lambda_handler(event, {})

                assert not mock_send.called, "Warning should not be sent for denied request"
                assert result['statusCode'] == 200

    def test_expiry_warning_sent_for_pending_approval(self):
        """Test that expiry warning IS sent if request is still pending_approval."""
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            table.put_item(Item={
                'request_id': 's56-004-pending',
                'command': 'aws s3 ls',
                'status': 'pending_approval',  # Still pending
                'created_at': int(time.time()),
                'user_id': '123456789',
            })

            import src.db as db_mod
            db_mod.table = table

            with patch('notifications.send_expiry_warning_notification') as mock_send:
                # Ensure src/app.py is imported (xdist isolation fix)
                import sys
                import os
                src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
                if src_path in sys.path:
                    sys.path.remove(src_path)
                sys.path.insert(0, src_path)
                if 'app' in sys.modules:
                    app_file = getattr(sys.modules['app'], '__file__', '')
                    if 'deployer' in app_file:
                        del sys.modules['app']
                        import app  # Re-import from src/
                from app import lambda_handler

                event = {
                    'source': 'bouncer-scheduler',
                    'action': 'expiry_warning',
                    'request_id': 's56-004-pending',
                    'command_preview': 'aws s3 ls',
                    'source_field': 'test',
                }

                result = lambda_handler(event, {})

                # Should send warning for pending request
                assert mock_send.called, "Warning should be sent for pending_approval request"
                assert result['statusCode'] == 200
                body = json.loads(result['body'])
                assert body['status'] == 'ok'
