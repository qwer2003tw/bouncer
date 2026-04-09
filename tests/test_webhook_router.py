"""
Bouncer - webhook_router.py 測試
覆蓋 webhook callback routing 的核心邏輯
"""

import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock

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

        # Deployer table (for infra approval tests)
        dynamodb.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'deploy_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )

        yield dynamodb


@pytest.fixture
def webhook_router_module(mock_dynamodb):
    """載入 webhook_router 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'

    # 清除模組
    modules_to_clear = [
        'webhook_router', 'db', 'constants', 'telegram', 'utils',
        'metrics', 'callbacks', 'callbacks_command', 'callbacks_upload',
        'callbacks_grant', 'callbacks_query_logs', 'trust', 'notifications',
        'deployer'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import webhook_router
    yield webhook_router

    sys.path.pop(0)


# ============================================================================
# Tests for handle_show_page
# ============================================================================

@patch('webhook_router.handle_show_page_callback')
@patch('webhook_router.answer_callback')
@patch('webhook_router.emit_metric')
def test_handle_show_page_success(mock_emit, mock_answer, mock_handler, webhook_router_module):
    """測試 handle_show_page 成功處理分頁請求"""
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock handler 回傳
    mock_handler.return_value = {'statusCode': 200, 'body': {'ok': True}}

    result = webhook_router_module.handle_show_page('req123:2', callback)

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 handler 被呼叫正確參數
    mock_handler.assert_called_once_with(callback, 'req123', 2)

    # 驗證 metric 被記錄
    mock_emit.assert_called_once()


@patch('webhook_router.answer_callback')
def test_handle_show_page_invalid_format(mock_answer, webhook_router_module):
    """測試 handle_show_page 處理無效的 request_id 格式"""
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    result = webhook_router_module.handle_show_page('invalid-format', callback)

    # 驗證回應
    assert result['statusCode'] == 400

    # 驗證 answer_callback 提示錯誤
    mock_answer.assert_called_once()
    assert '無效' in mock_answer.call_args[0][1]


@patch('webhook_router.answer_callback')
def test_handle_show_page_invalid_page_number(mock_answer, webhook_router_module):
    """測試 handle_show_page 處理無效的頁碼"""
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    result = webhook_router_module.handle_show_page('req123:abc', callback)

    # 驗證回應
    assert result['statusCode'] == 400

    # 驗證 answer_callback 提示錯誤
    mock_answer.assert_called_once()
    assert '無效' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for handle_infra_approval
# ============================================================================

@patch('webhook_router.get_deploy_record')
@patch('webhook_router.update_deploy_record')
@patch('webhook_router.answer_callback')
@patch('webhook_router.update_message')
def test_handle_infra_approval_approve(mock_update, mock_answer, mock_update_rec, mock_get_rec, webhook_router_module):
    """測試 handle_infra_approval 批准 infra 變更"""
    deploy_id = 'deploy-001'
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock deploy record with valid token
    mock_get_rec.return_value = {
        'deploy_id': deploy_id,
        'infra_approval_token': 'valid-token',
        'infra_approval_token_ttl': int(time.time()) + 600  # 未過期
    }

    with patch('webhook_router._boto3.client') as mock_boto:
        mock_sfn = MagicMock()
        mock_boto.return_value = mock_sfn

        result = webhook_router_module.handle_infra_approval(
            action='infra_approve',
            request_id=deploy_id,
            callback=callback,
            user_id='user123'
        )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 SFN send_task_success 被呼叫
    mock_sfn.send_task_success.assert_called_once()

    # 驗證 deploy record 被更新
    mock_update_rec.assert_called_once()


@patch('webhook_router.get_deploy_record')
@patch('webhook_router.answer_callback')
def test_handle_infra_approval_no_token(mock_answer, mock_get_rec, webhook_router_module):
    """測試 handle_infra_approval 沒有 task token"""
    deploy_id = 'deploy-002'
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock deploy record without token
    mock_get_rec.return_value = {
        'deploy_id': deploy_id
    }

    result = webhook_router_module.handle_infra_approval(
        action='infra_approve',
        request_id=deploy_id,
        callback=callback,
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示錯誤
    mock_answer.assert_called_once()
    assert 'token' in mock_answer.call_args[0][1].lower()


@patch('webhook_router.get_deploy_record')
@patch('webhook_router.answer_callback')
@patch('webhook_router.update_message')
def test_handle_infra_approval_expired_token(mock_update, mock_answer, mock_get_rec, webhook_router_module):
    """測試 handle_infra_approval token 已過期"""
    deploy_id = 'deploy-003'
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock deploy record with expired token
    expired_time = int(time.time()) - 100
    mock_get_rec.return_value = {
        'deploy_id': deploy_id,
        'infra_approval_token': 'expired-token',
        'infra_approval_token_ttl': expired_time
    }

    result = webhook_router_module.handle_infra_approval(
        action='infra_approve',
        request_id=deploy_id,
        callback=callback,
        user_id='user123'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示過期
    mock_answer.assert_called_once()
    assert '過期' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for handle_revoke_trust
# ============================================================================

@patch('webhook_router.revoke_trust_session')
@patch('webhook_router.send_trust_session_summary')
@patch('webhook_router.update_message')
@patch('webhook_router.answer_callback')
@patch('webhook_router.emit_metric')
def test_handle_revoke_trust_success(mock_emit, mock_answer, mock_update, mock_summary, mock_revoke, webhook_router_module, mock_dynamodb):
    """測試 handle_revoke_trust 成功撤銷信任時段"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    trust_id = 'trust-001'

    # 建立 trust session
    table.put_item(Item={
        'request_id': trust_id,
        'type': 'trust_session',
        'status': 'active',
        'trust_scope': 'deploy',
        'created_at': int(time.time())
    })

    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock revoke 成功
    mock_revoke.return_value = True

    result = webhook_router_module.handle_revoke_trust(trust_id, callback)

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 revoke_trust_session 被呼叫
    mock_revoke.assert_called_once_with(trust_id)

    # 驗證 update_message 顯示成功
    mock_update.assert_called_once()
    call_args = mock_update.call_args[0][1]
    assert '🛑' in call_args

    # 驗證 summary 被發送
    mock_summary.assert_called_once()


@patch('webhook_router.revoke_trust_session')
@patch('webhook_router.answer_callback')
def test_handle_revoke_trust_failure(mock_answer, mock_revoke, webhook_router_module):
    """測試 handle_revoke_trust 撤銷失敗"""
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock revoke 失敗
    mock_revoke.return_value = False

    result = webhook_router_module.handle_revoke_trust('nonexistent-trust', callback)

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示失敗
    mock_answer.assert_called_once()
    assert '❌' in mock_answer.call_args[0][1]


# ============================================================================
# Tests for handle_grant_callbacks
# ============================================================================

@patch('webhook_router.handle_grant_approve_all')
@patch('webhook_router._is_grant_expired')
@patch('webhook_router.emit_metric')
def test_handle_grant_callbacks_approve_all(mock_emit, mock_expired, mock_handler, webhook_router_module):
    """測試 handle_grant_callbacks grant_approve_all action"""
    callback = {'id': 'cb123'}
    grant_id = 'grant-001'

    # Mock grant 未過期
    mock_expired.return_value = False

    # Mock handler 回傳
    mock_handler.return_value = {'statusCode': 200, 'body': {'ok': True}}

    result = webhook_router_module.handle_grant_callbacks(
        action='grant_approve_all',
        request_id=grant_id,
        callback=callback
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 handler 被呼叫
    mock_handler.assert_called_once_with(callback, grant_id)


@patch('webhook_router._is_grant_expired')
@patch('webhook_router.emit_metric')
def test_handle_grant_callbacks_expired(mock_emit, mock_expired, webhook_router_module):
    """測試 handle_grant_callbacks grant 已過期"""
    callback = {'id': 'cb123'}
    grant_id = 'grant-002'

    # Mock grant 已過期
    mock_expired.return_value = True

    result = webhook_router_module.handle_grant_callbacks(
        action='grant_approve_all',
        request_id=grant_id,
        callback=callback
    )

    # 驗證回應（early return）
    assert result['statusCode'] == 200


# ============================================================================
# Tests for handle_general_approval
# ============================================================================

@patch('webhook_router.handle_command_callback')
@patch('webhook_router.answer_callback')
@patch('webhook_router.emit_metric')
def test_handle_general_approval_command_success(mock_emit, mock_answer, mock_handler, webhook_router_module, mock_dynamodb):
    """測試 handle_general_approval 處理命令審批"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'cmd-req-001'

    # 建立命令請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'execute',
        'command': 'aws s3 ls',
        'source': 'test-agent',
        'reason': 'Testing',
        'account_id': '111111111111',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock handler 回傳
    mock_handler.return_value = {'statusCode': 200, 'body': {'ok': True}}

    result = webhook_router_module.handle_general_approval(
        action='approve',
        request_id=request_id,
        callback=callback,
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 handler 被呼叫
    mock_handler.assert_called_once()


@patch('webhook_router.answer_callback')
def test_handle_general_approval_not_found(mock_answer, webhook_router_module, mock_dynamodb):
    """測試 handle_general_approval 請求不存在"""
    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    result = webhook_router_module.handle_general_approval(
        action='approve',
        request_id='nonexistent-req',
        callback=callback,
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 404

    # 驗證 answer_callback 提示錯誤
    mock_answer.assert_called_once()
    assert '過期' in mock_answer.call_args[0][1] or '不存在' in mock_answer.call_args[0][1]


@patch('webhook_router.answer_callback')
def test_handle_general_approval_already_processed(mock_answer, webhook_router_module, mock_dynamodb):
    """測試 handle_general_approval 請求已處理"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'cmd-req-002'

    # 建立已處理的請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'approved',  # 已處理
        'action': 'execute',
        'command': 'aws s3 ls',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    result = webhook_router_module.handle_general_approval(
        action='approve',
        request_id=request_id,
        callback=callback,
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 answer_callback 提示已處理
    mock_answer.assert_called_once()
    assert '已處理' in mock_answer.call_args[0][1]


@patch('webhook_router.answer_callback')
@patch('webhook_router.update_message')
def test_handle_general_approval_expired_ttl(mock_update, mock_answer, webhook_router_module, mock_dynamodb):
    """測試 handle_general_approval 請求已過期"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'cmd-req-003'

    # 建立過期請求
    expired_time = int(time.time()) - 100
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'execute',
        'command': 'aws s3 ls',
        'source': 'test-agent',
        'reason': 'Testing',
        'created_at': expired_time - 600,
        'ttl': expired_time  # 已過期
    })

    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    result = webhook_router_module.handle_general_approval(
        action='approve',
        request_id=request_id,
        callback=callback,
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200
    assert result['body'].get('expired') is True

    # 驗證 answer_callback 提示過期
    mock_answer.assert_called_once()
    assert '過期' in mock_answer.call_args[0][1]

    # 驗證 DDB 狀態更新為 timeout
    item = table.get_item(Key={'request_id': request_id})['Item']
    assert item['status'] == 'timeout'


@patch('webhook_router.handle_upload_callback')
@patch('webhook_router.emit_metric')
def test_handle_general_approval_upload_action(mock_emit, mock_handler, webhook_router_module, mock_dynamodb):
    """測試 handle_general_approval 路由到 upload handler"""
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    request_id = 'upload-req-001'

    # 建立上傳請求
    created_at = int(time.time())
    table.put_item(Item={
        'request_id': request_id,
        'status': 'pending_approval',
        'action': 'upload',  # 上傳 action
        'bucket': 'test-bucket',
        'key': 'test.txt',
        'created_at': created_at,
        'ttl': created_at + 600
    })

    callback = {
        'id': 'cb123',
        'message': {'message_id': 12345}
    }

    # Mock handler 回傳
    mock_handler.return_value = {'statusCode': 200, 'body': {'ok': True}}

    result = webhook_router_module.handle_general_approval(
        action='approve',
        request_id=request_id,
        callback=callback,
        user_id='user123',
        source_ip='1.2.3.4'
    )

    # 驗證回應
    assert result['statusCode'] == 200

    # 驗證 upload handler 被呼叫
    mock_handler.assert_called_once()
