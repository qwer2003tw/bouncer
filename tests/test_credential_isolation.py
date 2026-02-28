"""
Tests for bouncer-sec-006: credential isolation in execute_command.

Lock-based approach: _execute_lock ensures os.environ credential swap
is atomic under Lambda warm-start concurrency.
"""
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, 'src')
import commands as commands_mod


def _make_fake_sts_response(access_key, secret_key, token):
    return {
        'Credentials': {
            'AccessKeyId': access_key,
            'SecretAccessKey': secret_key,
            'SessionToken': token,
        }
    }


def _fake_driver_result(output='{"UserId":"AIDA..."}', rc=0):
    m = MagicMock()
    m.main.return_value = rc
    return m


awscli = pytest.importorskip("awscli", reason="awscli not installed in this environment")


class TestCredentialIsolation:

    def test_lock_exists(self):
        """_execute_lock must exist and be a threading.Lock."""
        assert hasattr(commands_mod, '_execute_lock')
        assert isinstance(commands_mod._execute_lock, type(threading.Lock()))

    def test_execute_command_uses_lock(self):
        """execute_command must acquire _execute_lock during execution."""
        lock_acquired = []

        original_locked = commands_mod._execute_locked

        def spy_locked(command, assume_role_arn=None):
            lock_acquired.append(commands_mod._execute_lock.locked())
            return original_locked(command, assume_role_arn)

        fake_sts = MagicMock()
        fake_sts.assume_role.return_value = _make_fake_sts_response(
            'AKIA_TEST', 'secret', 'token'
        )

        import io
        fake_out = io.StringIO('{"UserId":"AIDA..."}')

        with patch.object(commands_mod, '_execute_locked', side_effect=spy_locked), \
             patch('boto3.client', return_value=fake_sts):
            commands_mod.execute_command('aws sts get-caller-identity')

        # lock was held during _execute_locked
        assert any(lock_acquired), "Lock was not acquired during execute_command"

    def test_os_environ_restored_after_assume_role(self):
        """After execute_command with assume_role, os.environ must be restored."""
        original_key = os.environ.get('AWS_ACCESS_KEY_ID')

        fake_sts = MagicMock()
        fake_sts.assume_role.return_value = _make_fake_sts_response(
            'AKIA_ASSUMED', 'assumed_secret', 'assumed_token'
        )

        import io
        fake_stdout = io.StringIO('{"UserId":"AIDA..."}')
        fake_stderr = io.StringIO()

        def fake_main(cli_args):
            import sys
            sys.stdout.write('{"UserId":"AIDA..."}')
            return 0

        fake_driver = MagicMock()
        fake_driver.main.side_effect = fake_main

        with patch('boto3.client', return_value=fake_sts), \
             patch('commands.create_clidriver', return_value=fake_driver, create=True):
            # Patch create_clidriver inside the module
            import awscli.clidriver
            with patch.object(awscli.clidriver, 'create_clidriver', return_value=fake_driver):
                commands_mod.execute_command(
                    'aws sts get-caller-identity',
                    assume_role_arn='arn:aws:iam::111:role/TestRole',
                )

        # os.environ must be restored to original value
        assert os.environ.get('AWS_ACCESS_KEY_ID') == original_key, (
            f"os.environ was not restored! Now: {os.environ.get('AWS_ACCESS_KEY_ID')}"
        )

    def test_concurrent_calls_serialized(self):
        """Concurrent calls must be serialized (one at a time) via the lock."""
        concurrent_count = {'max': 0, 'current': 0}
        lock = threading.Lock()

        original_locked = commands_mod._execute_locked

        def slow_locked(command, assume_role_arn=None):
            with lock:
                concurrent_count['current'] += 1
                if concurrent_count['current'] > concurrent_count['max']:
                    concurrent_count['max'] = concurrent_count['current']
            time.sleep(0.05)
            result = original_locked(command, assume_role_arn)
            with lock:
                concurrent_count['current'] -= 1
            return result

        import awscli.clidriver
        fake_driver = MagicMock()
        fake_driver.main.return_value = 0

        errors = []

        def thread_fn():
            try:
                with patch.object(commands_mod, '_execute_locked', side_effect=slow_locked), \
                     patch.object(awscli.clidriver, 'create_clidriver', return_value=fake_driver):
                    commands_mod.execute_command('aws s3 ls')
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=thread_fn) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        # With the lock, max concurrent == 1
        assert concurrent_count['max'] == 1, (
            f"Expected max concurrent=1 (serialized), got {concurrent_count['max']}"
        )
