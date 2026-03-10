#!/usr/bin/env bash
# run-tests.sh — 安全執行 pytest，防止 OOM
# 用法:
#   bash scripts/run-tests.sh tests/test_foo.py tests/test_bar.py
#   bash scripts/run-tests.sh --all   # 全套，自動分批

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAX_FILES=5
MEMORY_LIMIT="2G"
MEMORY_HIGH="1600M"
MEMORY_SWAP="512M"
PYTEST_ARGS="-v --tb=short -p no:randomly"

# 顏色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

run_pytest_safe() {
    local files=("$@")
    local exit_code=0
    echo -e "${YELLOW}Running ${#files[@]} test file(s) with ${MEMORY_HIGH} soft / ${MEMORY_LIMIT} hard limit...${NC}"

    if command -v systemd-run &>/dev/null; then
        # Try --user scope first (works without root), fallback to system scope
        local sd_flags="--scope --quiet -p MemoryHigh=${MEMORY_HIGH} -p MemoryMax=${MEMORY_LIMIT} -p MemorySwapMax=${MEMORY_SWAP}"
        if systemd-run --user --scope true &>/dev/null 2>&1; then
            sd_flags="--user ${sd_flags}"
        fi
        # shellcheck disable=SC2086
        systemd-run ${sd_flags} \
            -- bash -c '
                python3 -m pytest "$@" '"$PYTEST_ARGS"'
                rc=$?
                # Report memory peak from inside the cgroup scope
                CG="/sys/fs/cgroup$(cat /proc/self/cgroup 2>/dev/null | head -1 | cut -d: -f3)"
                if [[ -f "$CG/memory.peak" ]]; then
                    peak=$(($(cat "$CG/memory.peak") / 1048576))
                    echo "📊 cgroup memory peak: ${peak}MB"
                fi
                high_ev=$(grep "^high " "$CG/memory.events" 2>/dev/null | awk "{print \$2}")
                [[ ${high_ev:-0} -gt 0 ]] && echo "⚡ memory.high reclaim events: $high_ev"
                exit $rc
            ' -- "${files[@]}" || exit_code=$?
    else
        # fallback: ulimit
        (ulimit -v 2097152; python3 -m pytest "${files[@]}" $PYTEST_ARGS) || exit_code=$?
    fi

    return $exit_code
}

# --all mode: collect all test files and split into batches of MAX_FILES
# Groups are run separately by rootdir to avoid conftest.py namespace conflicts:
#   Group 1: tests/ + mcp_server/  → rootdir = REPO_ROOT
#   Group 2: deployer/tests/       → rootdir = REPO_ROOT/deployer  (separate pytest run)
if [[ "${1:-}" == "--all" ]]; then
    cd "$REPO_ROOT"

    # Group 1: src tests (tests/ + mcp_server/) — run from REPO_ROOT
    mapfile -t src_files < <(find tests/ mcp_server/ -name 'test_*.py' 2>/dev/null | sort -u)

    # Group 2: deployer tests — collected as relative paths under deployer/
    mapfile -t deployer_files < <(find deployer/tests/ -name 'test_*.py' 2>/dev/null | sort -u)

    total=$(( ${#src_files[@]} + ${#deployer_files[@]} ))
    echo -e "${GREEN}Found ${total} test files total: ${#src_files[@]} src, ${#deployer_files[@]} deployer. Batches of ${MAX_FILES}...${NC}"

    # Helper: run a batch loop over an array of files using a given CWD
    run_batch_group() {
        local group_cwd="$1"
        shift
        local group_files=("$@")
        local group_failed=0
        local batch_num=0

        # Convert absolute paths to relative for the given CWD when needed
        local rel_files=()
        for f in "${group_files[@]}"; do
            rel_files+=("${f#${group_cwd}/}")
        done

        for ((i=0; i<${#rel_files[@]}; i+=MAX_FILES)); do
            BATCH=("${rel_files[@]:i:MAX_FILES}")
            batch_num=$(( batch_num + 1 ))
            echo -e "\n${YELLOW}Batch ${batch_num} [$(basename $group_cwd)]: ${BATCH[*]}${NC}"
            (
                cd "$group_cwd"
                EXIT_CODE=0
                run_pytest_safe "${BATCH[@]}" || EXIT_CODE=$?
                if [ $EXIT_CODE -eq 137 ]; then
                    echo -e "${RED}⚠️  OOM detected (exit 137) in batch, retrying with half batch size...${NC}"
                    HALF=$(( ${#BATCH[@]} / 2 ))
                    if [ $HALF -eq 0 ]; then
                        echo -e "${RED}❌ Single file OOM — cannot reduce further: ${BATCH[*]}${NC}"
                        exit 1
                    fi
                    BATCH_A=("${BATCH[@]:0:$HALF}")
                    BATCH_B=("${BATCH[@]:$HALF}")
                    echo -e "${BLUE}  Sub-batch A: ${BATCH_A[*]}${NC}"
                    run_pytest_safe "${BATCH_A[@]}" || { echo -e "${RED}❌ Sub-batch A failed${NC}"; exit 1; }
                    echo -e "${BLUE}  Sub-batch B: ${BATCH_B[*]}${NC}"
                    run_pytest_safe "${BATCH_B[@]}" || { echo -e "${RED}❌ Sub-batch B failed${NC}"; exit 1; }
                elif [ $EXIT_CODE -ne 0 ]; then
                    exit 1
                fi
            ) || group_failed=1
        done
        return $group_failed
    }

    failed=0

    # --- Group 1: src tests (tests/ + mcp_server/) ---
    if [[ ${#src_files[@]} -gt 0 ]]; then
        echo -e "\n${BLUE}=== Group 1: src tests (${#src_files[@]} files) ===${NC}"
        run_batch_group "$REPO_ROOT" "${src_files[@]}" || failed=1
    fi

    # --- Group 2: deployer tests (deployer/tests/) ---
    # Run from deployer/ so rootdir is deployer/ — avoids tests/ package namespace conflict
    if [[ ${#deployer_files[@]} -gt 0 ]]; then
        echo -e "\n${BLUE}=== Group 2: deployer tests (${#deployer_files[@]} files) ===${NC}"
        # Strip deployer/ prefix for relative paths under deployer/
        local_deployer_files=()
        for f in "${deployer_files[@]}"; do
            local_deployer_files+=("${f#deployer/}")
        done
        run_batch_group "$REPO_ROOT/deployer" "${local_deployer_files[@]}" || failed=1
    fi

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
