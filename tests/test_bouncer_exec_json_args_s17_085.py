"""
Sprint 17 #85: Regression tests -- bouncer_exec.sh --json-args mode.

Tests that --json-args mode correctly:
1. Passes the command JSON verbatim (preserving pipe chars, quotes, etc.)
2. Overrides reason/source/trust_scope with CLI flags
3. Optionally adds account when --account is provided
4. Rejects invalid JSON in --json-args
5. Normal (non-json-args) mode still works (regression guard)
"""
import subprocess
import json
import os
import pytest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'skills', 'bouncer-exec', 'scripts', 'bouncer_exec.sh'
))

CAPTURED_ARGS_FILE = '/tmp/test_json_args_captured_s17_085'


def _cleanup():
    try:
        os.remove(CAPTURED_ARGS_FILE)
    except FileNotFoundError:
        pass


def run_script(fake_mcporter_script: str, extra_args: list) -> subprocess.CompletedProcess:
    """Run bouncer_exec.sh with a fake mcporter that captures/returns preset responses."""
    tmpdir = '/tmp/test_json_args_bouncer_exec_s17'
    os.makedirs(tmpdir, exist_ok=True)

    fake_mcporter_path = os.path.join(tmpdir, 'mcporter')
    with open(fake_mcporter_path, 'w') as f:
        f.write(fake_mcporter_script)
    os.chmod(fake_mcporter_path, 0o755)

    env = os.environ.copy()
    env['PATH'] = tmpdir + ':' + env.get('PATH', '')

    result = subprocess.run(
        ['bash', SCRIPT] + extra_args,
        capture_output=True, text=True, env=env, timeout=60
    )
    return result


class TestBounceExecJsonArgs:
    """#85: --json-args mode passes pipe chars and special characters correctly."""

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_json_args_pipe_char_preserved(self):
        """--json-args passes command with | pipe char to Bouncer unmangled."""
        captured_file = CAPTURED_ARGS_FILE
        fake_script = f'''\
#!/bin/bash
# Capture the --args value for inspection
ARGS_VAL=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--args" ]]; then
    ARGS_VAL="$2"
    shift 2
  else
    shift
  fi
done
echo "$ARGS_VAL" > "{captured_file}"
echo '{{"status": "auto_approved", "result": "query-id-12345", "command": "aws logs start-query", "account": "111111111111", "account_name": "Test"}}'
'''
        cmd_with_pipe = 'aws logs start-query --query-string "fields @timestamp | filter level = ERROR"'
        result = run_script(fake_script, [
            '--reason', 'Query Lambda log to investigate error production issue',
            '--json-args', json.dumps({"command": cmd_with_pipe}),
        ])
        assert result.returncode == 0, \
            f"Expected success, rc={result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify the captured args contain the pipe character intact
        assert os.path.exists(captured_file), "mcporter was not called"
        with open(captured_file) as f:
            captured = f.read().strip()

        captured_json = json.loads(captured)
        assert '|' in captured_json['command'], \
            f"Pipe char lost in command: {captured_json['command']}"
        assert 'fields @timestamp' in captured_json['command'], \
            f"Query string mangled: {captured_json['command']}"
        assert captured_json['reason'] == 'Query Lambda log to investigate error production issue'
        assert captured_json['trust_scope'] == 'agent-bouncer-exec'

    def test_json_args_overrides_reason_source_trust_scope(self):
        """--json-args: CLI reason/source/trust_scope override any values in JSON input."""
        captured_file = CAPTURED_ARGS_FILE
        fake_script = f'''\
#!/bin/bash
ARGS_VAL=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--args" ]]; then
    ARGS_VAL="$2"
    shift 2
  else
    shift
  fi
done
echo "$ARGS_VAL" > "{captured_file}"
echo '{{"status": "auto_approved", "result": "ok", "command": "aws s3 ls", "account": "111111111111", "account_name": "Test"}}'
'''
        # Pass JSON with pre-filled reason/source that should be overridden
        input_json = {
            "command": "aws s3 ls",
            "reason": "OLD_REASON_SHOULD_BE_OVERRIDDEN",
            "source": "OLD_SOURCE_SHOULD_BE_OVERRIDDEN",
            "trust_scope": "OLD_SCOPE_SHOULD_BE_OVERRIDDEN",
        }
        result = run_script(fake_script, [
            '--reason', 'List S3 buckets to verify backup bucket exists correctly',
            '--source', 'Custom Bot (Sprint 17 test)',
            '--json-args', json.dumps(input_json),
        ])
        assert result.returncode == 0, \
            f"Expected success, rc={result.returncode}\nstderr: {result.stderr}"

        with open(captured_file) as f:
            captured_json = json.loads(f.read().strip())

        assert captured_json['reason'] == 'List S3 buckets to verify backup bucket exists correctly'
        assert captured_json['source'] == 'Custom Bot (Sprint 17 test)'
        assert captured_json['trust_scope'] == 'agent-bouncer-exec'

    def test_json_args_with_account_flag(self):
        """--json-args + --account adds account field to the JSON."""
        captured_file = CAPTURED_ARGS_FILE
        fake_script = f'''\
#!/bin/bash
ARGS_VAL=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--args" ]]; then
    ARGS_VAL="$2"
    shift 2
  else
    shift
  fi
done
echo "$ARGS_VAL" > "{captured_file}"
echo '{{"status": "auto_approved", "result": "ok", "command": "aws ec2 describe-instances", "account": "992382394211", "account_name": "Dev"}}'
'''
        result = run_script(fake_script, [
            '--reason', 'Describe EC2 instances to check node status in Dev account',
            '--account', '992382394211',
            '--json-args', json.dumps({"command": "aws ec2 describe-instances"}),
        ])
        assert result.returncode == 0, \
            f"Expected success, rc={result.returncode}\nstderr: {result.stderr}"

        with open(captured_file) as f:
            captured_json = json.loads(f.read().strip())

        assert captured_json.get('account') == '992382394211', \
            f"account not set correctly: {captured_json}"

    def test_json_args_invalid_json_rejected(self):
        """--json-args with invalid JSON exits with error."""
        fake_script = '''\
#!/bin/bash
echo '{"status": "auto_approved", "result": "ok"}'
'''
        result = run_script(fake_script, [
            '--reason', 'Test invalid JSON rejection for safety check',
            '--json-args', 'THIS IS NOT JSON',
        ])
        assert result.returncode != 0, \
            "Expected failure for invalid JSON in --json-args"
        assert '合法 JSON' in result.stderr or 'JSON' in result.stderr, \
            f"Expected JSON error message. stderr: {result.stderr}"

    def test_json_args_mode_no_positional_args_required(self):
        """In --json-args mode, no positional <aws command> args are needed."""
        fake_script = '''\
#!/bin/bash
echo '{"status": "auto_approved", "result": "bucket-list", "command": "aws s3 ls", "account": "111111111111", "account_name": "Test"}'
'''
        # No positional args after the flags -- should NOT fail with "Usage:" error
        result = run_script(fake_script, [
            '--reason', 'List buckets to confirm backup exists and is accessible',
            '--json-args', json.dumps({"command": "aws s3 ls"}),
        ])
        assert result.returncode == 0, \
            f"Expected success with no positional args in --json-args mode, rc={result.returncode}\nstderr: {result.stderr}"

    def test_normal_mode_unaffected_regression(self):
        """Normal positional-args mode still works (regression guard for #85)."""
        fake_script = '''\
#!/bin/bash
echo '{"status": "auto_approved", "result": "bucket-a\\nbucket-b", "command": "aws s3 ls", "account": "111111111111", "account_name": "Test"}'
'''
        result = run_script(fake_script, [
            '--reason', 'List S3 buckets to verify backup bucket exists in Default account',
            'aws', 's3', 'ls',
        ])
        assert result.returncode == 0, \
            f"Normal mode should still work. rc={result.returncode}\nstderr: {result.stderr}"
        assert 'bucket' in result.stdout.lower() or result.stdout.strip() != '', \
            f"Expected output. stdout: {result.stdout}"
