#!/usr/bin/env bash
# bouncer_exec.sh -- Execute AWS CLI commands via Bouncer with clean output
# Usage: bouncer_exec.sh [--reason "..."] [--account <id>] [--source "Custom Source"] [--json-args '{...}'] <aws command...>
#
# --json-args: Pass the full command as a JSON object to bypass shell special-char issues.
#   The JSON object must contain a "command" key. Other keys (reason/source/trust_scope/account)
#   are overridden by the CLI flags. This is useful when the command contains | pipes, quotes,
#   or other characters that would be mangled by shell expansion.
#
#   Example:
#     bouncer_exec.sh --reason "Query Lambda log to investigate error" \
#       --json-args '{"command": "aws logs start-query --query-string \"fields @timestamp | filter level = ERROR\""}'
set -euo pipefail

# Config
POLL_INTERVAL=10
MAX_POLL_TIME=600  # 10 minutes
TRUST_SCOPE="agent-bouncer-exec"

# Validate reason
_validate_reason() {
  local reason="$1"
  if [[ -z "$reason" ]]; then
    echo "❌ --reason 必填，請說明為什麼執行這個命令" >&2
    echo "   Usage: bouncer_exec.sh --reason \"<原因>\" [--account <id>] [--source \"<來源>\"] <aws command...>" >&2
    exit 1
  fi
  if [[ ${#reason} -lt 15 ]]; then
    echo "❌ --reason 太短（需至少 15 字），請說明執行目的" >&2
    exit 1
  fi
  if [[ "$reason" == aws\ * ]]; then
    echo "❌ --reason 不能是命令字串本身，請說明執行目的" >&2
    exit 1
  fi
}

# Parse optional flags
REASON=""
ACCOUNT=""
CUSTOM_SOURCE=""
JSON_ARGS_MODE=false
JSON_ARGS_VALUE=""

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
    --source)
      CUSTOM_SOURCE="$2"
      shift 2
      ;;
    --json-args)
      JSON_ARGS_MODE=true
      JSON_ARGS_VALUE="$2"
      shift 2
      ;;
    *)
      break
      ;;
  esac
done

# Validate
# In --json-args mode, no positional aws args required; otherwise at least one arg needed
if [[ "$JSON_ARGS_MODE" == "false" && $# -eq 0 ]]; then
  echo "Usage: bouncer_exec.sh --reason \"<原因>\" [--account <id>] [--source \"<來源>\"] <aws command...>" >&2
  echo "   Or: bouncer_exec.sh --reason \"<原因>\" --json-args '{\"command\": \"aws ...\"}'" >&2
  exit 1
fi

_validate_reason "$REASON"

# Helper: extract and clean result
extract_result() {
  local json="$1"
  local result

  result=$(echo "$json" | jq -r '.result // empty' 2>/dev/null)

  if [[ -z "$result" ]]; then
    echo "$json"
    return
  fi

  if echo "$result" | jq . >/dev/null 2>&1; then
    echo "$result" | jq .
  else
    printf '%b\n' "$result"
  fi
}

# Build MCPORTER_JSON
if [[ "$JSON_ARGS_MODE" == "true" ]]; then
  # --json-args mode: use the JSON object from the user, override reason/source/trust_scope
  # Validate the input is valid JSON first
  if ! echo "$JSON_ARGS_VALUE" | jq . >/dev/null 2>&1; then
    echo "❌ --json-args 的值不是合法 JSON" >&2
    exit 1
  fi

  # Determine source: use custom if provided, else derive from command field in JSON
  if [[ -n "$CUSTOM_SOURCE" ]]; then
    SOURCE="$CUSTOM_SOURCE"
  else
    COMMAND_FROM_JSON=$(echo "$JSON_ARGS_VALUE" | jq -r '.command // empty' 2>/dev/null)
    SOURCE="Agent (${COMMAND_FROM_JSON})"
  fi

  # Merge: start from user-supplied JSON, then overlay reason/source/trust_scope
  # Also conditionally add account if provided
  if [[ -n "$ACCOUNT" ]]; then
    MCPORTER_JSON=$(echo "$JSON_ARGS_VALUE" | jq \
      --arg reason "$REASON" \
      --arg source "$SOURCE" \
      --arg trust_scope "$TRUST_SCOPE" \
      --arg account "$ACCOUNT" \
      '. + {reason: $reason, source: $source, trust_scope: $trust_scope, account: $account}')
  else
    MCPORTER_JSON=$(echo "$JSON_ARGS_VALUE" | jq \
      --arg reason "$REASON" \
      --arg source "$SOURCE" \
      --arg trust_scope "$TRUST_SCOPE" \
      '. + {reason: $reason, source: $source, trust_scope: $trust_scope}')
  fi
else
  # Normal mode: build command string from positional args
  COMMAND_DISPLAY="$*"

  # Source: use custom if provided, else default "Agent (command)"
  SOURCE="${CUSTOM_SOURCE:-Agent (${COMMAND_DISPLAY})}"

  # Build JSON args via jq (safe: handles all special characters)
  # jq --arg properly escapes quotes, pipes, backslashes, unicode, etc.
  if [[ -n "$ACCOUNT" ]]; then
    MCPORTER_JSON=$(jq -n \
      --arg command "$COMMAND_DISPLAY" \
      --arg reason "$REASON" \
      --arg source "$SOURCE" \
      --arg trust_scope "$TRUST_SCOPE" \
      --arg account "$ACCOUNT" \
      '{command: $command, reason: $reason, source: $source, trust_scope: $trust_scope, account: $account}')
  else
    MCPORTER_JSON=$(jq -n \
      --arg command "$COMMAND_DISPLAY" \
      --arg reason "$REASON" \
      --arg source "$SOURCE" \
      --arg trust_scope "$TRUST_SCOPE" \
      '{command: $command, reason: $reason, source: $source, trust_scope: $trust_scope}')
  fi
fi

# Execute
RESPONSE=$(mcporter call bouncer bouncer_execute --args "$MCPORTER_JSON" 2>&1)

# Extract status
STATUS=$(echo "$RESPONSE" | jq -r '.status // empty' 2>/dev/null)

if [[ -z "$STATUS" ]]; then
  echo "Error: unexpected response from bouncer" >&2
  echo "$RESPONSE" >&2
  exit 1
fi

# Handle response
case "$STATUS" in
  auto_approved|trust_auto_approved|grant_auto_approved|approved)
    extract_result "$RESPONSE"
    ;;
  pending_approval)
    REQUEST_ID=$(echo "$RESPONSE" | jq -r '.request_id // empty' 2>/dev/null)
    if [[ -z "$REQUEST_ID" ]]; then
      echo "Error: pending_approval but no request_id" >&2
      echo "$RESPONSE" >&2
      exit 1
    fi

    echo "⏳ 等待審批中... (request: ${REQUEST_ID})" >&2

    ELAPSED=0
    while [[ $ELAPSED -lt $MAX_POLL_TIME ]]; do
      sleep "$POLL_INTERVAL"
      ELAPSED=$((ELAPSED + POLL_INTERVAL))

      POLL_RESPONSE=$(mcporter call bouncer bouncer_status "request_id=${REQUEST_ID}" 2>&1)
      POLL_STATUS=$(echo "$POLL_RESPONSE" | jq -r '.status // empty' 2>/dev/null)

      case "$POLL_STATUS" in
        approved)
          extract_result "$POLL_RESPONSE"
          exit 0
          ;;
        rejected)
          echo "❌ 請求被拒絕" >&2
          exit 1
          ;;
        pending_approval)
          ;;
        *)
          echo "Error: unexpected poll status: ${POLL_STATUS}" >&2
          echo "$POLL_RESPONSE" >&2
          exit 1
          ;;
      esac
    done

    echo "⏰ 等待審批超時（10 分鐘）" >&2
    exit 1
    ;;
  rejected)
    echo "❌ 請求被拒絕" >&2
    exit 1
    ;;
  rate_limited)
    echo "⏳ 請求被 rate limited，等待 15 秒後重試..." >&2
    sleep 15
    RESPONSE=$(mcporter call bouncer bouncer_execute --args "$MCPORTER_JSON" 2>&1)
    STATUS=$(echo "$RESPONSE" | jq -r '.status // empty' 2>/dev/null)
    if [[ "$STATUS" == "auto_approved" || "$STATUS" == "trust_auto_approved" || "$STATUS" == "grant_auto_approved" || "$STATUS" == "approved" ]]; then
      extract_result "$RESPONSE"
    elif [[ "$STATUS" == "pending_approval" ]]; then
      REQUEST_ID=$(echo "$RESPONSE" | jq -r '.request_id // empty' 2>/dev/null)
      echo "⏳ 請求需要審批 (request: $REQUEST_ID)" >&2
    elif [[ "$STATUS" == "rate_limited" ]]; then
      echo "Error: still rate limited after retry" >&2
      exit 1
    else
      echo "Error: unexpected status after retry: ${STATUS}" >&2
      exit 1
    fi
    ;;
  *)
    echo "Error: unexpected status: ${STATUS}" >&2
    echo "$RESPONSE" >&2
    exit 1
    ;;
esac
