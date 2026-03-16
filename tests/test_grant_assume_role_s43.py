"""Regression tests for grant_execute assume role (#127 fix)

Test coverage:
- grant.create_grant_request with project= looks up deploy_role_arn from DDB
- grant.create_grant_request with project=None sets assume_role_arn=''
- _check_grant_session uses grant's assume_role_arn when present
"""
import pytest
pytestmark = pytest.mark.xdist_group("grant_s43")

import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws
import boto3


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB for testing"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        # Create approval-requests table
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        # Create bouncer-projects table
        projects_table = dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        # Populate with test project
        projects_table.put_item(Item={
            'project_id': 'ztp-files',
            'deploy_role_arn': 'arn:aws:iam::190825685292:role/ztp-files-deploy-role'
        })
        yield {'table': table, 'projects_table': projects_table}


def test_create_grant_request_with_project_lookup_role(mock_dynamodb):
    """Test that create_grant_request with project= looks up deploy_role_arn from DDB"""
    from grant import create_grant_request
    import db

    # Point db.table to mock
    db.table = mock_dynamodb['table']

    result = create_grant_request(
        commands=['aws s3 ls s3://test-bucket'],
        reason='test deploy',
        source='test',
        account_id='190825685292',
        project='ztp-files',
    )

    # Verify project and assume_role_arn in result
    assert result['project'] == 'ztp-files'
    assert result['assume_role_arn'] == 'arn:aws:iam::190825685292:role/ztp-files-deploy-role'
    assert result['status'] == 'pending_approval'


def test_create_grant_request_without_project(mock_dynamodb):
    """Test that create_grant_request with project=None sets assume_role_arn=''"""
    from grant import create_grant_request
    import db

    db.table = mock_dynamodb['table']

    result = create_grant_request(
        commands=['aws s3 ls s3://test-bucket'],
        reason='test',
        source='test',
        account_id='123456789012',
        project=None,
    )

    assert result['project'] == ''
    assert result['assume_role_arn'] == ''


def test_create_grant_request_project_no_role(mock_dynamodb):
    """Test that create_grant_request with project= but DDB returns no role → assume_role_arn=''"""
    from grant import create_grant_request
    import db

    db.table = mock_dynamodb['table']

    # Add project without deploy_role_arn
    mock_dynamodb['projects_table'].put_item(Item={'project_id': 'test-project'})

    result = create_grant_request(
        commands=['aws s3 ls s3://test-bucket'],
        reason='test',
        source='test',
        account_id='123456789012',
        project='test-project',
    )

    assert result['project'] == 'test-project'
    assert result['assume_role_arn'] == ''


def test_create_grant_request_project_missing_in_ddb(mock_dynamodb):
    """Test that create_grant_request handles missing project gracefully"""
    from grant import create_grant_request
    import db

    db.table = mock_dynamodb['table']

    result = create_grant_request(
        commands=['aws s3 ls s3://test-bucket'],
        reason='test',
        source='test',
        account_id='123456789012',
        project='nonexistent-project',
    )

    # Should fail gracefully and set assume_role_arn=''
    assert result['project'] == 'nonexistent-project'
    assert result['assume_role_arn'] == ''


def test_check_grant_session_uses_grant_assume_role(mock_dynamodb):
    """Test that _check_grant_session uses grant's assume_role_arn when present"""
    from mcp_execute import _check_grant_session, ExecuteContext
    import db
    import time

    db.table = mock_dynamodb['table']

    # Create a grant with assume_role_arn
    grant_id = 'grant_test123'
    mock_dynamodb['table'].put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test',
        'account_id': '123456789012',
        'granted_commands': ['aws s3 ls s3://test-bucket'],
        'used_commands': {},
        'total_executions': 0,
        'max_total_executions': 50,
        'allow_repeat': False,
        'expires_at': int(time.time()) + 1800,
        'assume_role_arn': 'arn:aws:iam::190825685292:role/ztp-files-deploy-role',
    })

    ctx = ExecuteContext(
        req_id='req_123',
        command='aws s3 ls s3://test-bucket',
        reason='test',
        source='test',
        account_id='123456789012',
        account_name='test',
        assume_role='arn:aws:iam::123456789012:role/default-role',
        smart_decision=None,
        trust_scope='test',
        context='test',
        timeout=300,
        sync_mode=True,
        grant_id=grant_id,
    )

    with patch('mcp_execute.execute_command') as mock_execute, \
         patch('mcp_execute.store_paged_output') as mock_store, \
         patch('mcp_execute.send_grant_execute_notification') as mock_notify, \
         patch('mcp_execute.log_decision') as mock_log:

        mock_execute.return_value = 'output'
        mock_store.return_value = {'result': 'output', 'paged': False}

        result = _check_grant_session(ctx)

        # Verify execute_command was called with grant's assume_role_arn
        assert result is not None
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args
        assert call_args[0][0] == 'aws s3 ls s3://test-bucket'
        assert call_args[0][1] == 'arn:aws:iam::190825685292:role/ztp-files-deploy-role'


def test_check_grant_session_fallback_to_ctx_assume_role(mock_dynamodb):
    """Test that _check_grant_session falls back to ctx.assume_role when grant has no assume_role_arn"""
    from mcp_execute import _check_grant_session, ExecuteContext
    import db
    import time

    db.table = mock_dynamodb['table']

    # Create a grant without assume_role_arn
    grant_id = 'grant_test456'
    mock_dynamodb['table'].put_item(Item={
        'request_id': grant_id,
        'type': 'grant_session',
        'status': 'active',
        'source': 'test',
        'account_id': '123456789012',
        'granted_commands': ['aws s3 ls s3://test-bucket'],
        'used_commands': {},
        'total_executions': 0,
        'max_total_executions': 50,
        'allow_repeat': False,
        'expires_at': int(time.time()) + 1800,
    })

    ctx = ExecuteContext(
        req_id='req_123',
        command='aws s3 ls s3://test-bucket',
        reason='test',
        source='test',
        account_id='123456789012',
        account_name='test',
        assume_role='arn:aws:iam::123456789012:role/default-role',
        smart_decision=None,
        trust_scope='test',
        context='test',
        timeout=300,
        sync_mode=True,
        grant_id=grant_id,
    )

    with patch('mcp_execute.execute_command') as mock_execute, \
         patch('mcp_execute.store_paged_output') as mock_store, \
         patch('mcp_execute.send_grant_execute_notification') as mock_notify, \
         patch('mcp_execute.log_decision') as mock_log:

        mock_execute.return_value = 'output'
        mock_store.return_value = {'result': 'output', 'paged': False}

        result = _check_grant_session(ctx)

        # Verify execute_command was called with ctx.assume_role
        assert result is not None
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args
        assert call_args[0][0] == 'aws s3 ls s3://test-bucket'
        assert call_args[0][1] == 'arn:aws:iam::123456789012:role/default-role'
