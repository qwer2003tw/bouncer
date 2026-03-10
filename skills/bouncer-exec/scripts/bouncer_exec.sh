#!/usr/bin/env bash
# bouncer_exec.sh — Execute AWS CLI commands via Bouncer with clean output
# Usage: bouncer_exec.sh [--reason "..."] [--account <id>] [--source "Custom Source"] <aws command...>
# Usage: bouncer_exec.sh [--reason "..."] [--account <id>] [--source "Custom Source"] --json-args '<JSON>'
set -euo pipefail

# ── Config ──
POLL_INTERVAL=10
MAX_POLL_TIME=600  # 10 minutes
TRUST_SCOPE="agent-bouncer-exec"

# ── Validate reason ──
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

# ── Parse optional flags ──
REASON=""
ACCOUNT=""
CUSTOM_SOURCE=""
JSON_ARGS=""

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
      JSON_ARGS="$2"
      shift 2
      ;;
    *)
      break
      ;;
  esac
done

# ── Validate ──
if [[ -z "$JSON_ARGS" && $# -eq 0 ]]; then
  echo "Usage: bouncer_exec.sh --reason \"<原因>\" [--account <id>] [--source \"<來源>\"] <aws command...>" >&2
  exit 1
fi

_validate_reason "$REASON"

# ── Helper: extract and clean result ──
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

# ── Build JSON args ──
if [[ -n "$JSON_ARGS" ]]; then
  # --json-args mode: validate JSON, then overlay reason/source/trust_scope/account
  if ! echo "$JSON_ARGS" | jq . >/dev/null 2>&1; then
    echo "❌ --json-args 的值不是合法 JSON，請傳入有效的 JSON object" >&2
    exit 1
  fi

  COMMAND_DISPLAY=$(echo "$JSON_ARGS" | jq -r '.command // empty')
  SOURCE="${CUSTOM_SOURCE:-Agent (${COMMAND_DISPLAY})}"

  # Build final JSON: start from input, then overlay controlled fields
  if [[ -n "$ACCOUNT" ]]; then
    MCPORTER_JSON=$(echo "$JSON_ARGS" | jq \
      --arg reason "$REASON" \
      --arg source "$SOURCE" \
      --arg trust_scope "$TRUST_SCOPE" \
      --arg account "$ACCOUNT" \
      '. + {reason: $reason, source: $source, trust_scope: $trust_scope, account: $account}')
  else
    MCPORTER_JSON=$(echo "$JSON_ARGS" | jq \
      --arg reason "$REASON" \
      --arg source "$SOURCE" \
      --arg trust_scope "$TRUST_SCOPE" \
      '. + {reason: $reason, source: $source, trust_scope: $trust_scope}')
  fi
else
  # Normal positional-args mode
  COMMAND_DISPLAY="$*"
  SOURCE="${CUSTOM_SOURCE:-Agent (${COMMAND_DISPLAY})}"

  # Build JSON args via jq (safe: handles all special characters)
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

# ── Execute ──
RESPONSE=$(mcporter call bouncer bouncer_execute --args "$MCPORTER_JSON" 2>&1)

# Extract status
STATUS=$(echo "$RESPONSE" | jq -r '.status // empty' 2>/dev/null)

if [[ -z "$STATUS" ]]; then
  echo "Error: unexpected response from bouncer" >&2
  echo "$RESPONSE" >&2
  exit 1
fi

# ── Handle response ──
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
