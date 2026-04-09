import json
"""
Bouncer - callbacks_grant.py 測試
覆蓋 Grant 審批 callback 的核心邏輯
"""

import sys
import os
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
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def callbacks_grant_module(mock_dynamodb):
    """載入 callbacks_grant 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'callbacks_grant', 'db', 'constants', 'grant', 'telegram', 'utils'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import callbacks_grant
    yield callbacks_grant

    sys.path.pop(0)


# ============================================================================
# Tests for handle_grant_approve (mode='all')
# ============================================================================

@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_all_success(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve mode='all' 成功批准"""
    grant_id = 'grant-001'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock get_grant_session (no dangerous commands)
    mock_get.return_value = {
        'grant_id': grant_id,
        'commands_detail': [
            {'command': 's3:list_objects_v2', 'category': 'safe'},
            {'command': 'ec2:describe_instances', 'category': 'safe'}
        ]
    }

    # Mock approve_grant 回傳
    mock_approve.return_value = {
        'grant_id': grant_id,
        'status': 'active',
        'granted_commands': ['s3:list_objects_v2', 'ec2:describe_instances'],
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='all')

    # 驗證回應
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['ok'] is True

    # 驗證 approve_grant 被呼叫
    mock_approve.assert_called_once_with(grant_id, '123456', mode='all')

    # 驗證 answer_callback 不顯示 alert（沒有危險命令）
    mock_answer.assert_called_once()
    call_args = mock_answer.call_args
    assert call_args[1].get('show_alert', False) is False

    # 驗證 update_message 顯示成功
    mock_update.assert_called_once()
    call_args = mock_update.call_args[0][1]
    assert '✅' in call_args
    assert grant_id in call_args


@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_all_with_dangerous_commands(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve mode='all' 包含危險命令時顯示 alert"""
    grant_id = 'grant-002'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock get_grant_session (有危險命令)
    mock_get.return_value = {
        'grant_id': grant_id,
        'commands_detail': [
            {'command': 's3:delete_bucket', 'category': 'requires_individual'},
            {'command': 'ec2:terminate_instances', 'category': 'requires_individual'}
        ]
    }

    # Mock approve_grant 回傳
    mock_approve.return_value = {
        'grant_id': grant_id,
        'status': 'active',
        'granted_commands': ['s3:delete_bucket', 'ec2:terminate_instances'],
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='all')

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 顯示 alert
    mock_answer.assert_called_once()
    call_args = mock_answer.call_args
    assert call_args[1].get('show_alert', False) is True
    assert '⚠️' in call_args[0][1]


@patch('grant.approve_grant')
@patch('callbacks_grant.answer_callback')
def test_grant_approve_not_found(mock_answer, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve grant 不存在"""
    grant_id = 'nonexistent-grant'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock approve_grant 回傳 None（不存在）
    mock_approve.return_value = None

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='all')

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示錯誤
    mock_answer.assert_called_once()
    assert '不存在' in mock_answer.call_args[0][1] or '已處理' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for handle_grant_approve (mode='safe_only')
# ============================================================================

@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_safe_only_success(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve mode='safe_only' 成功批准"""
    grant_id = 'grant-003'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock get_grant_session
    mock_get.return_value = {
        'grant_id': grant_id,
        'commands_detail': [
            {'command': 's3:list_objects_v2', 'category': 'safe'}
        ]
    }

    # Mock approve_grant 回傳
    mock_approve.return_value = {
        'grant_id': grant_id,
        'status': 'active',
        'granted_commands': ['s3:list_objects_v2'],
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='safe_only')

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 approve_grant 被呼叫正確 mode
    mock_approve.assert_called_once_with(grant_id, '123456', mode='safe_only')

    # 驗證 update_message 顯示「僅安全」
    mock_update.assert_called_once()
    call_args = mock_update.call_args[0][1]
    assert '僅安全' in call_args or '安全' in call_args


# ============================================================================
# Tests for handle_grant_approve_all (alias)
# ============================================================================

@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_all_alias(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve_all 是 handle_grant_approve 的 alias"""
    grant_id = 'grant-004'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock
    mock_get.return_value = {'grant_id': grant_id, 'commands_detail': []}
    mock_approve.return_value = {
        'grant_id': grant_id,
        'granted_commands': [],
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve_all(query, grant_id)

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 approve_grant 被呼叫 mode='all'
    mock_approve.assert_called_once_with(grant_id, '123456', mode='all')


# ============================================================================
# Tests for handle_grant_approve_safe (alias)
# ============================================================================

@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_safe_alias(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve_safe 是 handle_grant_approve 的 alias"""
    grant_id = 'grant-005'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock
    mock_get.return_value = {'grant_id': grant_id, 'commands_detail': []}
    mock_approve.return_value = {
        'grant_id': grant_id,
        'granted_commands': [],
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve_safe(query, grant_id)

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 approve_grant 被呼叫 mode='safe_only'
    mock_approve.assert_called_once_with(grant_id, '123456', mode='safe_only')


# ============================================================================
# Tests for handle_grant_deny
# ============================================================================

@patch('grant.deny_grant')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_deny_success(mock_update, mock_answer, mock_deny, callbacks_grant_module):
    """測試 handle_grant_deny 成功拒絕"""
    grant_id = 'grant-006'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock deny_grant 成功
    mock_deny.return_value = True

    result = callbacks_grant_module.handle_grant_deny(query, grant_id)

    # 驗證回應
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['ok'] is True

    # 驗證 deny_grant 被呼叫
    mock_deny.assert_called_once_with(grant_id)

    # 驗證 answer_callback 顯示拒絕
    mock_answer.assert_called_once()
    assert '❌' in mock_answer.call_args[0][1]

    # 驗證 update_message 顯示拒絕
    mock_update.assert_called_once()
    call_args = mock_update.call_args[0][1]
    assert '❌' in call_args
    assert '拒絕' in call_args
    assert grant_id in call_args


@patch('grant.deny_grant')
@patch('callbacks_grant.answer_callback')
def test_grant_deny_failure(mock_answer, mock_deny, callbacks_grant_module):
    """測試 handle_grant_deny 拒絕失敗"""
    grant_id = 'grant-007'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock deny_grant 失敗
    mock_deny.return_value = False

    result = callbacks_grant_module.handle_grant_deny(query, grant_id)

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示失敗
    mock_answer.assert_called_once()
    assert '失敗' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for error handling
# ============================================================================

@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
def test_grant_approve_client_error(mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve 處理 ClientError"""
    from botocore.exceptions import ClientError

    grant_id = 'grant-008'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock get_grant_session
    mock_get.return_value = {'grant_id': grant_id, 'commands_detail': []}

    # Mock approve_grant 拋出 ClientError
    mock_approve.side_effect = ClientError(
        {'Error': {'Code': 'ServiceUnavailable', 'Message': 'Service unavailable'}},
        'UpdateItem'
    )

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='all')

    # 驗證回應
    assert result['statusCode'] == 500

    # 驗證 answer_callback 顯示錯誤
    mock_answer.assert_called_once()
    assert '失敗' in mock_answer.call_args[0][1]


@patch('grant.deny_grant')
@patch('callbacks_grant.answer_callback')
def test_grant_deny_timeout_error(mock_answer, mock_deny, callbacks_grant_module):
    """測試 handle_grant_deny 處理 TimeoutError"""
    grant_id = 'grant-009'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock deny_grant 拋出 TimeoutError
    mock_deny.side_effect = TimeoutError('Connection timeout')

    result = callbacks_grant_module.handle_grant_deny(query, grant_id)

    # 驗證回應
    assert result['statusCode'] == 500

    # 驗證 answer_callback 顯示錯誤
    mock_answer.assert_called_once()
    assert '失敗' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for edge cases
# ============================================================================

@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_empty_granted_commands(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve 批准後沒有命令（可能全被過濾）"""
    grant_id = 'grant-010'
    query = {
        'id': 'cb123',
        'from': {'id': 123456},
        'message': {'message_id': 12345}
    }

    # Mock
    mock_get.return_value = {'grant_id': grant_id, 'commands_detail': []}
    mock_approve.return_value = {
        'grant_id': grant_id,
        'granted_commands': [],  # 空的
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='safe_only')

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 update_message 顯示 0 個命令
    mock_update.assert_called_once()
    call_args = mock_update.call_args[0][1]
    assert '0 個' in call_args or '0個' in call_args


@patch('grant.approve_grant')
@patch('grant.get_grant_session')
@patch('callbacks_grant.answer_callback')
@patch('callbacks_grant.update_message')
def test_grant_approve_missing_user_id(mock_update, mock_answer, mock_get, mock_approve, callbacks_grant_module):
    """測試 handle_grant_approve 缺少 user_id"""
    grant_id = 'grant-011'
    query = {
        'id': 'cb123',
        'from': {},  # 沒有 id
        'message': {'message_id': 12345}
    }

    # Mock
    mock_get.return_value = {'grant_id': grant_id, 'commands_detail': []}
    mock_approve.return_value = {
        'grant_id': grant_id,
        'granted_commands': [],
        'ttl_minutes': 30
    }

    result = callbacks_grant_module.handle_grant_approve(query, grant_id, mode='all')

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 approve_grant 被呼叫（user_id 應該是空字串或預設值）
    mock_approve.assert_called_once()
    user_id_arg = mock_approve.call_args[0][1]
    assert user_id_arg == '' or isinstance(user_id_arg, str)
