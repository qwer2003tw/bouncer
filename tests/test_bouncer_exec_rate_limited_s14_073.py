"""
Sprint 14 #73: Regression tests — bouncer_exec.sh handles rate_limited status.

Uses bash subprocess to test the shell script behaviour for rate_limited responses.
"""
import subprocess
import json
import os
import shutil
import pytest

SCRIPT = os.path.join(
    os.path.dirname(__file__), '..', 'skills', 'bouncer-exec', 'scripts', 'bouncer_exec.sh'
)


def run_script(fake_mcporter_script: str, extra_args: list = None) -> subprocess.CompletedProcess:
    """Run bouncer_exec.sh with a fake mcporter that returns a preset response."""
    tmpdir = '/tmp/test_rate_limited_bouncer_exec'
    os.makedirs(tmpdir, exist_ok=True)

    fake_mcporter_path = os.path.join(tmpdir, 'mcporter')
    with open(fake_mcporter_path, 'w') as f:
        f.write(fake_mcporter_script)
    os.chmod(fake_mcporter_path, 0o755)

    env = os.environ.copy()
    env['PATH'] = tmpdir + ':' + env.get('PATH', '')

    args = extra_args or [
        '--reason', 'Test reason for rate limit check',
        'aws', 's3', 'ls',
    ]

    result = subprocess.run(
        ['bash', os.path.abspath(SCRIPT)] + args,
        capture_output=True, text=True, env=env, timeout=60
    )
    return result


class TestBounceExecRateLimited:
    """#73: bouncer_exec.sh retries on rate_limited status."""

    def test_rate_limited_then_success(self):
        """rate_limited on first call → retry → auto_approved succeeds."""
        # mcporter returns rate_limited first, then auto_approved on retry
        fake_script = '''\
#!/bin/bash
COUNTER_FILE=/tmp/test_rl_counter_$$_success
if [[ ! -f "$COUNTER_FILE" ]]; then
    echo 0 > "$COUNTER_FILE"
fi
COUNT=$(cat "$COUNTER_FILE")
if [[ "$COUNT" -eq 0 ]]; then
    echo 1 > "$COUNTER_FILE"
    echo '{"status": "rate_limited", "retry_after": 1}'
else
    rm -f "$COUNTER_FILE"
    echo '{"status": "auto_approved", "result": "s3-list-output", "command": "aws s3 ls", "account": "111111111111", "account_name": "Test"}'
fi
'''
        result = run_script(fake_script)
        assert result.returncode == 0, \
            f"Expected success after retry, got rc={result.returncode}\nstderr: {result.stderr}"
        assert 'rate limited' in result.stderr.lower() or '⏳' in result.stderr, \
            "Expected rate-limited message in stderr"

    def test_rate_limited_twice_fails(self):
        """rate_limited on both attempts → exit 1."""
        fake_script = '''\
#!/bin/bash
echo '{"status": "rate_limited", "retry_after": 1}'
'''
        result = run_script(fake_script)
        assert result.returncode != 0, \
            "Expected failure when rate_limited on both attempts"
        assert 'rate limited' in result.stderr.lower() or \
               'still rate limited' in result.stderr.lower() or \
               '⏳' in result.stderr, \
            f"Expected rate-limit error message. stderr: {result.stderr}"

    def test_normal_auto_approved_unaffected(self):
        """Normal auto_approved still works (regression guard)."""
        fake_script = '''\
#!/bin/bash
echo '{"status": "auto_approved", "result": "bucket-a\\nbucket-b", "command": "aws s3 ls", "account": "111111111111", "account_name": "Test"}'
'''
        result = run_script(fake_script)
        assert result.returncode == 0, \
            f"Normal auto_approved should succeed. stderr: {result.stderr}"
