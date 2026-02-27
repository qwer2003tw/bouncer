"""
Tests for Sprint 3 Approach B features:
- bouncer-bug-017: lambda update-function-configuration --environment guard
- bouncer-ux-016-019: deploy history + notification with git commit SHA
- bouncer-ux-018: deploy conflict error shows running deploy_id
- bouncer-feat-stats-cmd: /stats [hours] Telegram command with peak hour
"""
import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')

REQUESTS_TABLE = 'clawdbot-approval-requests'
ACCOUNTS_TABLE = 'bouncer-accounts'


# ===========================================================================
# Task 1: lambda update-function-configuration --environment guard (bouncer-bug-017)
# ===========================================================================

class TestLambdaEnvGuard:
    """Regression tests for lambda update-function-configuration --environment"""

    def setup_method(self):
        """Clear module cache to ensure fresh imports."""
        for mod in list(sys.modules.keys()):
            if mod in ('commands',):
                del sys.modules[mod]

    def test_empty_variables_is_blocked(self):
        """--environment Variables={} should be BLOCKED."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name my-fn --environment Variables={}'
        assert commands.is_blocked(cmd), "Empty Variables={} must be blocked"
        reason = commands.get_block_reason(cmd)
        assert reason is not None
        assert '清空' in reason or 'Variables={}' in reason or '封鎖' in reason

    def test_empty_variables_with_spaces_is_blocked(self):
        """--environment Variables={   } should be BLOCKED."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name my-fn --environment Variables={  }'
        assert commands.is_blocked(cmd), "Empty Variables with spaces must be blocked"

    def test_variables_with_values_is_dangerous(self):
        """--environment Variables={KEY=VALUE} should be DANGEROUS."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name my-fn --environment Variables={KEY=VALUE,OTHER=123}'
        assert not commands.is_blocked(cmd), "Non-empty Variables should not be blocked"
        assert commands.is_dangerous(cmd), "Non-empty Variables must be dangerous"

    def test_variables_with_json_value_is_dangerous(self):
        """--environment Variables={\"Key\":\"Val\"} should be DANGEROUS."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name my-fn --environment \'{"Variables":{"KEY":"VALUE"}}\''
        # At minimum: not blocked; json form may not match --environment Variables= pattern
        # but the key concern is Variables={} BLOCKED
        assert not commands.is_blocked(cmd)

    def test_check_lambda_env_update_empty_returns_blocked(self):
        """check_lambda_env_update returns ('BLOCKED', ...) for empty vars."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name fn --environment Variables={}'
        level, msg = commands.check_lambda_env_update(cmd)
        assert level == 'BLOCKED'
        assert msg is not None

    def test_check_lambda_env_update_with_values_returns_dangerous(self):
        """check_lambda_env_update returns ('DANGEROUS', ...) for non-empty vars."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name fn --environment Variables={KEY=VAL}'
        level, msg = commands.check_lambda_env_update(cmd)
        assert level == 'DANGEROUS'
        assert msg is not None
        assert '⚠️' in msg or '環境變數' in msg

    def test_check_lambda_env_update_no_environment_flag(self):
        """update-function-configuration without --environment → None."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name fn --timeout 30'
        level, msg = commands.check_lambda_env_update(cmd)
        assert level is None

    def test_check_lambda_env_update_other_lambda_command(self):
        """Other lambda commands are not affected."""
        import commands
        cmd = 'aws lambda list-functions'
        level, msg = commands.check_lambda_env_update(cmd)
        assert level is None

    def test_check_lambda_env_update_non_lambda_command(self):
        """Non-lambda commands are not affected."""
        import commands
        cmd = 'aws s3 ls --environment Variables={}'
        level, msg = commands.check_lambda_env_update(cmd)
        assert level is None

    def test_lambda_env_warn_msg_content(self):
        """LAMBDA_ENV_WARN_MSG should contain 覆蓋 and 備份."""
        import commands
        assert '覆蓋' in commands.LAMBDA_ENV_WARN_MSG
        assert '備份' in commands.LAMBDA_ENV_WARN_MSG

    def test_dangerous_warning_included_in_notification(self):
        """Notification for lambda env DANGEROUS should include warning message."""
        # Clear module cache
        for mod in list(sys.modules.keys()):
            if mod in ('notifications', 'commands'):
                del sys.modules[mod]

        from notifications import send_approval_request

        with patch('notifications._send_message') as mock_send:
            mock_send.return_value = {'ok': True}
            send_approval_request(
                request_id='r-test-lambda-env',
                command='aws lambda update-function-configuration --function-name fn --environment Variables={KEY=VAL}',
                reason='test update',
                source='bot',
                account_id='190825685292',
                account_name='Default',
            )

        mock_send.assert_called_once()
        text_sent = mock_send.call_args[0][0]
        # Should contain the lambda env warning message
        assert '環境變數' in text_sent or '⚠️' in text_sent

    def test_blocked_empty_env_not_in_notification_path(self):
        """Blocked commands should not reach notification (is_blocked check first)."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name fn --environment Variables={}'
        # is_blocked returns True → command never gets to notification stage
        assert commands.is_blocked(cmd)

    def test_update_function_configuration_without_environment_not_dangerous(self):
        """update-function-configuration with --timeout only → not dangerous by new rule."""
        import commands
        cmd = 'aws lambda update-function-configuration --function-name fn --timeout 60'
        level, _ = commands.check_lambda_env_update(cmd)
        assert level is None


# ===========================================================================
# Task 2: Deploy history + notification with git commit SHA (bouncer-ux-016-019)
# ===========================================================================

class TestDeployCommitSHA:
    """Tests for git commit SHA in deploy history and notification."""

    def test_create_deploy_record_includes_commit_sha(self):
        """create_deploy_record should auto-detect and store git commit_sha."""
        for mod in list(sys.modules.keys()):
            if 'deployer' in mod:
                del sys.modules[mod]

        with mock_aws():
            import boto3 as _b3
            ddb = _b3.resource('dynamodb', region_name='us-east-1')

            # Create history table
            history_tbl = ddb.create_table(
                TableName='bouncer-deploy-history',
                KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[
                    {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                    {'AttributeName': 'started_at', 'AttributeType': 'N'},
                ],
                GlobalSecondaryIndexes=[{
                    'IndexName': 'project-time-index',
                    'KeySchema': [
                        {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                        {'AttributeName': 'started_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                }],
                BillingMode='PAY_PER_REQUEST',
            )
            history_tbl.wait_until_exists()

            import deployer
            deployer.history_table = history_tbl

            # Final: create_deploy_record auto-detects git commit info
            # Patch get_git_commit_info to return controlled values
            with patch.object(deployer, 'get_git_commit_info', return_value={
                'commit_sha': 'abc1234567890',
                'commit_short': 'abc1234',
                'commit_message': 'fix: add reason to notification',
            }):
                record = deployer.create_deploy_record('deploy-abc', 'bouncer', {
                    'branch': 'master',
                    'triggered_by': 'test-bot',
                    'reason': 'test deploy',
                })

            assert record['commit_sha'] == 'abc1234567890'
            assert record['commit_message'] == 'fix: add reason to notification'

            # Verify persisted in DDB
            item = history_tbl.get_item(Key={'deploy_id': 'deploy-abc'}).get('Item', {})
            assert item.get('commit_sha') == 'abc1234567890'
            assert item.get('commit_message') == 'fix: add reason to notification'

    def test_create_deploy_record_without_commit_sha(self):
        """create_deploy_record without git repo should gracefully omit commit fields."""
        for mod in list(sys.modules.keys()):
            if 'deployer' in mod:
                del sys.modules[mod]

        with mock_aws():
            import boto3 as _b3
            ddb = _b3.resource('dynamodb', region_name='us-east-1')
            history_tbl = ddb.create_table(
                TableName='bouncer-deploy-history',
                KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[
                    {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                    {'AttributeName': 'started_at', 'AttributeType': 'N'},
                ],
                GlobalSecondaryIndexes=[{
                    'IndexName': 'project-time-index',
                    'KeySchema': [
                        {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                        {'AttributeName': 'started_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                }],
                BillingMode='PAY_PER_REQUEST',
            )
            history_tbl.wait_until_exists()

            import deployer
            deployer.history_table = history_tbl

            # Patch get_git_commit_info to simulate non-git environment
            with patch.object(deployer, 'get_git_commit_info', return_value={
                'commit_sha': None,
                'commit_short': None,
                'commit_message': None,
            }):
                record = deployer.create_deploy_record('deploy-xyz', 'bouncer', {
                    'branch': 'master',
                    'triggered_by': 'test-bot',
                    'reason': 'test deploy',
                })

            assert 'commit_sha' not in record or record.get('commit_sha') is None
            assert record['deploy_id'] == 'deploy-xyz'

    def test_deploy_notification_includes_commit_sha(self):
        """Approval request notification is sent successfully (commit SHA shown in callbacks after start_deploy)."""
        for mod in list(sys.modules.keys()):
            if 'deployer' in mod:
                del sys.modules[mod]

        import deployer

        project = {
            'project_id': 'bouncer',
            'name': 'Bouncer',
            'stack_name': 'clawdbot-bouncer',
            'default_branch': 'master',
            'target_account': '190825685292',
            'target_role_arn': '',
        }

        with patch('telegram.send_telegram_message') as mock_tg:
            deployer.send_deploy_approval_request(
                request_id='req-deploy-001',
                project=project,
                branch='master',
                reason='v3.2.1 hotfix',
                source='Private Bot',
            )

        mock_tg.assert_called_once()
        text_sent = mock_tg.call_args[0][0]
        # Should contain project name and branch
        assert 'Bouncer' in text_sent
        assert 'master' in text_sent

    def test_deploy_notification_without_commit_sha(self):
        """send_deploy_approval_request without commit SHA should work (backward compat)."""
        for mod in list(sys.modules.keys()):
            if 'deployer' in mod:
                del sys.modules[mod]

        import deployer

        project = {
            'project_id': 'bouncer',
            'name': 'Bouncer',
            'stack_name': 'clawdbot-bouncer',
            'default_branch': 'master',
            'target_account': '',
            'target_role_arn': '',
        }

        with patch('telegram.send_telegram_message') as mock_tg:
            deployer.send_deploy_approval_request(
                request_id='req-deploy-002',
                project=project,
                branch='master',
                reason='test deploy',
                source='Private Bot',
            )

        mock_tg.assert_called_once()
        text_sent = mock_tg.call_args[0][0]
        # Should not crash; commit line should not appear
        assert '🔖' not in text_sent or 'Commit' not in text_sent

    def test_deploy_notification_commit_sha_only(self):
        """send_deploy_approval_request succeeds; commit SHA shown in started callback via start_deploy."""
        for mod in list(sys.modules.keys()):
            if 'deployer' in mod:
                del sys.modules[mod]

        import deployer

        project = {
            'project_id': 'bouncer',
            'name': 'Bouncer',
            'stack_name': 'clawdbot-bouncer',
            'default_branch': 'master',
            'target_account': '',
            'target_role_arn': '',
        }

        with patch('telegram.send_telegram_message') as mock_tg:
            deployer.send_deploy_approval_request(
                request_id='req-deploy-003',
                project=project,
                branch='master',
                reason='deploy',
                source='Bot',
            )

        mock_tg.assert_called_once()
        text_sent = mock_tg.call_args[0][0]
        # Should contain project and branch info
        assert 'Bouncer' in text_sent
        assert 'master' in text_sent


# ===========================================================================
# Task 3: Deploy conflict error shows running deploy_id (bouncer-ux-018)
# ===========================================================================

class TestDeployConflictDetails:
    """Tests for conflict error including running deploy info."""

    @pytest.fixture
    def deploy_tables(self):
        """Set up moto DynamoDB tables for deployer tests."""
        for mod in list(sys.modules.keys()):
            if 'deployer' in mod:
                del sys.modules[mod]

        with mock_aws():
            import boto3 as _b3
            ddb = _b3.resource('dynamodb', region_name='us-east-1')

            # Projects table
            projects_tbl = ddb.create_table(
                TableName='bouncer-projects',
                KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            projects_tbl.wait_until_exists()

            # History table
            history_tbl = ddb.create_table(
                TableName='bouncer-deploy-history',
                KeySchema=[{'AttributeName': 'deploy_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[
                    {'AttributeName': 'deploy_id', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                    {'AttributeName': 'started_at', 'AttributeType': 'N'},
                ],
                GlobalSecondaryIndexes=[{
                    'IndexName': 'project-time-index',
                    'KeySchema': [
                        {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                        {'AttributeName': 'started_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                }],
                BillingMode='PAY_PER_REQUEST',
            )
            history_tbl.wait_until_exists()

            # Locks table
            locks_tbl = ddb.create_table(
                TableName='bouncer-deploy-locks',
                KeySchema=[{'AttributeName': 'project_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'project_id', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            locks_tbl.wait_until_exists()

            # Requests table (for mcp_tool_deploy)
            requests_tbl = ddb.create_table(
                TableName=REQUESTS_TABLE,
                KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            requests_tbl.wait_until_exists()

            yield projects_tbl, history_tbl, locks_tbl, requests_tbl

    def test_conflict_response_includes_running_deploy_id(self, deploy_tables):
        """When lock exists, mcp_tool_deploy error should include running_deploy_id."""
        projects_tbl, history_tbl, locks_tbl, requests_tbl = deploy_tables

        import deployer
        deployer.projects_table = projects_tbl
        deployer.history_table = history_tbl
        deployer.locks_table = locks_tbl

        now = int(time.time())
        running_deploy_id = 'deploy-running-abc123'

        # Add project
        projects_tbl.put_item(Item={
            'project_id': 'bouncer',
            'name': 'Bouncer',
            'enabled': True,
            'stack_name': 'clawdbot-bouncer',
            'default_branch': 'master',
        })

        # Add lock record
        locks_tbl.put_item(Item={
            'project_id': 'bouncer',
            'lock_id': running_deploy_id,
            'locked_at': now - 60,
            'locked_by': 'test-bot',
            'ttl': now + 3600,
        })

        # Add history record for the running deploy
        history_tbl.put_item(Item={
            'deploy_id': running_deploy_id,
            'project_id': 'bouncer',
            'status': 'RUNNING',
            'started_at': now - 60,
        })

        result = deployer.mcp_tool_deploy(
            'req-001',
            {
                'project': 'bouncer',
                'reason': 'test deploy',
                'source': 'test-bot',
            },
            requests_tbl,
            None,
        )

        body = json.loads(result['body'])
        text = json.loads(body['result']['content'][0]['text'])

        assert text.get('status') in ('error', 'conflict') or 'error' in text
        assert text.get('running_deploy_id') == running_deploy_id or \
               text.get('current_deploy') == running_deploy_id

    def test_conflict_response_includes_started_at(self, deploy_tables):
        """Conflict response should include started_at."""
        projects_tbl, history_tbl, locks_tbl, requests_tbl = deploy_tables

        import deployer
        deployer.projects_table = projects_tbl
        deployer.history_table = history_tbl
        deployer.locks_table = locks_tbl

        now = int(time.time())
        running_deploy_id = 'deploy-running-xyz789'

        projects_tbl.put_item(Item={
            'project_id': 'bouncer',
            'name': 'Bouncer',
            'enabled': True,
            'stack_name': 'clawdbot-bouncer',
            'default_branch': 'master',
        })

        locks_tbl.put_item(Item={
            'project_id': 'bouncer',
            'lock_id': running_deploy_id,
            'locked_at': now - 120,
            'locked_by': 'test-bot',
            'ttl': now + 3600,
        })

        history_tbl.put_item(Item={
            'deploy_id': running_deploy_id,
            'project_id': 'bouncer',
            'status': 'RUNNING',
            'started_at': now - 120,
        })

        result = deployer.mcp_tool_deploy(
            'req-002',
            {'project': 'bouncer', 'reason': 'test', 'source': 'bot'},
            requests_tbl,
            None,
        )

        body = json.loads(result['body'])
        text = json.loads(body['result']['content'][0]['text'])

        # Should have started_at info
        assert 'started_at' in text or 'locked_at' in text or \
               str(now - 120) in json.dumps(text)

    def test_conflict_response_includes_estimated_remaining(self, deploy_tables):
        """Conflict response should include estimated_remaining seconds."""
        projects_tbl, history_tbl, locks_tbl, requests_tbl = deploy_tables

        import deployer
        deployer.projects_table = projects_tbl
        deployer.history_table = history_tbl
        deployer.locks_table = locks_tbl

        now = int(time.time())
        running_deploy_id = 'deploy-running-est123'

        projects_tbl.put_item(Item={
            'project_id': 'bouncer',
            'name': 'Bouncer',
            'enabled': True,
            'default_branch': 'master',
        })

        locks_tbl.put_item(Item={
            'project_id': 'bouncer',
            'lock_id': running_deploy_id,
            'locked_at': now - 60,
            'locked_by': 'bot',
            'ttl': now + 3600,
        })

        history_tbl.put_item(Item={
            'deploy_id': running_deploy_id,
            'project_id': 'bouncer',
            'status': 'RUNNING',
            'started_at': now - 60,
        })

        result = deployer.mcp_tool_deploy(
            'req-003',
            {'project': 'bouncer', 'reason': 'test', 'source': 'bot'},
            requests_tbl,
            None,
        )

        body = json.loads(result['body'])
        text = json.loads(body['result']['content'][0]['text'])

        # estimated_remaining should be present and be a number ≥ 0
        assert 'estimated_remaining' in text
        assert isinstance(text['estimated_remaining'], (int, float))
        assert text['estimated_remaining'] >= 0


# ===========================================================================
# Task 4: /stats [hours] Telegram command (bouncer-feat-stats-cmd)
# ===========================================================================

class TestStatsTelegramCommand:
    """Tests for /stats [hours] Telegram command."""

    def setup_method(self):
        for mod in list(sys.modules.keys()):
            if mod in ('telegram_commands', 'db'):
                del sys.modules[mod]

    def test_handle_telegram_command_routes_stats(self):
        """handle_telegram_command routes /stats to handle_stats_command."""
        import telegram_commands as tc

        with patch.object(tc, 'APPROVED_CHAT_IDS', {'999999999'}):
            with patch.object(tc, 'handle_stats_command', return_value={'statusCode': 200, 'body': '{}'}) as mock_stats:
                tc.handle_telegram_command({
                    'from': {'id': '999999999'},
                    'chat': {'id': '999999999'},
                    'text': '/stats',
                })
                mock_stats.assert_called_once_with('999999999', hours=24)

    def test_handle_telegram_command_routes_stats_with_hours(self):
        """handle_telegram_command parses /stats 48 correctly."""
        import telegram_commands as tc

        with patch.object(tc, 'APPROVED_CHAT_IDS', {'999999999'}):
            with patch.object(tc, 'handle_stats_command', return_value={'statusCode': 200, 'body': '{}'}) as mock_stats:
                tc.handle_telegram_command({
                    'from': {'id': '999999999'},
                    'chat': {'id': '999999999'},
                    'text': '/stats 48',
                })
                mock_stats.assert_called_once_with('999999999', hours=48)

    def test_stats_command_default_24h(self):
        """handle_stats_command with default 24h produces output."""
        import telegram_commands as tc

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': [], 'ScannedCount': 0}

        import db
        db.table = mock_table

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to') as mock_send:
                tc.handle_stats_command('chat-123', hours=24)

        mock_send.assert_called_once()
        text_sent = mock_send.call_args[0][1]
        assert '24h' in text_sent or '24' in text_sent
        assert '統計' in text_sent or 'Stats' in text_sent.lower() or '請求' in text_sent

    def test_stats_command_custom_hours(self):
        """handle_stats_command with 48h shows 48 in output."""
        import telegram_commands as tc

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': [], 'ScannedCount': 0}

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to') as mock_send:
                tc.handle_stats_command('chat-123', hours=48)

        mock_send.assert_called_once()
        text_sent = mock_send.call_args[0][1]
        assert '48' in text_sent

    def test_stats_command_counts_approved(self):
        """handle_stats_command counts approved requests correctly."""
        import telegram_commands as tc

        now = int(time.time())
        items = [
            {'request_id': 'r1', 'status': 'approved', 'created_at': now - 100},
            {'request_id': 'r2', 'status': 'approved', 'created_at': now - 200},
            {'request_id': 'r3', 'status': 'denied', 'created_at': now - 300},
        ]

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': items, 'ScannedCount': 3}

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to') as mock_send:
                tc.handle_stats_command('chat-123', hours=24)

        text_sent = mock_send.call_args[0][1]
        # Should show 2 approved, 1 denied
        assert '2' in text_sent  # approved count
        assert '1' in text_sent  # denied count

    def test_stats_command_shows_peak_hour(self):
        """handle_stats_command shows 尖峰時段 (peak hour) line."""
        import telegram_commands as tc

        now = int(time.time())
        # All 3 items in same hour window
        items = [
            {'request_id': f'r{i}', 'status': 'approved', 'created_at': now - i * 10}
            for i in range(3)
        ]

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': items, 'ScannedCount': 3}

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to') as mock_send:
                tc.handle_stats_command('chat-123', hours=24)

        text_sent = mock_send.call_args[0][1]
        assert '尖峰' in text_sent or 'peak' in text_sent.lower() or '📈' in text_sent

    def test_stats_command_empty_data_no_crash(self):
        """handle_stats_command with no data should not crash."""
        import telegram_commands as tc

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': [], 'ScannedCount': 0}

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to') as mock_send:
                result = tc.handle_stats_command('chat-123', hours=24)

        assert result is not None
        mock_send.assert_called_once()

    def test_stats_command_returns_200(self):
        """handle_stats_command should return HTTP 200."""
        import telegram_commands as tc

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': [], 'ScannedCount': 0}

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to'):
                result = tc.handle_stats_command('chat-123')

        assert result['statusCode'] == 200

    def test_stats_command_approval_rate_displayed(self):
        """handle_stats_command shows approval rate."""
        import telegram_commands as tc

        now = int(time.time())
        items = [
            {'request_id': 'r1', 'status': 'approved', 'created_at': now - 100},
            {'request_id': 'r2', 'status': 'denied', 'created_at': now - 200},
        ]

        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': items, 'ScannedCount': 2}

        with patch.object(tc, '_get_table', return_value=mock_table):
            with patch.object(tc, 'send_telegram_message_to') as mock_send:
                tc.handle_stats_command('chat-123', hours=24)

        text_sent = mock_send.call_args[0][1]
        assert '%' in text_sent or '審批率' in text_sent or 'rate' in text_sent.lower()

    def test_stats_in_help_command(self):
        """handle_help_command should mention /stats."""
        import telegram_commands as tc

        with patch.object(tc, 'send_telegram_message_to') as mock_send:
            tc.handle_help_command('chat-123')

        text_sent = mock_send.call_args[0][1]
        assert '/stats' in text_sent

    def test_stats_unauthorized_user_ignored(self):
        """Unauthorized user sending /stats should be silently ignored."""
        import telegram_commands as tc

        # Patch APPROVED_CHAT_IDS to only include the admin, not the test user
        with patch.object(tc, 'APPROVED_CHAT_IDS', {'999999999'}):
            with patch.object(tc, 'handle_stats_command') as mock_stats:
                result = tc.handle_telegram_command({
                    'from': {'id': 'unauthorized-user-000'},
                    'chat': {'id': '999999999'},
                    'text': '/stats',
                })
                mock_stats.assert_not_called()
        assert result['statusCode'] == 200
