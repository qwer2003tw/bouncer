"""
Bouncer - telegram_commands.py 測試
覆蓋 Telegram 指令處理功能
"""

import sys
import os
import time
import pytest
from unittest.mock import patch

from moto import mock_aws
import boto3


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dynamodb():
    """建立 mock DynamoDB 表"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

        # Main table
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'type', 'AttributeType': 'S'},
                {'AttributeName': 'expires_at', 'AttributeType': 'N'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'type-expires-at-index',
                    'KeySchema': [
                        {'AttributeName': 'type', 'KeyType': 'HASH'},
                        {'AttributeName': 'expires_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                },
                {
                    'IndexName': 'status-created-index',
                    'KeySchema': [
                        {'AttributeName': 'status', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()

        # Accounts table
        accounts_table = dynamodb.create_table(
            TableName='bouncer-accounts',
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        accounts_table.wait_until_exists()

        yield dynamodb


@pytest.fixture
def telegram_commands_module(mock_dynamodb):
    """載入 telegram_commands 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['APPROVED_CHAT_IDS'] = '999999999'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'

    # 清除模組
    modules_to_clear = [
        'telegram_commands', 'db', 'constants', 'utils', 'accounts',
        'telegram', 'otp', 'callbacks_command'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]


    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.accounts_table = mock_dynamodb.Table('bouncer-accounts')
    db.dynamodb = mock_dynamodb

    # 初始化預設帳號
    db.accounts_table.put_item(Item={
        'account_id': '111111111111',
        'name': 'Default',
        'role_arn': None,
        'is_default': True,
        'enabled': True,
        'created_at': int(time.time())
    })

    import telegram_commands
    yield telegram_commands

    sys.path.pop(0)


# ============================================================================
# Tests for handle_telegram_command
# ============================================================================

@patch('telegram_commands.send_telegram_message_to')
def test_telegram_command_unauthorized_user(mock_send, telegram_commands_module):
    """測試未授權用戶的指令應被忽略"""
    message = {
        'from': {'id': '111111111'},  # 不在 APPROVED_CHAT_IDS
        'chat': {'id': '111111111'},
        'text': '/accounts'
    }

    result = telegram_commands_module.handle_telegram_command(message)

    # 應回傳 200 OK，但不執行任何操作
    assert result['statusCode'] == 200
    # 不應發送 Telegram 訊息
    mock_send.assert_not_called()


# ============================================================================
# Tests for handle_accounts_command
# ============================================================================

@patch('telegram_commands.send_telegram_message_to')
def test_accounts_command_success(mock_send, telegram_commands_module, mock_dynamodb):
    """測試 /accounts 指令成功列出帳號"""
    chat_id = '999999999'

    result = telegram_commands_module.handle_accounts_command(chat_id)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert chat_id in call_args[0]
    # 訊息應包含 "AWS 帳號"
    assert 'AWS 帳號' in call_args[0][1] or '111111111111' in call_args[0][1]


# ============================================================================
# Tests for handle_trust_command
# ============================================================================

@patch('telegram_commands.send_telegram_message_to')
def test_trust_command_no_active_sessions(mock_send, telegram_commands_module):
    """測試 /trust 指令沒有活躍的信任時段"""
    chat_id = '999999999'

    result = telegram_commands_module.handle_trust_command(chat_id)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert '信任時段' in call_args[0][1]
    assert '沒有' in call_args[0][1] or '目前' in call_args[0][1]


@patch('telegram_commands.send_telegram_message_to')
def test_trust_command_with_active_sessions(mock_send, telegram_commands_module, mock_dynamodb):
    """測試 /trust 指令有活躍的信任時段"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())
    trust_id = 'trust-001'

    # 建立信任時段
    table.put_item(Item={
        'request_id': trust_id,
        'type': 'trust_session',
        'status': 'active',
        'source': 'test-agent',
        'account_id': '111111111111',
        'expires_at': now + 1800,
        'command_count': 5,
        'approved_by': 'admin',
        'created_at': now
    })

    chat_id = '999999999'

    result = telegram_commands_module.handle_trust_command(chat_id)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息，包含信任時段資訊
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert '信任時段' in call_args[0][1]
    assert 'test-agent' in call_args[0][1]


# ============================================================================
# Tests for handle_pending_command
# ============================================================================

@patch('telegram_commands.send_telegram_message_to')
def test_pending_command_no_pending_requests(mock_send, telegram_commands_module):
    """測試 /pending 指令沒有待審批請求"""
    chat_id = '999999999'

    result = telegram_commands_module.handle_pending_command(chat_id)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert '待審批' in call_args[0][1]
    assert '沒有' in call_args[0][1] or '目前' in call_args[0][1]


@patch('telegram_commands.send_telegram_message_to')
def test_pending_command_with_pending_requests(mock_send, telegram_commands_module, mock_dynamodb):
    """測試 /pending 指令有待審批請求"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立待審批請求
    table.put_item(Item={
        'request_id': 'pending-001',
        'status': 'pending',
        'action': 'execute',
        'command': 'aws s3 ls',
        'source': 'test-agent',
        'account_id': '111111111111',
        'created_at': now - 300
    })

    chat_id = '999999999'

    result = telegram_commands_module.handle_pending_command(chat_id)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息，包含待審批請求
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert '待審批' in call_args[0][1]
    assert 'test-agent' in call_args[0][1]


# ============================================================================
# Tests for handle_stats_command
# ============================================================================

@patch('telegram_commands.send_telegram_message_to')
def test_stats_command_empty_results(mock_send, telegram_commands_module):
    """測試 /stats 指令沒有資料"""
    chat_id = '999999999'

    result = telegram_commands_module.handle_stats_command(chat_id, hours=24)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert '統計' in call_args[0][1]
    assert '總請求' in call_args[0][1]


@patch('telegram_commands.send_telegram_message_to')
def test_stats_command_with_data(mock_send, telegram_commands_module, mock_dynamodb):
    """測試 /stats 指令有資料"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    now = int(time.time())

    # 建立測試資料
    items = [
        {'request_id': 'req-s1', 'status': 'approved', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 1000},
        {'request_id': 'req-s2', 'status': 'denied', 'action': 'execute', 'source': 'agent-b', 'created_at': now - 2000},
        {'request_id': 'req-s3', 'status': 'pending', 'action': 'execute', 'source': 'agent-a', 'created_at': now - 3000},
    ]
    for item in items:
        table.put_item(Item=item)

    chat_id = '999999999'

    result = telegram_commands_module.handle_stats_command(chat_id, hours=24)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息，包含統計資訊
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    message = call_args[0][1]
    assert '統計' in message
    assert '總請求' in message
    assert '3' in message  # 總請求數


# ============================================================================
# Tests for handle_help_command
# ============================================================================

@patch('telegram_commands.send_telegram_message_to')
def test_help_command_success(mock_send, telegram_commands_module):
    """測試 /help 指令成功顯示說明"""
    chat_id = '999999999'

    result = telegram_commands_module.handle_help_command(chat_id)

    # 應回傳成功
    assert result['statusCode'] == 200

    # 應發送 Telegram 訊息
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    message = call_args[0][1]
    # 訊息應包含各種指令
    assert '/accounts' in message
    assert '/trust' in message
    assert '/pending' in message
    assert '/stats' in message
    assert '/help' in message
