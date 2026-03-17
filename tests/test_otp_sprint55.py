"""Tests for OTP two-factor verification (Sprint 55 s55-007)."""

import os
import sys
import time

import pytest
import boto3
from moto import mock_aws
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

import pytest
pytestmark = pytest.mark.xdist_group("otp")


def _create_mock_table(dynamodb):
    table = dynamodb.create_table(
        TableName='clawdbot-approval-requests',
        KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    table.wait_until_exists()
    return table


class TestOTPGeneration:
    def test_generate_otp_is_6_digits(self):
        from otp import generate_otp
        code = generate_otp()
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_otp_is_random(self):
        from otp import generate_otp
        codes = {generate_otp() for _ in range(20)}
        assert len(codes) > 1  # Should not all be the same


class TestOTPValidation:

    def test_valid_otp_succeeds(self):
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            import src.db as db_mod
            db_mod.table = table
            otp_mod._get_table = lambda: table

            otp_mod.create_otp_record('req-001', '123456789', '654321', message_id=100)
            success, msg = otp_mod.validate_otp('req-001', '654321')
            assert success is True
            assert 'OTP 驗證成功' in msg

    def test_wrong_otp_fails_and_increments_attempts(self):
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            otp_mod.create_otp_record('req-002', '123456789', '654321')
            success, msg = otp_mod.validate_otp('req-002', '999999')
            assert success is False
            assert '還剩' in msg

            # Check attempts incremented
            item = table.get_item(Key={'request_id': 'otp#req-002'}).get('Item')
            assert int(item['attempts']) == 1

    def test_max_attempts_exceeded_marks_failed(self):
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            otp_mod.create_otp_record('req-003', '123456789', '654321')
            # 3 wrong attempts
            for _ in range(3):
                otp_mod.validate_otp('req-003', '000000')

            success, msg = otp_mod.validate_otp('req-003', '654321')
            assert success is False
            assert '超過上限' in msg

    def test_expired_otp_fails(self):
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            # Create expired OTP (ttl in the past)
            now = int(time.time())
            table.put_item(Item={
                'request_id': 'otp#req-004',
                'otp_code': '654321',
                'user_id': '123456789',
                'attempts': 0,
                'created_at': now - 400,
                'ttl': now - 100,  # expired
                'type': 'otp_pending',
            })

            success, msg = otp_mod.validate_otp('req-004', '654321')
            assert success is False
            assert '過期' in msg

    def test_nonexistent_otp_fails(self):
        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = _create_mock_table(dynamodb)

            import src.otp as otp_mod
            otp_mod._get_table = lambda: table

            success, msg = otp_mod.validate_otp('nonexistent', '123456')
            assert success is False
            assert '不存在' in msg or '過期' in msg


class TestOTPCommandCallback:

    def test_high_risk_command_triggers_otp(self, mock_dynamodb, app_module):
        """risk_score >= 66 should trigger OTP flow instead of executing."""
        request_id = 'otp-test-high-risk-001'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws iam delete-role --role-name TestRole',
            'reason': 'test',
            'source': 'test',
            'trust_scope': '',
            'account_id': '123456789012',
            'status': 'pending',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 3600,
            'risk_score': 80,  # High risk
        })

        with patch('otp.generate_otp', return_value='123456'), \
             patch('otp.create_otp_record'), \
             patch('telegram.send_telegram_message_to') as mock_dm, \
             patch('telegram.answer_callback') as mock_answer, \
             patch('telegram.update_message') as mock_update:

            from callbacks_command import handle_command_callback
            result = handle_command_callback(
                'approve', request_id,
                app_module.table.get_item(Key={'request_id': request_id})['Item'],
                message_id=123, callback_id='cb-001', user_id='316743844',
            )

        # Should send OTP DM and NOT execute command
        mock_dm.assert_called_once()
        assert '123456' in mock_dm.call_args[0][1]  # OTP in DM

    def test_low_risk_command_skips_otp(self, mock_dynamodb, app_module):
        """risk_score < 66 should execute directly without OTP."""
        request_id = 'otp-test-low-risk-001'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws s3 ls',
            'reason': 'test',
            'source': 'test',
            'trust_scope': '',
            'account_id': '123456789012',
            'status': 'pending',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 3600,
            'risk_score': 20,  # Low risk
        })

        with patch('callbacks_command._execute_and_store_result', return_value={'result': 's3://bucket', 'paged': {'paged': False}}) as mock_exec, \
             patch('callbacks_command.store_paged_output', return_value={'paged': False, 'result': 's3://bucket', 'page': 1, 'total_pages': 1, 'output_length': 12}), \
             patch('callbacks_command.answer_callback'), \
             patch('callbacks_command.update_message'), \
             patch('callbacks_command.send_telegram_message_silent'), \
             patch('callbacks_command.emit_metric'):

            from callbacks_command import handle_command_callback
            handle_command_callback(
                'approve', request_id,
                app_module.table.get_item(Key={'request_id': request_id})['Item'],
                message_id=123, callback_id='cb-002', user_id='316743844',
            )

        # Should execute directly
        mock_exec.assert_called_once()

    def test_otp_verified_item_skips_otp_check(self, mock_dynamodb, app_module):
        """Item with otp_verified=True should skip OTP and execute directly."""
        request_id = 'otp-test-verified-001'
        item = {
            'request_id': request_id,
            'command': 'aws iam delete-role --role-name TestRole',
            'reason': 'test',
            'source': 'test',
            'trust_scope': '',
            'account_id': '123456789012',
            'status': 'pending',
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 3600,
            'risk_score': 80,
            'otp_verified': True,  # Already verified
        }
        app_module.table.put_item(Item=item)

        with patch('callbacks_command.execute_command', return_value='role deleted') as mock_exec, \
             patch('callbacks_command.store_paged_output', return_value={'paged': False, 'result': 'role deleted', 'page': 1, 'total_pages': 1, 'output_length': 12}), \
             patch('callbacks_command.answer_callback'), \
             patch('callbacks_command.update_message'), \
             patch('callbacks_command.send_telegram_message_silent'), \
             patch('callbacks_command.emit_metric'):

            from callbacks_command import handle_command_callback
            handle_command_callback(
                'approve', request_id, item,
                message_id=123, callback_id='cb-003', user_id='316743844',
            )

        # Should execute directly (OTP already verified)
        mock_exec.assert_called_once()
