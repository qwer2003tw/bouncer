"""
Bouncer - mcp_whoami.py 測試
覆蓋 bouncer_whoami tool 功能
"""

import json
import sys
import os
import pytest

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
                {'AttributeName': 'type', 'AttributeType': 'S'},
                {'AttributeName': 'expires_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'type-expires-at-index',
                    'KeySchema': [
                        {'AttributeName': 'type', 'KeyType': 'HASH'},
                        {'AttributeName': 'expires_at', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def mcp_whoami_module(mock_dynamodb):
    """載入 mcp_whoami 模組並注入 mock"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'
    os.environ['DEFAULT_ACCOUNT_ID'] = '123456789012'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['GRANT_SESSION_ENABLED'] = 'true'
    os.environ['TRUST_SESSION_ENABLED'] = 'true'
    os.environ['TRUST_RATE_LIMIT_ENABLED'] = 'false'
    os.environ['RATE_LIMIT_ENABLED'] = 'true'
    os.environ['BOUNCER_IP_BINDING_MODE'] = 'relaxed'

    # 清除模組
    modules_to_clear = [
        'mcp_whoami', 'db', 'constants', 'utils'
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import mcp_whoami
    yield mcp_whoami

    sys.path.pop(0)


# ============================================================================
# Tests for mcp_tool_whoami
# ============================================================================

def test_whoami_returns_version_and_config(mcp_whoami_module):
    """測試 mcp_tool_whoami 回傳版本和配置資訊"""
    req_id = 'test-whoami-001'
    arguments = {}

    result = mcp_whoami_module.mcp_tool_whoami(req_id, arguments)

    # 應回傳成功
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)

    # 驗證基本欄位
    assert 'version' in data
    assert 'default_account_id' in data
    assert 'region' in data
    assert 'features' in data

    # 驗證環境變數讀取正確
    assert data['default_account_id'] == '123456789012'
    assert data['region'] == 'us-west-2'

    # 驗證 feature flags
    features = data['features']
    assert features['grant_enabled'] is True
    assert features['trust_enabled'] is True
    assert features['trust_rate_limit_enabled'] is False
    assert features['rate_limit_enabled'] is True
    assert features['ip_binding_mode'] == 'relaxed'

    # 驗證提示訊息
    assert 'uptime_hint' in data


def test_whoami_with_default_feature_flags(mcp_whoami_module):
    """測試 mcp_tool_whoami 在缺少環境變數時使用預設值"""
    # 清除部分環境變數
    for key in ['GRANT_SESSION_ENABLED', 'TRUST_SESSION_ENABLED',
                'TRUST_RATE_LIMIT_ENABLED', 'RATE_LIMIT_ENABLED',
                'BOUNCER_IP_BINDING_MODE']:
        if key in os.environ:
            del os.environ[key]

    # 重新載入模組以套用環境變數變更
    import importlib
    importlib.reload(mcp_whoami_module)

    req_id = 'test-whoami-002'
    arguments = {}

    result = mcp_whoami_module.mcp_tool_whoami(req_id, arguments)
    body = json.loads(result['body'])
    content = body['result']['content'][0]['text']
    data = json.loads(content)

    # 驗證預設值
    features = data['features']
    assert features['grant_enabled'] is True  # default 'true'
    assert features['trust_enabled'] is True  # default 'true'
    assert features['trust_rate_limit_enabled'] is True  # default 'true'
    assert features['rate_limit_enabled'] is True  # default 'true'
    assert features['ip_binding_mode'] == 'strict'  # default 'strict'


def test_whoami_no_arguments_required(mcp_whoami_module):
    """測試 mcp_tool_whoami 不需要任何參數"""
    req_id = 'test-whoami-003'
    # 空參數字典
    arguments = {}

    result = mcp_whoami_module.mcp_tool_whoami(req_id, arguments)

    # 應回傳成功，不應有錯誤
    body = json.loads(result['body'])
    assert 'result' in body
    assert 'error' not in body
