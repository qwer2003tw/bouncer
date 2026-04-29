"""
Bouncer - callbacks_command.py 測試
覆蓋命令審批 callback 的核心邏輯
"""

import sys
import os
import time
import json
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
        table = dynamodb.create_table(
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
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def callbacks_module(mock_dynamodb):
    """載入 callbacks_command 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'callbacks_command', 'db', 'constants', 'trust', 'commands',
        'telegram', 'notifications', 'utils', 'paging', 'metrics'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import callbacks_command
    yield callbacks_command

    sys.path.pop(0)


# ============================================================================
# Tests for _is_execute_failed
# ============================================================================

def test_is_execute_failed_with_error_prefix(callbacks_module):
    """測試 _is_execute_failed 正確識別 ❌ prefix"""
    result = callbacks_module._is_execute_failed("❌ Command failed")
    assert result is True


def test_is_execute_failed_with_exit_code(callbacks_module):
    """測試 _is_execute_failed 正確識別 exit code"""
    result = callbacks_module._is_execute_failed("Error: something went wrong (exit code: 1)")
    assert result is True


def test_is_execute_failed_success(callbacks_module):
    """測試 _is_execute_failed 正確識別成功"""
    result = callbacks_module._is_execute_failed("Success output (exit code: 0)")
    assert result is False


# ============================================================================
# Tests for _update_request_status
# ============================================================================

def test_update_request_status_success(callbacks_module, mock_dynamodb):
    """測試 _update_request_status 成功更新狀態"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-001'

    # 建立初始請求
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'command': 'aws s3 ls',
        'created_at': int(time.time())
    })

    # 更新狀態
    success = callbacks_module._update_request_status(
        table, request_id, 'approved', 'user123'
    )

    assert success is True

    # 驗證更新結果
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'approved'
    assert item['approver'] == 'user123'
    assert 'approved_at' in item


def test_update_request_status_stale(callbacks_module, mock_dynamodb):
    """測試 _update_request_status 處理過期請求（狀態已改變）"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-002'

    # 建立已處理的請求
    table.put_item(Item={
        'request_id': request_id,
        'status': 'approved',  # 已經 approved
        'command': 'aws s3 ls',
        'created_at': int(time.time())
    })

    # 嘗試再次更新
    success = callbacks_module._update_request_status(
        table, request_id, 'denied', 'user456'
    )

    # 應該回傳 False（stale）
    assert success is False

    # 狀態應保持 approved
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'approved'


# ============================================================================
# Tests for _parse_command_callback_request
# ============================================================================

def test_parse_command_callback_request_complete(callbacks_module):
    """測試 _parse_command_callback_request 正確解析完整欄位"""
    item = {
        'command': 'aws s3 ls',
        'assume_role': 'arn:aws:iam::123:role/Test',
        'source': 'test-agent',
        'trust_scope': 'deploy',
        'reason': 'Testing',
        'context': 'CI/CD',
        'account_id': '222222222222',
        'account_name': 'Test Account'
    }

    parsed = callbacks_module._parse_command_callback_request(item)

    assert parsed['command'] == 'aws s3 ls'
    assert parsed['assume_role'] == 'arn:aws:iam::123:role/Test'
    assert parsed['source'] == 'test-agent'
    assert parsed['trust_scope'] == 'deploy'
    assert parsed['reason'] == 'Testing'
    assert parsed['context'] == 'CI/CD'
    assert parsed['account_id'] == '222222222222'
    assert parsed['account_name'] == 'Test Account'


def test_parse_command_callback_request_with_defaults(callbacks_module):
    """測試 _parse_command_callback_request 處理缺少欄位時的預設值"""
    item = {
        'command': 'aws s3 ls'
    }

    parsed = callbacks_module._parse_command_callback_request(item)

    assert parsed['command'] == 'aws s3 ls'
    assert parsed['assume_role'] is None
    assert parsed['source'] == ''
    assert parsed['trust_scope'] == ''
    assert parsed['reason'] == ''
    assert parsed['context'] == ''
    assert parsed['account_id'] == '111111111111'  # DEFAULT_ACCOUNT_ID
    assert parsed['account_name'] == 'Default'


# ============================================================================
# Tests for _format_command_info
# ============================================================================

def test_format_command_info(callbacks_module):
    """測試 _format_command_info 正確格式化"""
    parsed = {
        'command': 'aws s3 ls s3://bucket/path',
        'source': 'test-agent',
        'context': 'deploy-prod',
        'reason': 'Check files',
        'account_id': '111111111111',
        'account_name': 'Production'
    }

    info = callbacks_module._format_command_info(parsed)

    assert 'source_line' in info
    assert 'account_line' in info
    assert 'safe_reason' in info
    assert 'cmd_preview' in info
    assert info['cmd_preview'] == 'aws s3 ls s3://bucket/path'


def test_format_command_info_long_command(callbacks_module):
    """測試 _format_command_info 截斷長命令"""
    parsed = {
        'command': 'a' * 600,  # 600 字元
        'source': 'test',
        'context': '',
        'reason': 'test',
        'account_id': '111111111111',
        'account_name': 'Test'
    }

    info = callbacks_module._format_command_info(parsed)

    # 應截斷至 500 字元 + '...'
    assert len(info['cmd_preview']) == 503
    assert info['cmd_preview'].endswith('...')


# ============================================================================
# Tests for _execute_and_store_result
# ============================================================================

@patch('callbacks_command.execute_command')
@patch('callbacks_command.store_paged_output')
@patch('callbacks_command.emit_metric')
def test_execute_and_store_result_success(mock_emit, mock_store, mock_exec, callbacks_module, mock_dynamodb):
    """測試 _execute_and_store_result 成功執行"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-003'

    # 建立初始請求
    created_at = int(time.time()) - 10
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'command': 'aws s3 ls',
        'created_at': created_at
    })

    # Mock 執行結果
    mock_exec.return_value = "bucket1\nbucket2"
    from paging import PaginatedOutput
    mock_store.return_value = PaginatedOutput(paged=False, result='bucket1\nbucket2', telegram_pages=1)

    result = callbacks_module._execute_and_store_result(
        command='aws s3 ls',
        assume_role=None,
        request_id=request_id,
        item={'created_at': created_at},
        user_id='user123',
        source_ip='1.2.3.4',
        action='approve'
    )

    assert result['result'] == "bucket1\nbucket2"
    assert isinstance(result['paged'], PaginatedOutput)
    assert not result['paged'].paged
    assert result['decision_latency_ms'] >= 10000  # >= 10 秒
    assert 'stale' not in result

    # 驗證 DDB 更新
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'approved'
    assert item['result'] == "bucket1\nbucket2"
    assert item['approver'] == 'user123'


@patch('callbacks_command.execute_command')
@patch('callbacks_command.store_paged_output')
def test_execute_and_store_result_stale_status(mock_store, mock_exec, callbacks_module, mock_dynamodb):
    """測試 _execute_and_store_result 處理 stale status（已被其他 callback 處理）"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-004'

    # 建立已處理的請求
    table.put_item(Item={
        'request_id': request_id,
        'status': 'approved',  # 已經處理
        'command': 'aws s3 ls',
        'created_at': int(time.time())
    })

    mock_exec.return_value = "output"
    from paging import PaginatedOutput
    mock_store.return_value = PaginatedOutput(paged=False, result='output')

    result = callbacks_module._execute_and_store_result(
        command='aws s3 ls',
        assume_role=None,
        request_id=request_id,
        item={'created_at': int(time.time())},
        user_id='user123',
        source_ip='1.2.3.4',
        action='approve'
    )

    # 應回傳 stale=True
    assert result.get('stale') is True


# ============================================================================
# Tests for handle_command_callback (integration)
# ============================================================================

@patch('callbacks_command.answer_callback')
@patch('callbacks_command.update_message')
@patch('callbacks_command.execute_command')
@patch('callbacks_command.store_paged_output')
@patch('callbacks_command.emit_metric')
@patch('callbacks_command.send_chat_action')
@patch('callbacks_command.send_telegram_message_silent')
def test_handle_command_callback_approve_success(
    mock_send, mock_chat, mock_emit, mock_store, mock_exec, mock_update, mock_answer,
    callbacks_module, mock_dynamodb
):
    """測試 handle_command_callback approve action 成功執行"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-005'

    # 建立請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'command': 'aws s3 ls',
        'source': 'test-agent',
        'reason': 'Testing',
        'account_id': '111111111111',
        'account_name': 'Test',
        'trust_scope': '',
        'created_at': created_at,
        'ttl': created_at + 600  # 10 分鐘後過期
    })

    # Mock 執行結果
    mock_exec.return_value = "bucket1\nbucket2 (exit code: 0)"
    from paging import PaginatedOutput
    mock_store.return_value = PaginatedOutput(paged=False, result='output')

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_module.handle_command_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['ok'] is True

    # 驗證 answer_callback 被呼叫
    mock_answer.assert_called()

    # 驗證 update_message 被呼叫 (Sprint 99 #434: should retain context)
    assert mock_update.call_count >= 1
    # Check the final update_message call retains context (not just "見下方結果")
    final_update_call = mock_update.call_args
    final_update_text = final_update_call[0][1]
    assert request_id in final_update_text, "Update message should contain request_id"
    assert 'aws s3 ls' in final_update_text, "Update message should contain command"
    assert '見下方結果' not in final_update_text, "Update message should not contain '見下方結果'"

    # 驗證 send_telegram_message_silent 被呼叫（發送結果）
    mock_send.assert_called_once()
    call_args = mock_send.call_args[0][0]
    assert '✅' in call_args  # 成功標記


@patch('callbacks_command.answer_callback')
@patch('callbacks_command.update_message')
@patch('callbacks_command.execute_command')
@patch('callbacks_command.store_paged_output')
@patch('callbacks_command.emit_metric')
@patch('callbacks_command.send_chat_action')
@patch('callbacks_command.send_telegram_message_silent')
def test_regression_434_approval_message_retains_context(
    mock_send, mock_chat, mock_emit, mock_store, mock_exec, mock_update, mock_answer,
    callbacks_module, mock_dynamodb
):
    """Sprint 99 #434: Approval message should retain context, not just '見下方結果'

    This test explicitly verifies that after approval execution:
    1. update_message is called with remove_buttons=True
    2. The message text contains the request_id
    3. The message text contains the command preview
    4. The message text does NOT contain '見下方結果'
    """
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-434'
    test_command = 'aws ec2 describe-instances --region us-west-2'

    # 建立請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'command': test_command,
        'source': 'regression-test',
        'reason': 'Verify #434 fix',
        'account_id': '222222222222',
        'account_name': 'RegressionTest',
        'trust_scope': '',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    # Mock 執行結果
    mock_exec.return_value = "i-1234567890abcdef0 (exit code: 0)"
    from paging import PaginatedOutput
    mock_store.return_value = PaginatedOutput(paged=False, result='output')

    item = table.get_item(Key={'request_id': request_id})['Item']
    callbacks_module.handle_command_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=54321,
        callback_id='cb434',
        user_id='user434',
        source_ip='10.20.30.40'
    )

    # Verify update_message was called
    assert mock_update.call_count >= 1, "update_message should be called"

    # Get the final update_message call (should be the one that removes buttons)
    final_call = mock_update.call_args
    message_id_arg = final_call[0][0]
    message_text = final_call[0][1]
    remove_buttons = final_call[1].get('remove_buttons', False)

    # Explicit assertions for #434
    assert message_id_arg == 54321, "Should update the correct message"
    assert remove_buttons is True, "Should remove buttons"
    assert request_id in message_text, f"Message should contain request_id '{request_id}'"
    assert 'aws ec2 describe-instances' in message_text, "Message should contain command"
    assert '見下方結果' not in message_text, "Message should NOT contain '見下方結果' (#434 regression)"
    assert '已執行' in message_text, "Message should indicate execution completed"


@patch('callbacks_command.answer_callback')
@patch('callbacks_command.update_message')
def test_handle_command_callback_deny(mock_update, mock_answer, callbacks_module, mock_dynamodb):
    """測試 handle_command_callback deny action"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-006'

    # 建立請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'command': 'aws s3 rm s3://bucket --recursive',
        'source': 'test-agent',
        'reason': 'Cleanup',
        'account_id': '111111111111',
        'account_name': 'Test',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_module.handle_command_callback(
        action='deny',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['ok'] is True

    # 驗證 answer_callback 被呼叫
    mock_answer.assert_called_once()
    assert '❌' in mock_answer.call_args[0][1]

    # 驗證 DDB 狀態
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'denied'


@patch('callbacks_command.answer_callback')
@patch('callbacks_command.update_message')
def test_handle_command_callback_expired(mock_update, mock_answer, callbacks_module, mock_dynamodb):
    """測試 handle_command_callback 處理過期請求"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'test-req-007'

    # 建立過期請求
    expired_time = int(time.time()) - 100
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'command': 'aws s3 ls',
        'source': 'test-agent',
        'reason': 'Testing',
        'account_id': '111111111111',
        'account_name': 'Test',
        'created_at': expired_time - 600,
        'ttl': expired_time,  # 已過期
        'approval_expiry': expired_time
    })

    item = table.get_item(Key={'request_id': request_id})['Item']
    result = callbacks_module.handle_command_callback(
        action='approve',
        request_id=request_id,
        item=item,
        message_id=12345,
        callback_id='cb123',
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 被呼叫並提示過期
    mock_answer.assert_called_once()
    assert '過期' in mock_answer.call_args[0][1]
