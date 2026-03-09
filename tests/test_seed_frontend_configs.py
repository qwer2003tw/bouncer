"""
Regression tests for scripts/seed_frontend_configs.py (#89)

Verifies that seed() uses update_item semantics:
- Existing fields (stack_name, default_branch, git_repo) are preserved after seed
- Frontend config fields are written/updated correctly
- dry_run=True does NOT write to DDB
"""
import os
import sys
import pytest

# Ensure scripts/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('PROJECTS_TABLE', 'bouncer-projects')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('AWS_SESSION_TOKEN', 'test')

from moto import mock_aws
import boto3


@pytest.fixture
def ddb_table():
    """Create a mock DynamoDB bouncer-projects table with pre-existing ztp-files record."""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='bouncer-projects',
            KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
        # Pre-populate with existing fields that seed() should NOT overwrite
        table.put_item(Item={
            'project_id': 'ztp-files',
            'stack_name': 'ztp-files-dev',
            'default_branch': 'main',
            'git_repo': 'qwer2003tw/ztp-files',
            'deploy_role_arn': 'arn:aws:iam::190825685292:role/some-existing-role',
        })
        yield table


class TestSeedFrontendConfigs:
    """Regression tests: seed() must use update_item (not put_item)."""

    def test_seed_preserves_existing_fields(self, ddb_table):
        """
        Regression test for #89: seed() must NOT overwrite existing project fields.
        After seed(), stack_name, default_branch, and git_repo must still exist.
        """
        import seed_frontend_configs as seed_mod

        original_region = seed_mod.REGION
        original_table = seed_mod.TABLE_NAME
        try:
            seed_mod.REGION = 'us-east-1'
            seed_mod.TABLE_NAME = 'bouncer-projects'
            seed_mod.seed(dry_run=False)
        finally:
            seed_mod.REGION = original_region
            seed_mod.TABLE_NAME = original_table

        item = ddb_table.get_item(Key={'project_id': 'ztp-files'})['Item']

        # Existing fields must be preserved (update_item behavior)
        assert item.get('stack_name') == 'ztp-files-dev', (
            "stack_name was overwritten -- seed() used put_item instead of update_item"
        )
        assert item.get('default_branch') == 'main', (
            "default_branch was overwritten -- seed() used put_item instead of update_item"
        )
        assert item.get('git_repo') == 'qwer2003tw/ztp-files', (
            "git_repo was overwritten -- seed() used put_item instead of update_item"
        )

        # Frontend config fields must be present
        assert 'frontend_bucket' in item
        assert 'frontend_distribution_id' in item
        assert 'frontend_region' in item
        assert 'frontend_deploy_role_arn' in item

    def test_seed_writes_correct_frontend_values(self, ddb_table):
        """Frontend fields written by seed() must match FRONTEND_CONFIGS."""
        import seed_frontend_configs as seed_mod

        original_region = seed_mod.REGION
        original_table = seed_mod.TABLE_NAME
        try:
            seed_mod.REGION = 'us-east-1'
            seed_mod.TABLE_NAME = 'bouncer-projects'
            seed_mod.seed(dry_run=False)
        finally:
            seed_mod.REGION = original_region
            seed_mod.TABLE_NAME = original_table

        item = ddb_table.get_item(Key={'project_id': 'ztp-files'})['Item']
        expected = seed_mod.FRONTEND_CONFIGS['ztp-files']

        assert item['frontend_bucket'] == expected['frontend_bucket']
        assert item['frontend_distribution_id'] == expected['frontend_distribution_id']
        assert item['frontend_region'] == expected['frontend_region']
        assert item['frontend_deploy_role_arn'] == expected['frontend_deploy_role_arn']

    def test_seed_dry_run_does_not_write(self, ddb_table):
        """dry_run=True must not write frontend fields to DDB."""
        import seed_frontend_configs as seed_mod

        original_region = seed_mod.REGION
        original_table = seed_mod.TABLE_NAME
        try:
            seed_mod.REGION = 'us-east-1'
            seed_mod.TABLE_NAME = 'bouncer-projects'
            seed_mod.seed(dry_run=True)
        finally:
            seed_mod.REGION = original_region
            seed_mod.TABLE_NAME = original_table

        item = ddb_table.get_item(Key={'project_id': 'ztp-files'})['Item']

        # dry_run must NOT have written frontend_bucket
        assert 'frontend_bucket' not in item, (
            "dry_run=True should not write to DDB"
        )
        # Original fields must still be intact
        assert item.get('stack_name') == 'ztp-files-dev'
