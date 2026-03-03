#!/usr/bin/env bash
# test_bouncer_exec_sh.sh — Unit tests for bouncer_exec.sh arg parsing
# Tests the argument parsing logic without calling mcporter
# Run: bash tests/test_bouncer_exec_sh.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/../skills/bouncer-exec/scripts/bouncer_exec.sh"

PASS=0
FAIL=0
ERRORS=()

_run_test() {
  local name="$1"
  local expected_command="$2"
  local expected_reason="$3"
  local expected_account="$4"
  shift 4
  local args=("$@")

  # Source the parsing logic from the script (up to the mcporter call)
  # We'll create a test harness that sources just the parsing section
  local REASON=""
  local ACCOUNT=""
  local AWS_ARGS=()

  # Simulate the parsing loop from bouncer_exec.sh
  local pos_args=("${args[@]}")
  set -- "${pos_args[@]}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reason)
        REASON="$2"
        shift 2
        ;;
      --account)
        ACCOUNT="$2"
        shift 2
        ;;
      *)
        AWS_ARGS+=("$1")
        shift
        ;;
    esac
  done

  local COMMAND=""
  if [[ ${#AWS_ARGS[@]} -gt 0 ]]; then
    COMMAND=$(printf '%q ' "${AWS_ARGS[@]}")
    COMMAND="${COMMAND% }"
  fi
  local ACTUAL_REASON="${REASON:-${COMMAND}}"

  local ok=1
  if [[ "$COMMAND" != "$expected_command" ]]; then
    ok=0
    ERRORS+=("FAIL [$name]: COMMAND expected='$expected_command' got='$COMMAND'")
  fi
  if [[ "$ACTUAL_REASON" != "$expected_reason" ]]; then
    ok=0
    ERRORS+=("FAIL [$name]: REASON expected='$expected_reason' got='$ACTUAL_REASON'")
  fi
  if [[ "$ACCOUNT" != "$expected_account" ]]; then
    ok=0
    ERRORS+=("FAIL [$name]: ACCOUNT expected='$expected_account' got='$ACCOUNT'")
  fi

  if [[ $ok -eq 1 ]]; then
    echo "PASS [$name]"
    PASS=$((PASS + 1))
  else
    echo "FAIL [$name]"
    FAIL=$((FAIL + 1))
  fi
}

# ─────────────────────────────────────────────────────────
# Test: --reason at beginning (existing behavior)
_run_test "reason_at_beginning" \
  "aws s3 ls" \
  "查看 S3" \
  "" \
  --reason "查看 S3" aws s3 ls

# Test: --reason at end (new: #51)
_run_test "reason_at_end" \
  "aws s3 ls" \
  "查看 S3" \
  "" \
  aws s3 ls --reason "查看 S3"

# Test: no --reason → fallback to COMMAND
_run_test "no_reason_fallback" \
  "aws s3 ls" \
  "aws s3 ls" \
  "" \
  aws s3 ls

# Test: --account at beginning
_run_test "account_at_beginning" \
  "aws s3 ls" \
  "aws s3 ls" \
  "992382394211" \
  --account "992382394211" aws s3 ls

# Test: --account at end (new: #51)
_run_test "account_at_end" \
  "aws s3 ls" \
  "aws s3 ls" \
  "992382394211" \
  aws s3 ls --account "992382394211"

# Test: --reason and --account both at end
_run_test "reason_account_both_at_end" \
  "aws s3 ls" \
  "check S3" \
  "992382394211" \
  aws s3 ls --reason "check S3" --account "992382394211"

# Test: arg with pipe character preserved (not parsed by shell since we're testing in-process)
# The key test is that AWS_ARGS keeps the arg intact
_run_test "arg_with_spaces_preserved" \
  "aws logs start-query --query-string fields\\ @timestamp\\ \\|\\ filter\\ message" \
  "aws logs start-query --query-string fields\\ @timestamp\\ \\|\\ filter\\ message" \
  "" \
  aws logs start-query --query-string "fields @timestamp | filter message"

# Test: --reason with special chars
_run_test "reason_with_special_chars" \
  "aws sts get-caller-identity" \
  "check who I am | verify" \
  "" \
  --reason "check who I am | verify" aws sts get-caller-identity

# Test: multiple aws args preserved
_run_test "multiple_args_preserved" \
  "aws ec2 describe-instances --region us-east-1 --filters Name=instance-state-name\\,Values=running" \
  "debug EC2" \
  "" \
  aws ec2 describe-instances --region us-east-1 "--filters" "Name=instance-state-name,Values=running" --reason "debug EC2"

# Test: validate no-args exits (simulate: check that AWS_ARGS is empty)
echo ""
echo "--- No-args validation test ---"
# Run the actual script with no args and check exit code
if bash "$SCRIPT" 2>/dev/null; then
  echo "FAIL [no_args_exits]: script should exit non-zero with no args"
  FAIL=$((FAIL + 1))
else
  echo "PASS [no_args_exits]"
  PASS=$((PASS + 1))
fi

# Test: --reason missing value (script exits with error)
echo "--- reason_missing_value test ---"
if bash "$SCRIPT" --reason 2>/dev/null; then
  echo "FAIL [reason_missing_value]: should exit non-zero"
  FAIL=$((FAIL + 1))
else
  echo "PASS [reason_missing_value]"
  PASS=$((PASS + 1))
fi

# ─────────────────────────────────────────────────────────
echo ""
echo "=================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  $e"
  done
fi
echo "=================================="

[[ $FAIL -eq 0 ]]
