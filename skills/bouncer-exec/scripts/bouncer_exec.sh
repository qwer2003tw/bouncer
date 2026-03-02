#!/usr/bin/env bash
# bouncer_exec.sh — Execute AWS CLI commands via Bouncer with clean output
# Usage: bouncer_exec.sh [--reason "..."] [--account <id>] <aws command...>
set -euo pipefail

# ── Config ──
POLL_INTERVAL=10
MAX_POLL_TIME=600  # 10 minutes
SOURCE_PREFIX="Agent"
TRUST_SCOPE="agent-bouncer-exec"

# ── Parse optional flags ──
REASON=""
ACCOUNT=""

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
      break
      ;;
  esac
done

# ── Validate ──
if [[ $# -eq 0 ]]; then
  echo "Usage: bouncer_exec.sh [--reason \"...\"] [--account <id>] <aws command...>" >&2
  exit 1
fi

COMMAND="$*"
REASON="${REASON:-$COMMAND}"
SOURCE="${SOURCE_PREFIX} (${COMMAND})"

# ── Helper: extract and clean result ──
extract_result() {
  local json="$1"
  local result

  # Extract .result from JSON
  result=$(echo "$json" | jq -r '.result // empty' 2>/dev/null)

  if [[ -z "$result" ]]; then
    echo "$json"
    return
  fi

  # Try to pretty-print if result is valid JSON
  if echo "$result" | jq . >/dev/null 2>&1; then
    echo "$result" | jq .
  else
    # Unescape \n to real newlines
    printf '%b\n' "$result"
  fi
}

# ── Build mcporter args ──
MCPORTER_ARGS=(
  call bouncer bouncer_execute
  "command=${COMMAND}"
  "reason=${REASON}"
  "source=${SOURCE}"
  "trust_scope=${TRUST_SCOPE}"
)

if [[ -n "$ACCOUNT" ]]; then
  MCPORTER_ARGS+=("account=${ACCOUNT}")
fi

# ── Execute ──
RESPONSE=$(mcporter "${MCPORTER_ARGS[@]}" 2>&1)

# Extract status
STATUS=$(echo "$RESPONSE" | jq -r '.status // empty' 2>/dev/null)

if [[ -z "$STATUS" ]]; then
  # Not valid JSON or no status field — dump raw output
  echo "Error: unexpected response from bouncer" >&2
  echo "$RESPONSE" >&2
  exit 1
fi

# ── Handle response ──
case "$STATUS" in
  auto_approved|trust_auto_approved|approved)
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
          # Still waiting, continue polling
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
  *)
    echo "Error: unexpected status: ${STATUS}" >&2
    echo "$RESPONSE" >&2
    exit 1
    ;;
esac
