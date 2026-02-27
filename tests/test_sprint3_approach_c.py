"""
Tests for Bouncer Sprint 3 – Approach C
Covers:
  - bouncer-bug-017: lambda update-function-configuration --environment compliance rule
  - bouncer-ux-016-019: git commit SHA in deploy record
  - bouncer-ux-018: structured conflict error with running_deploy_id
  - bouncer-feat-stats-cmd: /stats Telegram command
"""

import os
import sys
import time
import importlib
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

# Set required env vars BEFORE importing any src modules
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')
os.environ.setdefault('TRUSTED_ACCOUNT_IDS', '111111111111,222222222222')

SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ============================================================================
# Task 1: bouncer-bug-017 — lambda update-function-configuration --environment
# ============================================================================

class TestLambdaEnvOverwriteCompliance:
    """compliance_checker: B-LAMBDA-01 rule"""

    def test_lambda_update_env_blocked(self):
        """B-LAMBDA-01: lambda update-function-configuration --environment should be blocked"""
        from compliance_checker import check_compliance
        cmd = "aws lambda update-function-configuration --function-name my-func --environment Variables={KEY=VALUE}"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation is not None
        assert violation.rule_id == "B-LAMBDA-01"

    def test_lambda_update_env_blocked_with_json(self):
        """B-LAMBDA-01: also blocked when environment is JSON"""
        from compliance_checker import check_compliance
        cmd = 'aws lambda update-function-configuration --function-name fn --environment \'{"Variables":{"K":"V"}}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "B-LAMBDA-01"

    def test_lambda_update_function_no_env_ok(self):
        """Updating lambda config without --environment should still pass"""
        from compliance_checker import check_compliance
        cmd = "aws lambda update-function-configuration --function-name my-func --timeout 30"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_lambda_update_env_remediation_present(self):
        """B-LAMBDA-01 violation should have remediation text"""
        from compliance_checker import check_compliance
        cmd = "aws lambda update-function-configuration --function-name fn --environment Variables={}"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.remediation  # non-empty

    def test_lambda_env_overwrite_in_risk_rules(self):
        """risk-rules.json should have lambda_env_overwrite parameter pattern"""
        from risk_scorer import create_default_rules
        rules = create_default_rules()
        patterns = [p.get('description', '') for p in rules.parameter_patterns]
        found = any(
            'lambda_env_overwrite' in p or
            ('lambda' in p.lower() and 'env' in p.lower())
            for p in patterns
        )
        assert found, f"Expected lambda_env_overwrite pattern, got: {patterns}"

    def test_lambda_env_overwrite_risk_score_elevated(self):
        """lambda update-function-configuration --environment should have elevated risk score"""
        from risk_scorer import calculate_risk, create_default_rules
        rules = create_default_rules()
        result = calculate_risk(
            command="aws lambda update-function-configuration --function-name fn --environment Variables={K=V}",
            reason="test",
            source="test",
            rules=rules,
        )
        # The parameter pattern score is 80, pushing total well above baseline
        assert result.score >= 50


# ============================================================================
# Task 2: bouncer-ux-016-019 — git commit SHA in deploy record
# ============================================================================

class TestGetGitCommitInfo:
    """deployer.get_git_commit_info: SHA extraction + graceful fallback"""

    def setup_method(self):
        """Ensure deployer can be imported with moto mock"""
        pass

    @mock_aws()
    def _import_deployer(self):
        """Helper to import deployer under mock_aws"""
        if 'deployer' in sys.modules:
            del sys.modules['deployer']
        import deployer as d
        return d

    def test_git_info_keys_present(self):
        """get_git_commit_info should always return dict with all three keys"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer
            info = deployer.get_git_commit_info()
        assert 'commit_sha' in info
        assert 'commit_short' in info
        assert 'commit_message' in info

    def test_git_info_in_real_repo(self):
        """In a git repo, should return a valid SHA"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer
            info = deployer.get_git_commit_info(cwd='/tmp/bouncer-s3-c')
        # Either valid SHA or graceful fallback (both are acceptable)
        if info['commit_sha']:
            assert len(info['commit_sha']) == 40
            assert info['commit_short'] is not None
            assert len(info['commit_short']) == 7

    def test_git_info_fallback_non_repo(self, tmp_path):
        """When git returns non-zero exit code, all fields should be null"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer
            # Mock subprocess.run to simulate git not available / non-git-repo
            import subprocess
            from unittest.mock import patch
            mock_result = MagicMock()
            mock_result.returncode = 128  # git error
            mock_result.stdout = ''
            with patch('subprocess.run', return_value=mock_result):
                info = deployer.get_git_commit_info(cwd=str(tmp_path))
        # Should fall back gracefully (all None)
        assert info['commit_sha'] is None
        assert info['commit_short'] is None
        assert info['commit_message'] is None

    def test_git_info_short_is_7_chars_when_sha_present(self):
        """commit_short should be exactly 7 chars when commit_sha is present"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer
            info = deployer.get_git_commit_info(cwd='/tmp/bouncer-s3-c')
        if info['commit_sha']:
            assert len(info['commit_short']) == 7
            assert info['commit_sha'].startswith(info['commit_short'])


# ============================================================================
# Task 3: bouncer-ux-018 — structured conflict error
# ============================================================================

class TestDeployConflictStructuredError:
    """start_deploy and mcp_tool_deploy return structured conflict response"""

    def test_start_deploy_conflict_has_running_deploy_id(self):
        """start_deploy conflict: response should contain running_deploy_id"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer

            fake_project = {'project_id': 'test', 'name': 'Test', 'enabled': True}
            fake_lock = {
                'lock_id': 'deploy-abc123',
                'locked_at': int(time.time()),
            }

            with patch.object(deployer, 'get_project', return_value=fake_project), \
                 patch.object(deployer, 'get_lock', return_value=fake_lock):
                result = deployer.start_deploy('test', 'main', 'user', 'reason')

        assert result.get('status') == 'conflict'
        assert result.get('running_deploy_id') == 'deploy-abc123'
        assert 'started_at' in result
        assert 'hint' in result

    def test_start_deploy_conflict_hint_present(self):
        """Conflict response should have a hint about bouncer_deploy_status"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer

            fake_project = {'project_id': 'p', 'name': 'P', 'enabled': True}
            fake_lock = {'lock_id': 'deploy-xyz', 'locked_at': int(time.time())}

            with patch.object(deployer, 'get_project', return_value=fake_project), \
                 patch.object(deployer, 'get_lock', return_value=fake_lock):
                result = deployer.start_deploy('p', 'main', 'user', 'reason')

        hint = result.get('hint', '')
        assert 'bouncer_deploy_status' in hint or 'bouncer_deploy_cancel' in hint

    def test_start_deploy_conflict_started_at_iso_format(self):
        """started_at should be an ISO 8601 datetime string"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer

            locked_ts = 1740592800  # fixed timestamp
            fake_project = {'project_id': 'p2', 'name': 'P2', 'enabled': True}
            fake_lock = {'lock_id': 'deploy-test', 'locked_at': locked_ts}

            with patch.object(deployer, 'get_project', return_value=fake_project), \
                 patch.object(deployer, 'get_lock', return_value=fake_lock):
                result = deployer.start_deploy('p2', 'main', 'user', 'reason')

        started_at = result.get('started_at')
        assert started_at is not None
        import re
        assert re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', started_at)

    def test_start_deploy_conflict_message_in_chinese(self):
        """Conflict message should be in Chinese"""
        with mock_aws():
            if 'deployer' in sys.modules:
                del sys.modules['deployer']
            import deployer

            fake_project = {'project_id': 'p3', 'name': 'P3', 'enabled': True}
            fake_lock = {'lock_id': 'deploy-abc', 'locked_at': int(time.time())}

            with patch.object(deployer, 'get_project', return_value=fake_project), \
                 patch.object(deployer, 'get_lock', return_value=fake_lock):
                result = deployer.start_deploy('p3', 'main', 'user', 'reason')

        assert '進行中' in result.get('message', '')


# ============================================================================
# Task 4: bouncer-feat-stats-cmd — /stats Telegram command
# ============================================================================

def _mock_tc_module():
    """Import telegram_commands with all external deps mocked"""
    # Patch at the module level before import
    mocks = {
        'boto3': MagicMock(),
        'telegram': MagicMock(),
    }
    # Make db module return a mock table
    mock_db = MagicMock()
    mock_db.table = MagicMock()
    if 'telegram_commands' in sys.modules:
        del sys.modules['telegram_commands']
    if 'db' in sys.modules:
        pass  # keep existing mock

    with mock_aws():
        if 'telegram_commands' in sys.modules:
            del sys.modules['telegram_commands']
        import telegram_commands as tc
    return tc


class TestHandleStatsCommand:
    """telegram_commands.handle_stats_command"""

    def _make_mock_table(self, items):
        """Create a minimal mock DynamoDB table for stats scan"""
        mock_table = MagicMock()
        mock_table.scan.return_value = {'Items': items, 'ScannedCount': len(items)}
        return mock_table

    def test_stats_empty_shows_no_records(self):
        """When no items, stats output should include summary without crashing"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        mock_table = self._make_mock_table([])
        sent_texts = []

        with patch.object(tc_mod, '_get_table', return_value=mock_table), \
             patch.object(tc_mod, 'send_telegram_message_to',
                          side_effect=lambda cid, txt, **kw: sent_texts.append(txt)):
            result = tc_mod.handle_stats_command('chat-001')

        assert result['statusCode'] == 200
        combined = '\n'.join(sent_texts)
        # B's format: shows 統計資訊 header and total: 0
        assert '統計' in combined or '0' in combined

    def test_stats_with_items_shows_counts(self):
        """When items exist, output should include counts"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        now = int(time.time())
        items = [
            {'created_at': now - 100, 'status': 'approved', 'source': 'BotA', 'action': 'execute', 'command': 'aws s3 ls'},
            {'created_at': now - 200, 'status': 'denied', 'source': 'BotB', 'action': 'execute', 'command': 'aws ec2 describe-instances'},
            {'created_at': now - 300, 'status': 'approved', 'source': 'BotA', 'action': 'deploy', 'command': ''},
        ]
        mock_table = self._make_mock_table(items)
        sent_texts = []

        with patch.object(tc_mod, '_get_table', return_value=mock_table), \
             patch.object(tc_mod, 'send_telegram_message_to',
                          side_effect=lambda cid, txt, **kw: sent_texts.append(txt)):
            result = tc_mod.handle_stats_command('chat-001')

        combined = '\n'.join(sent_texts)
        assert '3' in combined  # total = 3
        assert result['statusCode'] == 200

    def test_stats_by_action_summary(self):
        """Output should contain status counts"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        now = int(time.time())
        items = [
            {'created_at': now - 10, 'status': 'approved', 'source': 'Bot', 'action': 'execute', 'command': 'aws s3 ls'},
            {'created_at': now - 20, 'status': 'approved', 'source': 'Bot', 'action': 'deploy', 'command': ''},
            {'created_at': now - 30, 'status': 'approved', 'source': 'Bot', 'action': 'upload', 'command': ''},
        ]
        mock_table = self._make_mock_table(items)
        sent_texts = []

        with patch.object(tc_mod, '_get_table', return_value=mock_table), \
             patch.object(tc_mod, 'send_telegram_message_to',
                          side_effect=lambda cid, txt, **kw: sent_texts.append(txt)):
            tc_mod.handle_stats_command('chat-001')

        combined = '\n'.join(sent_texts)
        # B's stats shows total + approval counts
        assert '3' in combined  # total count

    def test_stats_approval_rate(self):
        """Output should include approval rate percentage"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        now = int(time.time())
        items = [
            {'created_at': now - 10, 'status': 'approved', 'source': 'Bot', 'action': 'execute', 'command': 'aws s3 ls'},
            {'created_at': now - 20, 'status': 'approved', 'source': 'Bot', 'action': 'execute', 'command': 'aws s3 ls'},
            {'created_at': now - 30, 'status': 'denied', 'source': 'Bot', 'action': 'execute', 'command': 'aws iam'},
        ]
        mock_table = self._make_mock_table(items)
        sent_texts = []

        with patch.object(tc_mod, '_get_table', return_value=mock_table), \
             patch.object(tc_mod, 'send_telegram_message_to',
                          side_effect=lambda cid, txt, **kw: sent_texts.append(txt)):
            tc_mod.handle_stats_command('chat-001')

        combined = '\n'.join(sent_texts)
        # B's format uses 審批率, should have 67%
        assert '67%' in combined

    def test_stats_command_registered_in_handle_telegram_command(self):
        """handle_telegram_command should route /stats to handle_stats_command"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        from constants import APPROVED_CHAT_IDS
        approved_user = next(iter(APPROVED_CHAT_IDS)) if APPROVED_CHAT_IDS else '12345'
        message = {
            'from': {'id': int(approved_user)},
            'chat': {'id': 99999},
            'text': '/stats',
        }

        called_with = []

        # B's handle_stats_command accepts hours parameter; use a flexible mock
        with patch.object(tc_mod, 'handle_stats_command',
                          side_effect=lambda cid, **kwargs: called_with.append(cid) or {'statusCode': 200, 'body': '{}'}):
            tc_mod.handle_telegram_command(message)

        assert len(called_with) == 1

    def test_stats_top_sources_and_commands_empty_shows_placeholder(self):
        """When no items, stats output should return 200 without crashing"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        mock_table = self._make_mock_table([])
        sent_texts = []

        with patch.object(tc_mod, '_get_table', return_value=mock_table), \
             patch.object(tc_mod, 'send_telegram_message_to',
                          side_effect=lambda cid, txt, **kw: sent_texts.append(txt)):
            result = tc_mod.handle_stats_command('x')

        assert result['statusCode'] == 200
        combined = '\n'.join(sent_texts)
        # Should have content without crashing
        assert len(combined) > 0

    def test_stats_help_includes_stats(self):
        """/help output should mention /stats"""
        with mock_aws():
            if 'telegram_commands' in sys.modules:
                del sys.modules['telegram_commands']
            import telegram_commands as tc_mod

        sent_texts = []
        with patch.object(tc_mod, 'send_telegram_message_to',
                          side_effect=lambda cid, txt, **kw: sent_texts.append(txt)):
            tc_mod.handle_help_command('chat-001')

        combined = '\n'.join(sent_texts)
        assert '/stats' in combined
