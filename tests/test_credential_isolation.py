"""
Tests for bouncer-sec-006: credential isolation in execute_command.

Subprocess approach: os.environ is never mutated,
credentials are passed via isolated env dict to subprocess.
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


def _fake_subprocess_result(stdout='ok', returncode=0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ''
    return result


class TestCredentialIsolation:

    def test_os_environ_not_mutated_during_assume_role(self):
        """After assume_role, os.environ must NOT contain the assumed credentials."""
        original_key = os.environ.get('AWS_ACCESS_KEY_ID')

        fake_sts = MagicMock()
        fake_sts.assume_role.return_value = _make_fake_sts_response(
            'AKIA_ASSUMED_KEY', 'assumed_secret', 'assumed_token'
        )

        captured_env = {}

        def fake_run(args, capture_output, text, env, timeout):
            captured_env.update(dict(env))
            return _fake_subprocess_result('{"UserId": "AIDA..."}')

        with patch('boto3.client', return_value=fake_sts), \
             patch.object(commands_mod.subprocess, 'run', side_effect=fake_run):
            commands_mod.execute_command(
                'aws sts get-caller-identity',
                assume_role_arn='arn:aws:iam::111111111111:role/TestRole',
            )

        # subprocess env must have assumed credentials
        assert captured_env.get('AWS_ACCESS_KEY_ID') == 'AKIA_ASSUMED_KEY'
        assert captured_env.get('AWS_SECRET_ACCESS_KEY') == 'assumed_secret'
        assert captured_env.get('AWS_SESSION_TOKEN') == 'assumed_token'

        # os.environ must NOT have changed
        assert os.environ.get('AWS_ACCESS_KEY_ID') == original_key, (
            f"os.environ was mutated! Before: {original_key}, "
            f"After: {os.environ.get('AWS_ACCESS_KEY_ID')}"
        )

    def test_concurrent_calls_credential_isolation(self):
        """Two threads: each subprocess must get its own credentials, os.environ clean."""
        CREDS_A = ('AKIA_THREAD_A_KEY', 'secret_a', 'token_a')
        CREDS_B = ('AKIA_THREAD_B_KEY', 'secret_b', 'token_b')

        captured = {}
        capture_lock = threading.Lock()
        errors = {}

        def make_sts_mock(access_key, secret_key, token):
            sts = MagicMock()
            sts.assume_role.return_value = _make_fake_sts_response(
                access_key, secret_key, token
            )
            return sts

        sts_clients = [make_sts_mock(*CREDS_A), make_sts_mock(*CREDS_B)]
        call_counter = {'n': 0}
        counter_lock = threading.Lock()

        def boto3_client_side_effect(service, **kwargs):
            with counter_lock:
                idx = call_counter['n'] % 2
                call_counter['n'] += 1
            return sts_clients[idx]

        def make_fake_subprocess(slot):
            def fake_run(args, capture_output, text, env, timeout):
                with capture_lock:
                    captured[slot] = {
                        'env_key': env.get('AWS_ACCESS_KEY_ID'),
                        'os_env_key': os.environ.get('AWS_ACCESS_KEY_ID'),
                    }
                time.sleep(0.05)
                return _fake_subprocess_result('{}')
            return fake_run

        def thread_fn(slot, role_arn):
            try:
                with patch('boto3.client', side_effect=boto3_client_side_effect), \
                     patch.object(commands_mod.subprocess, 'run',
                                  side_effect=make_fake_subprocess(slot)):
                    commands_mod.execute_command(
                        'aws sts get-caller-identity',
                        assume_role_arn=role_arn,
                    )
            except Exception as e:
                errors[slot] = str(e)

        t_a = threading.Thread(target=thread_fn, args=('a', 'arn:aws:iam::111:role/A'))
        t_b = threading.Thread(target=thread_fn, args=('b', 'arn:aws:iam::222:role/B'))
        t_a.start(); t_b.start()
        t_a.join(timeout=5); t_b.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert 'a' in captured and 'b' in captured

        for slot in ('a', 'b'):
            assert captured[slot]['os_env_key'] not in (CREDS_A[0], CREDS_B[0]), (
                f"Thread {slot}: os.environ contaminated: {captured[slot]['os_env_key']}"
            )

    def test_no_assume_role_env_untouched(self):
        """Without assume_role, os.environ must not change at all."""
        original_snapshot = {
            k: os.environ.get(k)
            for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')
        }

        with patch.object(commands_mod.subprocess, 'run',
                          return_value=_fake_subprocess_result('i-1234')):
            commands_mod.execute_command('aws ec2 describe-instances')

        for key, val in original_snapshot.items():
            assert os.environ.get(key) == val, f"os.environ[{key}] changed"

    def test_subprocess_env_has_aws_pager_disabled(self):
        """subprocess must always receive AWS_PAGER='' to prevent blocking."""
        captured_env = {}

        def fake_run(args, capture_output, text, env, timeout):
            captured_env['AWS_PAGER'] = env.get('AWS_PAGER')
            return _fake_subprocess_result('ok')

        with patch.object(commands_mod.subprocess, 'run', side_effect=fake_run):
            commands_mod.execute_command('aws s3 ls')

        assert captured_env.get('AWS_PAGER') == '', (
            f"AWS_PAGER not empty: {captured_env.get('AWS_PAGER')!r}"
        )
