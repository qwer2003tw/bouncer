#!/usr/bin/env bash
# run-tests.sh — 安全執行 pytest，防止 OOM
# 用法:
#   bash scripts/run-tests.sh tests/test_foo.py tests/test_bar.py
#   bash scripts/run-tests.sh --all   # 全套，自動分批

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAX_FILES=5
MEMORY_LIMIT="2G"
PYTEST_ARGS="-q --tb=short -p no:randomly"

# 顏色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

run_pytest_safe() {
    local files=("$@")
    echo -e "${YELLOW}Running ${#files[@]} test file(s) with ${MEMORY_LIMIT} memory limit...${NC}"

    if command -v systemd-run &>/dev/null; then
        # Try --user scope first (works without root), fallback to system scope
        local sd_flags="--scope --quiet -p MemoryMax=${MEMORY_LIMIT} -p MemorySwapMax=0"
        if systemd-run --user --scope true &>/dev/null 2>&1; then
            sd_flags="--user ${sd_flags}"
        fi
        # shellcheck disable=SC2086
        systemd-run ${sd_flags} \
            -- python3 -m pytest "${files[@]}" $PYTEST_ARGS
    else
        # fallback: ulimit
        (ulimit -v 2097152; python3 -m pytest "${files[@]}" $PYTEST_ARGS)
    fi
}

# --all mode: collect all test files and split into batches of MAX_FILES
if [[ "${1:-}" == "--all" ]]; then
    cd "$REPO_ROOT"
    mapfile -t all_files < <(python3 -m pytest tests/ -q --collect-only -p no:randomly -k "not safelist" 2>/dev/null | grep "::test_" | sed 's/::test_.*//' | sort -u)

    echo -e "${GREEN}Found ${#all_files[@]} test files. Splitting into batches of ${MAX_FILES}...${NC}"

    failed=0
    for ((i=0; i<${#all_files[@]}; i+=MAX_FILES)); do
        batch=("${all_files[@]:i:MAX_FILES}")
        echo -e "\n${YELLOW}Batch $((i/MAX_FILES+1)): ${batch[*]}${NC}"
        if ! run_pytest_safe "${batch[@]}"; then
            failed=1
        fi
    done

    if [[ $failed -eq 0 ]]; then
        echo -e "\n${GREEN}✅ All batches passed!${NC}"
    else
        echo -e "\n${RED}❌ Some batches failed.${NC}"
        exit 1
    fi
    exit 0
fi

# Normal mode: explicit file list
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 tests/test_foo.py [tests/test_bar.py ...]"
    echo "       $0 --all"
    exit 1
fi

if [[ $# -gt $MAX_FILES ]]; then
    echo -e "${RED}❌ Too many test files (${#}). Max ${MAX_FILES} per run.${NC}"
    echo "Use --all for full suite."
    exit 1
fi

cd "$REPO_ROOT"
run_pytest_safe "$@"
