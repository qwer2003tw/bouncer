"""
Sprint20 - #87: get_deploy_status() cfn_failure_events enrichment.

When SFN status transitions to FAILED/TIMED_OUT/ABORTED, deploy_status should
expose cfn_failure_events with richer format:
  logical_resource_id, resource_status, reason, timestamp.
"""
import os
import sys
import time
import datetime
import pytest
import boto3
from unittest.mock import MagicMock, patch
from moto import mock_aws


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================================
# Fixtures (reuse same pattern as test_deployer_cfn_events_s17)
# ============================================================================

@pytest.fixture(scope='module')
def _ddb():
    """Minimal moto DynamoDB with deploy-history + deploy-locks + projects tables."""
    with mock_aws():
        dyn = boto3.resource('dynamodb', region_name='us-east-1')
        dyn.create_table(
            TableName='bouncer-deploy-history',
            KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                {'AttributeName': 'project_id', 'AttributeType': 'S'},
                {'AttributeName': 'started_at', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[{
                'IndexName': 'project-started-index',
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'started_at', 'KeyType': 'RANGE'},
                ],
                'Projection': {'ProjectionType': 'ALL'},
            }],
            BillingMode='PAY_PER_REQUEST',
        )
        dyn.create_table(
            TableName='bouncer-deploy-locks',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
        dyn.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
        yield dyn


@pytest.fixture()
def dep(_ddb):
    """Fresh deployer module with moto-backed tables, reset between tests."""
    for mod in list(sys.modules.keys()):
        if 'deployer' in mod:
            del sys.modules[mod]

    import deployer as d
    d.history_table = _ddb.Table('bouncer-deploy-history')
    d.locks_table   = _ddb.Table('bouncer-deploy-locks')
    d.projects_table = _ddb.Table('bouncer-projects')
    d.sfn_client = None
    d.cfn_client = None
    yield d


def _put_running(dep, deploy_id, execution_arn, stack_name='my-cfn-stack'):
    dep.history_table.put_item(Item={
        'deploy_id': deploy_id,
        'project_id': 'test-proj',
        'status': 'RUNNING',
        'execution_arn': execution_arn,
        'started_at': int(time.time()) - 120,
        'stack_name': stack_name,
    })


def _mock_sfn(status='FAILED'):
    mock = MagicMock()
    mock.describe_execution.return_value = {'status': status}
    mock.get_execution_history.return_value = {'events': []}
    return mock


def _mock_cfn_events(events):
    mock = MagicMock()
    mock.describe_stack_events.return_value = {'StackEvents': events}
    return mock


# Sample events with Timestamp as datetime (as returned by real boto3)
_TS1 = datetime.datetime(2026, 3, 9, 7, 19, 0, tzinfo=datetime.timezone.utc)
_TS2 = datetime.datetime(2026, 3, 9, 7, 20, 0, tzinfo=datetime.timezone.utc)

_SAMPLE_EVENTS_WITH_TS = [
    {
        'LogicalResourceId': 'ApprovalFunction',
        'ResourceStatus': 'UPDATE_FAILED',
        'ResourceStatusReason': 'Resource handler returned message: Lambda function error',
        'Timestamp': _TS1,
    },
    {
        'LogicalResourceId': 'ApiGateway',
        'ResourceStatus': 'CREATE_FAILED',
        'ResourceStatusReason': 'Gateway conflict',
        'Timestamp': _TS2,
    },
]

_SAMPLE_EVENTS_NO_TS = [
    {
        'LogicalResourceId': 'MyQueue',
        'ResourceStatus': 'UPDATE_FAILED',
        'ResourceStatusReason': 'Queue already exists',
        # No Timestamp field — should degrade gracefully
    },
]


# ============================================================================
# Tests
# ============================================================================

class TestCfnFailureEvents:
    """cfn_failure_events key present on FAILED deploy_status (#87)."""

    def test_cfn_failure_events_present(self, dep):
        """cfn_failure_events is included in the response when FAILED."""
        deploy_id = 's20-cfe-001'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = _mock_cfn_events(_SAMPLE_EVENTS_WITH_TS)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert 'cfn_failure_events' in result, "cfn_failure_events missing from FAILED response"

    def test_cfn_failure_events_fields(self, dep):
        """cfn_failure_events entries have correct keys."""
        deploy_id = 's20-cfe-002'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = _mock_cfn_events(_SAMPLE_EVENTS_WITH_TS)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        events = result['cfn_failure_events']
        assert isinstance(events, list)
        assert len(events) == 2

        first = events[0]
        assert first['logical_resource_id'] == 'ApprovalFunction'
        assert first['resource_status'] == 'UPDATE_FAILED'
        assert 'Lambda function error' in first['reason']
        assert first['timestamp'] == '2026-03-09T07:19:00Z'

    def test_cfn_failure_events_timestamp_isoformat(self, dep):
        """Timestamps are formatted as ISO 8601 UTC strings."""
        deploy_id = 's20-cfe-003'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = _mock_cfn_events(_SAMPLE_EVENTS_WITH_TS)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        for ev in result['cfn_failure_events']:
            ts = ev['timestamp']
            # Must be non-empty string
            assert isinstance(ts, str) and len(ts) > 0, f"Bad timestamp: {ts!r}"

    def test_cfn_failure_events_no_timestamp_field(self, dep):
        """Events without Timestamp degrade gracefully (empty string)."""
        deploy_id = 's20-cfe-004'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = _mock_cfn_events(_SAMPLE_EVENTS_NO_TS)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert 'cfn_failure_events' in result
        ev = result['cfn_failure_events'][0]
        assert ev['logical_resource_id'] == 'MyQueue'
        assert isinstance(ev['timestamp'], str)

    def test_cfn_failure_events_max_five(self, dep):
        """At most 5 events are returned in cfn_failure_events."""
        deploy_id = 's20-cfe-005'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        many_events = [
            {
                'LogicalResourceId': f'Resource{i}',
                'ResourceStatus': 'UPDATE_FAILED',
                'ResourceStatusReason': f'Error {i}',
                'Timestamp': _TS1,
            }
            for i in range(8)
        ]
        mock_cfn = _mock_cfn_events(many_events)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert len(result['cfn_failure_events']) <= 5

    def test_cfn_failure_events_not_present_on_success(self, dep):
        """cfn_failure_events NOT present when SUCCEEDED."""
        deploy_id = 's20-cfe-006'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = MagicMock()

        with patch.object(dep, 'sfn_client', _mock_sfn('SUCCEEDED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert 'cfn_failure_events' not in result
        mock_cfn.describe_stack_events.assert_not_called()
