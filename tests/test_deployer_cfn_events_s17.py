"""
Sprint17 - #55: get_deploy_status() CloudFormation failed_resources enrichment.

When SFN status transitions to FAILED/TIMED_OUT/ABORTED and the deploy record
has a stack_name, describe_stack_events is called and the results are surfaced
as failed_resources + error_summary in the returned record and DynamoDB.
"""
import os
import sys
import time
import pytest
import boto3
from unittest.mock import MagicMock, patch
from moto import mock_aws
from botocore.exceptions import ClientError


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================================
# Fixtures
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
    """Return a mock CFN client whose describe_stack_events returns *events*."""
    mock = MagicMock()
    mock.describe_stack_events.return_value = {'StackEvents': events}
    return mock


_SAMPLE_EVENTS = [
    {
        'LogicalResourceId': 'MyLambda',
        'ResourceStatus': 'CREATE_FAILED',
        'ResourceStatusReason': 'Resource creation cancelled',
    },
    {
        'LogicalResourceId': 'MyBucket',
        'ResourceStatus': 'UPDATE_FAILED',
        'ResourceStatusReason': 'Bucket policy conflict',
    },
    {
        'LogicalResourceId': 'MyRole',
        'ResourceStatus': 'ROLLBACK_FAILED',
        'ResourceStatusReason': 'Cannot rollback',
    },
    {
        'LogicalResourceId': 'MyApi',
        'ResourceStatus': 'DELETE_FAILED',
        'ResourceStatusReason': 'Dependency violation',
    },
    {
        'LogicalResourceId': 'MyDDB',
        'ResourceStatus': 'CREATE_FAILED',
        'ResourceStatusReason': 'Table already exists',
    },
    # 6th event — should be dropped (cap is 5)
    {
        'LogicalResourceId': 'Extra',
        'ResourceStatus': 'CREATE_FAILED',
        'ResourceStatusReason': 'Should be excluded',
    },
]


# ============================================================================
# Tests
# ============================================================================

class TestCFNEventsOnFailed:
    """When SFN FAILED + stack_name present, CFN events are fetched."""

    def test_failed_resources_in_response(self, dep):
        """failed_resources list is present in the returned record."""
        deploy_id = 'cfn-test-001'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(_SAMPLE_EVENTS)), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert 'failed_resources' in result, "failed_resources missing from response"
        fr = result['failed_resources']
        assert isinstance(fr, list)
        assert len(fr) <= 5, "At most 5 failed resources should be returned"
        assert len(fr) == 5, "Expected 5 (all failed events from _SAMPLE_EVENTS[:5])"

    def test_failed_resources_fields(self, dep):
        """Each entry has resource, status, reason keys."""
        deploy_id = 'cfn-test-002'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(_SAMPLE_EVENTS)), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        fr = result['failed_resources']
        first = fr[0]
        assert first['resource'] == 'MyLambda'
        assert first['status'] == 'CREATE_FAILED'
        assert first['reason'] == 'Resource creation cancelled'

    def test_error_summary_from_first_failed_event(self, dep):
        """error_summary is populated from the first failed event's reason."""
        deploy_id = 'cfn-test-003'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(_SAMPLE_EVENTS)), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert result.get('error_summary') == 'Resource creation cancelled'

    def test_failed_resources_stored_in_ddb(self, dep):
        """failed_resources and error_summary are persisted to DynamoDB."""
        deploy_id = 'cfn-test-004'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(_SAMPLE_EVENTS)), \
             patch('deployer.send_deploy_failure_notification'):
            dep.get_deploy_status(deploy_id)

        item = dep.history_table.get_item(Key={'deploy_id': deploy_id})['Item']
        assert 'failed_resources' in item
        assert 'error_summary' in item
        assert item['error_summary'] == 'Resource creation cancelled'

    def test_reason_truncated_to_200_chars(self, dep):
        """Reason fields are truncated to 200 characters."""
        deploy_id = 'cfn-test-005'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        long_reason = 'A' * 300
        events = [{
            'LogicalResourceId': 'BigResource',
            'ResourceStatus': 'CREATE_FAILED',
            'ResourceStatusReason': long_reason,
        }]

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(events)), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        fr = result['failed_resources']
        assert len(fr[0]['reason']) == 200

    def test_error_summary_truncated_to_300_chars(self, dep):
        """error_summary is truncated to 300 characters."""
        deploy_id = 'cfn-test-006'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        long_reason = 'B' * 400
        events = [{
            'LogicalResourceId': 'Res',
            'ResourceStatus': 'CREATE_FAILED',
            'ResourceStatusReason': long_reason,
        }]

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(events)), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert len(result['error_summary']) == 300


class TestCFNEventsEdgeCases:
    """Edge cases for CFN event enrichment."""

    def test_no_failed_events_gives_empty_list(self, dep):
        """When all CFN events are non-FAILED, failed_resources is []."""
        deploy_id = 'cfn-edge-001'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        ok_events = [
            {'LogicalResourceId': 'Res', 'ResourceStatus': 'CREATE_COMPLETE',
             'ResourceStatusReason': ''},
        ]
        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', _mock_cfn_events(ok_events)), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        assert result.get('failed_resources') == []
        assert result.get('error_summary') == ''

    def test_no_stack_name_skips_cfn(self, dep):
        """When stack_name is absent from record, CFN is NOT called."""
        deploy_id = 'cfn-edge-002'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        # Put record WITHOUT stack_name
        dep.history_table.put_item(Item={
            'deploy_id': deploy_id,
            'project_id': 'test-proj',
            'status': 'RUNNING',
            'execution_arn': arn,
            'started_at': int(time.time()) - 120,
        })

        mock_cfn = MagicMock()

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        mock_cfn.describe_stack_events.assert_not_called()
        assert 'failed_resources' not in result

    def test_cfn_exception_does_not_crash(self, dep):
        """If describe_stack_events raises, get_deploy_status returns gracefully."""
        deploy_id = 'cfn-edge-003'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = MagicMock()
        mock_cfn.describe_stack_events.side_effect = ClientError({'Error': {'Code': 'Throttling', 'Message': 'CFN API error'}}, 'DescribeStackEvents')

        with patch.object(dep, 'sfn_client', _mock_sfn('FAILED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        # Must not raise; status still FAILED
        assert result['status'] == 'FAILED'
        # failed_resources NOT in result (exception path skips it)
        assert 'failed_resources' not in result

    def test_timed_out_also_queries_cfn(self, dep):
        """TIMED_OUT SFN status triggers CFN events lookup."""
        deploy_id = 'cfn-edge-004'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = _mock_cfn_events(_SAMPLE_EVENTS[:2])

        with patch.object(dep, 'sfn_client', _mock_sfn('TIMED_OUT')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        mock_cfn.describe_stack_events.assert_called_once_with(StackName='my-cfn-stack')
        assert 'failed_resources' in result

    def test_aborted_also_queries_cfn(self, dep):
        """ABORTED SFN status triggers CFN events lookup."""
        deploy_id = 'cfn-edge-005'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = _mock_cfn_events(_SAMPLE_EVENTS[:1])

        with patch.object(dep, 'sfn_client', _mock_sfn('ABORTED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        mock_cfn.describe_stack_events.assert_called_once_with(StackName='my-cfn-stack')

    def test_succeeded_does_not_query_cfn(self, dep):
        """SUCCEEDED SFN status does NOT trigger CFN events lookup."""
        deploy_id = 'cfn-edge-006'
        arn = f'arn:aws:states:us-east-1:123:execution:x:{deploy_id}'
        _put_running(dep, deploy_id, arn)

        mock_cfn = MagicMock()

        with patch.object(dep, 'sfn_client', _mock_sfn('SUCCEEDED')), \
             patch.object(dep, 'cfn_client', mock_cfn), \
             patch('deployer.send_deploy_failure_notification'):
            result = dep.get_deploy_status(deploy_id)

        mock_cfn.describe_stack_events.assert_not_called()
        assert 'failed_resources' not in result
