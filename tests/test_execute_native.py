"""
Test bouncer_execute_native MCP tool — boto3 native execution without awscli
"""
import json
import pytest
from unittest.mock import patch, MagicMock, Mock
from moto import mock_aws
import boto3
from botocore.exceptions import ClientError


@pytest.fixture
def mock_table():
    """Mock DynamoDB table"""
    with patch('mcp_execute.table') as mock:
        yield mock


@pytest.fixture
def mock_notifications():
    """Mock notification functions"""
    with patch('mcp_execute.send_approval_request') as mock_approval, \
         patch('mcp_execute.send_trust_auto_approve_notification') as mock_trust, \
         patch('mcp_execute.send_blocked_notification') as mock_blocked:
        mock_approval.return_value = MagicMock(ok=True, message_id='msg-123')
        yield {
            'approval': mock_approval,
            'trust': mock_trust,
            'blocked': mock_blocked,
        }


class TestExecuteBoto3Native:
    """Test execute_boto3_native function"""

    @mock_aws
    def test_execute_native_s3_list_buckets_success(self):
        """Test successful S3 list_buckets call"""
        from commands import execute_boto3_native

        # Create a test bucket
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-bucket-native')

        # Execute native call
        result = execute_boto3_native(
            service='s3',
            operation='list_buckets',
            params={},
            region='us-east-1',
        )

        # Verify result
        assert '❌' not in result
        result_data = json.loads(result)
        assert 'Buckets' in result_data
        assert any(b['Name'] == 'test-bucket-native' for b in result_data['Buckets'])

    def test_execute_native_unsupported_operation(self):
        """Test error when operation doesn't exist"""
        from commands import execute_boto3_native

        result = execute_boto3_native(
            service='s3',
            operation='nonexistent_operation',
            params={},
            region='us-east-1',
        )

        assert result.startswith('❌ 不支援的操作:')
        assert 's3.nonexistent_operation' in result

    @mock_aws
    def test_execute_native_with_client_error(self):
        """Test handling of boto3 ClientError"""
        from commands import execute_boto3_native

        # Try to get nonexistent bucket
        result = execute_boto3_native(
            service='s3',
            operation='get_bucket_location',
            params={'Bucket': 'nonexistent-bucket-12345'},
            region='us-east-1',
        )

        assert result.startswith('❌ AWS API 錯誤:')
        assert 'NoSuchBucket' in result or 'does not exist' in result.lower()

    @mock_aws
    def test_execute_native_with_assume_role(self):
        """Test execution with assume role"""
        from commands import execute_boto3_native

        # Create IAM role
        iam = boto3.client('iam', region_name='us-east-1')
        iam.create_role(
            RoleName='test-bouncer-role',
            AssumeRolePolicyDocument=json.dumps({
                'Version': '2012-10-17',
                'Statement': [{
                    'Effect': 'Allow',
                    'Principal': {'Service': 'lambda.amazonaws.com'},
                    'Action': 'sts:AssumeRole'
                }]
            })
        )

        # Mock STS assume_role
        with patch('boto3.client') as mock_client:
            mock_sts = MagicMock()
            mock_s3 = MagicMock()

            def client_factory(service, **kwargs):
                if service == 'sts':
                    return mock_sts
                elif service == 's3':
                    return mock_s3
                return MagicMock()

            mock_client.side_effect = client_factory

            # Configure assume_role response
            mock_sts.assume_role.return_value = {
                'Credentials': {
                    'AccessKeyId': 'ASIA123',
                    'SecretAccessKey': 'secret123',
                    'SessionToken': 'token123',
                }
            }

            # Configure S3 response
            mock_s3.list_buckets.return_value = {'Buckets': []}

            # Execute with assume role
            result = execute_boto3_native(
                service='s3',
                operation='list_buckets',
                params={},
                region='us-east-1',
                assume_role_arn='arn:aws:iam::123456789012:role/test-bouncer-role',
            )

            # Verify assume_role was called
            assert mock_sts.assume_role.called
            assert '❌' not in result

    @mock_aws
    def test_execute_native_empty_response(self):
        """Test handling of empty response"""
        from commands import execute_boto3_native

        with patch('boto3.client') as mock_client:
            mock_service = MagicMock()
            mock_client.return_value = mock_service
            mock_service.some_operation = MagicMock(return_value={})

            result = execute_boto3_native(
                service='dynamodb',
                operation='some_operation',
                params={},
                region='us-east-1',
            )

            assert '⚠️ 命令執行完成（無輸出，請確認結果）' in result


class TestMcpToolExecuteNative:
    """Test mcp_tool_execute_native MCP tool"""

    def test_missing_aws_section(self, mock_table, mock_notifications):
        """Test error when aws section is missing"""
        from mcp_execute import mcp_tool_execute_native

        result = mcp_tool_execute_native('req-123', {
            'bouncer': {
                'trust_scope': 'test-scope',
                'reason': 'test',
            }
        })

        assert 'error' in result
        assert 'Missing required parameter: aws' in json.dumps(result)

    def test_missing_service(self, mock_table, mock_notifications):
        """Test error when service is missing"""
        from mcp_execute import mcp_tool_execute_native

        result = mcp_tool_execute_native('req-123', {
            'aws': {
                'operation': 'list_buckets',
                'params': {},
            },
            'bouncer': {
                'trust_scope': 'test-scope',
            }
        })

        assert 'error' in result
        assert 'Missing required parameter: aws.service' in json.dumps(result)

    def test_missing_trust_scope(self, mock_table, mock_notifications):
        """Test error when trust_scope is missing"""
        from mcp_execute import mcp_tool_execute_native

        result = mcp_tool_execute_native('req-123', {
            'aws': {
                'service': 's3',
                'operation': 'list_buckets',
                'params': {},
            },
            'bouncer': {
                'reason': 'test',
            }
        })

        assert 'error' in result
        assert 'trust_scope' in json.dumps(result)

    @mock_aws
    def test_compliance_blocked(self, mock_table, mock_notifications):
        """Test that compliance blocking works for native calls"""
        from mcp_execute import mcp_tool_execute_native

        # Mock compliance checker to block the command
        with patch('mcp_execute._check_compliance') as mock_compliance:
            mock_compliance.return_value = {
                'id': 'req-123',
                'jsonrpc': '2.0',
                'result': {
                    'content': [{
                        'type': 'text',
                        'text': json.dumps({
                            'status': 'compliance_blocked',
                            'reason': 'Test block'
                        })
                    }]
                }
            }

            result = mcp_tool_execute_native('req-123', {
                'aws': {
                    'service': 'iam',
                    'operation': 'create_user',
                    'params': {'UserName': 'test-user'},
                },
                'bouncer': {
                    'trust_scope': 'test-scope',
                    'reason': 'test create user',
                }
            })

            assert mock_compliance.called
            assert 'compliance_blocked' in json.dumps(result)

    @mock_aws
    def test_auto_approve_execution(self, mock_table, mock_notifications):
        """Test auto-approve path for safe native operations"""
        from mcp_execute import mcp_tool_execute_native

        # Mock auto_approve to return True for describe operations
        with patch('mcp_execute.is_auto_approve') as mock_auto_approve, \
             patch('mcp_execute.store_paged_output') as mock_paging:
            mock_auto_approve.return_value = True
            mock_paging.return_value = False

            # Create a test bucket
            s3 = boto3.client('s3', region_name='us-east-1')
            s3.create_bucket(Bucket='test-bucket-auto')

            result = mcp_tool_execute_native('req-123', {
                'aws': {
                    'service': 's3',
                    'operation': 'list_buckets',
                    'params': {},
                    'region': 'us-east-1',
                },
                'bouncer': {
                    'trust_scope': 'test-scope',
                    'reason': 'List S3 buckets',
                    'source': 'test-bot',
                }
            })

            # Should execute successfully via auto-approve
            assert mock_auto_approve.called
            result_str = json.dumps(result)
            assert 'error' not in result_str.lower() or 'Buckets' in result_str

    def test_synthetic_command_format(self, mock_table, mock_notifications):
        """Test that synthetic command is correctly formatted for compliance"""
        from mcp_execute import mcp_tool_execute_native

        captured_ctx = []

        def capture_compliance(ctx):
            captured_ctx.append(ctx)
            # Return None to continue pipeline
            return None

        with patch('mcp_execute._check_compliance', side_effect=capture_compliance), \
             patch('mcp_execute._check_blocked') as mock_blocked:
            # Make blocked return a result to stop pipeline
            mock_blocked.return_value = {
                'id': 'req-123',
                'jsonrpc': '2.0',
                'result': {
                    'content': [{
                        'type': 'text',
                        'text': json.dumps({'status': 'blocked'})
                    }]
                }
            }

            result = mcp_tool_execute_native('req-123', {
                'aws': {
                    'service': 'eks',
                    'operation': 'create_cluster',
                    'params': {
                        'name': 'test-cluster',
                        'version': '1.32',
                    },
                    'region': 'us-east-1',
                },
                'bouncer': {
                    'trust_scope': 'test-scope',
                    'reason': 'Create EKS cluster',
                }
            })

            # Verify ExecuteContext was created with correct synthetic command
            assert len(captured_ctx) > 0
            ctx = captured_ctx[0]
            assert ctx.is_native is True
            assert ctx.native_service == 'eks'
            assert ctx.native_operation == 'create_cluster'
            assert ctx.command.startswith('aws eks create-cluster')
            assert 'test-cluster' in ctx.command

    @mock_aws
    def test_invalid_params_type(self, mock_table, mock_notifications):
        """Test error when params is not a dict"""
        from mcp_execute import mcp_tool_execute_native

        result = mcp_tool_execute_native('req-123', {
            'aws': {
                'service': 's3',
                'operation': 'list_buckets',
                'params': 'not-a-dict',
            },
            'bouncer': {
                'trust_scope': 'test-scope',
            }
        })

        assert 'error' in result
        assert 'must be a dict' in json.dumps(result)
