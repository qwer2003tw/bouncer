"""
Tests for bouncer-sec-006: Credential Isolation in execute_command

Verifies that concurrent calls to execute_command with different assume_role_arn
values do not cross-contaminate credentials via os.environ mutation.
"""
import os
import sys
import threading
import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_sts_response(access_key: str, secret_key: str, token: str):
    """Build a minimal STS assume_role response dict."""
    return {
        'Credentials': {
            'AccessKeyId': access_key,
            'SecretAccessKey': secret_key,
            'SessionToken': token,
        }
    }


def _get_execute_command():
    """Import the real execute_command from src/commands.py."""
    src_path = os.path.join(os.path.dirname(__file__), '..', 'src')
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    import commands
    return commands.execute_command, commands


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestCredentialIsolation:
    """Verify that execute_command never mutates os.environ for credentials."""

    # ------------------------------------------------------------------
    # 1. os.environ must NOT be mutated during an assume-role call
    # ------------------------------------------------------------------

    def test_os_environ_not_mutated_during_assume_role(self):
        """execute_command must NOT write AWS_* keys into os.environ."""
        execute_command, commands_mod = _get_execute_command()

        sts_mock = MagicMock()
        sts_mock.assume_role.return_value = _make_fake_sts_response(
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            'AQoXnyc4lcK4W4rs//////////wEaoAK1wvxJY12',
        )

        original_key = os.environ.get('AWS_ACCESS_KEY_ID', '__not_set__')

        driver_instances = {}

        def fake_cli_driver(session):
            driver_instances['session'] = session
            m = MagicMock()
            m.main.return_value = 0
            return m

        with patch('boto3.client', return_value=sts_mock), \
             patch.object(commands_mod, '_AWSCLIDriver', side_effect=fake_cli_driver), \
             patch.object(commands_mod, '_AWSCLI_AVAILABLE', True):
            execute_command(
                'aws sts get-caller-identity',
                assume_role_arn='arn:aws:iam::123456789012:role/TestRole',
            )

        # os.environ must be untouched
        assert os.environ.get('AWS_ACCESS_KEY_ID', '__not_set__') == original_key, (
            "os.environ['AWS_ACCESS_KEY_ID'] was mutated!"
        )

        # The session passed to CLIDriver must carry the injected credentials
        assert 'session' in driver_instances
        creds = driver_instances['session'].get_credentials()
        assert creds is not None
        frozen = creds.get_frozen_credentials()
        assert frozen.access_key == 'AKIAIOSFODNN7EXAMPLE'
        assert frozen.secret_key == 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        assert frozen.token == 'AQoXnyc4lcK4W4rs//////////wEaoAK1wvxJY12'

    # ------------------------------------------------------------------
    # 2. Concurrent calls receive correct, isolated credentials
    # ------------------------------------------------------------------

    def test_concurrent_calls_credential_isolation(self):
        """
        Two threads call execute_command simultaneously with different role ARNs.
        Each CLIDriver instance must receive *its own* credentials, not the other
        thread's.
        """
        execute_command, commands_mod = _get_execute_command()

        CREDS_A = ('AKIA_THREAD_A_KEY', 'secret_a', 'token_a')
        CREDS_B = ('AKIA_THREAD_B_KEY', 'secret_b', 'token_b')

        captured = {}  # keyed by 'a' / 'b'
        capture_lock = threading.Lock()

        def make_sts_mock(access_key, secret_key, token):
            sts = MagicMock()
            sts.assume_role.return_value = _make_fake_sts_response(
                access_key, secret_key, token
            )
            return sts

        # Track which STS client to hand out per call (alternating)
        sts_clients = [make_sts_mock(*CREDS_A), make_sts_mock(*CREDS_B)]
        call_counter = {'n': 0}
        counter_lock = threading.Lock()

        def boto3_client_side_effect(service, **kwargs):
            with counter_lock:
                idx = call_counter['n'] % 2
                call_counter['n'] += 1
            return sts_clients[idx]

        def make_driver_factory(slot):
            """Creates a CLIDriver side_effect that records session credentials."""
            def factory(session):
                try:
                    creds = session.get_credentials()
                    if creds is not None:
                        frozen = creds.get_frozen_credentials()
                        data = {
                            'access_key': frozen.access_key,
                            'secret_key': frozen.secret_key,
                            'token': frozen.token,
                            # Record os.environ state — must NOT have changed
                            'env_key': os.environ.get('AWS_ACCESS_KEY_ID'),
                        }
                    else:
                        data = {'access_key': None, 'env_key': os.environ.get('AWS_ACCESS_KEY_ID')}
                except Exception as e:
                    data = {'error': str(e)}

                with capture_lock:
                    captured[slot] = data

                # Slow down so both threads overlap
                time.sleep(0.05)

                m = MagicMock()
                m.main.return_value = 0
                return m
            return factory

        errors = {}

        def thread_fn(slot, role_arn):
            try:
                with patch.object(commands_mod, '_AWSCLIDriver',
                                  side_effect=make_driver_factory(slot)), \
                     patch.object(commands_mod, '_AWSCLI_AVAILABLE', True):
                    execute_command(
                        'aws sts get-caller-identity',
                        assume_role_arn=role_arn,
                    )
            except Exception as e:
                errors[slot] = str(e)

        with patch('boto3.client', side_effect=boto3_client_side_effect):
            t_a = threading.Thread(
                target=thread_fn,
                args=('a', 'arn:aws:iam::111111111111:role/RoleA'),
            )
            t_b = threading.Thread(
                target=thread_fn,
                args=('b', 'arn:aws:iam::222222222222:role/RoleB'),
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=5)
            t_b.join(timeout=5)

        # Both threads must complete without error
        assert not errors, f"Thread errors: {errors}"
        assert 'a' in captured, "Thread A did not capture credentials"
        assert 'b' in captured, "Thread B did not capture credentials"
        assert 'error' not in captured.get('a', {}), captured.get('a')
        assert 'error' not in captured.get('b', {}), captured.get('b')

        # Each thread must have gotten its own credentials
        assert captured['a']['access_key'] == CREDS_A[0], (
            f"Thread A got wrong key: {captured['a']['access_key']}"
        )
        assert captured['b']['access_key'] == CREDS_B[0], (
            f"Thread B got wrong key: {captured['b']['access_key']}"
        )

        # os.environ must NOT have been set to either thread's credentials
        env_key = os.environ.get('AWS_ACCESS_KEY_ID')
        assert env_key not in (CREDS_A[0], CREDS_B[0]), (
            f"os.environ was contaminated with credentials: {env_key}"
        )

    # ------------------------------------------------------------------
    # 3. No assume_role → no credential injection, os.environ untouched
    # ------------------------------------------------------------------

    def test_no_assume_role_env_untouched(self):
        """Without assume_role_arn, os.environ must not be touched at all."""
        execute_command, commands_mod = _get_execute_command()

        before = dict(os.environ)

        def fake_cli_driver(session):
            m = MagicMock()
            m.main.return_value = 0
            return m

        with patch.object(commands_mod, '_AWSCLIDriver', side_effect=fake_cli_driver), \
             patch.object(commands_mod, '_AWSCLI_AVAILABLE', True):
            execute_command('aws sts get-caller-identity')

        after = dict(os.environ)
        assert before == after, "os.environ was modified even without assume_role_arn"
