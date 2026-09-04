#!/usr/bin/env bash
# scripts/preflight-fast.sh — fast pre-PR gate for executor subagents.
#
# Runs only always-on gates (syntax/imports/interface/spawn-guard/index/drift)
# plus diff-aware pytest via pytest-testmon (pytest-picked fallback).
# Target: <30s on a 1-file diff with a warm testmon DB.
#
# Usage:
#   bash scripts/preflight-fast.sh               # normal run
#   bash scripts/preflight-fast.sh --skip-lint   # skip lint/typecheck (gate override)
#
# PRESUM output (last line on every exit path):
#   PRESUM: pass checks=N duration=Ns
#   PRESUM: fail step=<slug> exit=<code> checks=N duration=Ns

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source shared helpers (trap, always-run gates)
# shellcheck source=scripts/lib/preflight-common.sh
source "$SCRIPT_DIR/lib/preflight-common.sh"

# ── Process-group trap (kills all children on exit) ───────────────────────────
setup_process_group

# ── PRESUM tracking ───────────────────────────────────────────────────────────
START_TS=$SECONDS
CHECKS_RUN=0
CURRENT_SLUG=""
FAILED=0

# ── Flags ─────────────────────────────────────────────────────────────────────
SKIP_LINT=false
for arg in "$@"; do
    case "$arg" in
        --skip-lint) SKIP_LINT=true ;;
    esac
done

# ── Control plane: lint_must_pass gate ────────────────────────────────────────
LINT_GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.lint_must_pass 2>/dev/null | tr -d '"' || echo "true")
if [ "$LINT_GATE" = "false" ]; then
    echo "[INFO] lint_must_pass gate is off — skipping lint/typecheck"
    SKIP_LINT=true
fi

# ── Control plane: pr_size_max_lines ─────────────────────────────────────────
MAX_LINES=$(python3 "$REPO_ROOT/backend/control_plane.py" get policies.executor.pr_size_max_lines 2>/dev/null | tr -d '"' || echo "2000")
MAX_LINES="${MAX_LINES:-2000}"
DIFF_LINES=$(git -C "$REPO_ROOT" diff --stat HEAD 2>/dev/null | tail -1 | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo "0")
DIFF_LINES="${DIFF_LINES:-0}"
if [ "$DIFF_LINES" -gt "$MAX_LINES" ] 2>/dev/null; then
    echo "[FAIL] PR diff ($DIFF_LINES insertion lines) exceeds pr_size_max_lines ($MAX_LINES)."
    echo "       Split the work into smaller PRs and re-run preflight."
    echo "PRESUM: fail step=pr-size exit=1 checks=0 duration=$((SECONDS-START_TS))s"
    exit 1
fi
[ "$DIFF_LINES" -gt 0 ] && echo "[INFO] PR size: $DIFF_LINES insertion lines (limit: $MAX_LINES)"

# ── Diff-aware pytest (testmon primary, picked fallback) ──────────────────────
check_pytest_fast() {
    CURRENT_CHECK="Pytest (diff-aware)"
    CURRENT_SLUG="pytest-fast"
    ((CHECKS_RUN++)) || true

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[WARN] python3 not found — skipping pytest"
        return 0
    fi

    if ! python3 -c "import pytest" 2>/dev/null; then
        echo "[WARN] pytest not installed — skipping test suite"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tests" ]; then
        pass "$CURRENT_CHECK (no tests/ directory)"
        return 0
    fi

    # Per-worktree testmon DB to avoid cross-worktree corruption
    local branch worktree_name
    branch=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    worktree_name="${PWD##*/}"
    local TESTMON_DB="$REPO_ROOT/.testmondata-${branch}-${worktree_name}"

    local output exit_code

    # Try testmon if installed
    if python3 -c "import testmon" 2>/dev/null; then
        # If DB is missing, seed it asynchronously on first run, use picked for this run
        if [ ! -f "$TESTMON_DB" ]; then
            echo "[INFO] testmon DB missing — seeding in background, using pytest-picked for this run"
            if python3 -c "import pytest_picked" 2>/dev/null; then
                output=$(timeout --kill-after=5s 60 python3 -m pytest tests/ --picked --mode=branch -x --tb=short 2>&1) && exit_code=$? || exit_code=$?
            else
                echo "[INFO] pytest-picked not installed — running touched-dir subset"
                _run_touched_dir_subset
                return $?
            fi
            # Note: testmon DB will be seeded on next non-timeout full run
            # (we don't background-seed here to avoid orphan processes)
        else
            # Testmon DB exists — run diff-aware selection
            output=$(timeout --kill-after=5s 60 python3 -m pytest tests/ --testmon --testmon-datafile="$TESTMON_DB" -x --tb=short 2>&1) && exit_code=$? || exit_code=$?

            # On testmon failure/empty selection, fall back to picked
            if [ "$exit_code" -ne 0 ] && echo "$output" | grep -qiE "no tests|empty|corrupt|unable"; then
                echo "[WARN] testmon returned empty/corrupt — falling back to pytest-picked"
                rm -f "$TESTMON_DB"
                if python3 -c "import pytest_picked" 2>/dev/null; then
                    output=$(timeout --kill-after=5s 60 python3 -m pytest tests/ --picked --mode=branch -x --tb=short 2>&1) && exit_code=$? || exit_code=$?
                else
                    _run_touched_dir_subset
                    return $?
                fi
            fi
        fi
    elif python3 -c "import pytest_picked" 2>/dev/null; then
        echo "[INFO] testmon not installed — using pytest-picked"
        output=$(timeout --kill-after=5s 60 python3 -m pytest tests/ --picked --mode=branch -x --tb=short 2>&1) && exit_code=$? || exit_code=$?
    else
        echo "[INFO] neither testmon nor picked installed — running touched-dir subset"
        _run_touched_dir_subset
        return $?
    fi

    if [ "${exit_code:-1}" -eq 124 ]; then
        # timeout hit — fall back to touched-dir subset
        echo "[WARN] pytest-fast: time-box (60s) exceeded — falling back to touched-dir subset"
        _run_touched_dir_subset
        return $?
    fi

    if [ "${exit_code:-1}" -eq 0 ] || [ "${exit_code:-1}" -eq 5 ]; then
        # exit 5 = no tests collected (diff-aware correctly found nothing to run)
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Fallback: run only test files in directories touched by this diff
_run_touched_dir_subset() {
    CURRENT_CHECK="Pytest (touched-dir fallback)"
    CURRENT_SLUG="pytest-fast"

    local changed_files
    changed_files=$(git diff --name-only "origin/main" 2>/dev/null || git diff --name-only HEAD~1 2>/dev/null || true)

    # Collect test dirs matching touched source dirs
    local test_args=()
    while IFS= read -r f; do
        local dir
        dir=$(dirname "$f")
        local test_dir="$REPO_ROOT/tests/${dir##*/}"
        if [ -d "$test_dir" ]; then
            test_args+=("$test_dir")
        fi
    done <<< "$changed_files"

    # Always include the root tests/ dir (fast, small)
    if [ "${#test_args[@]}" -eq 0 ]; then
        test_args=("$REPO_ROOT/tests/")
    fi

    local output exit_code
    output=$(timeout --kill-after=5s 60 python3 -m pytest "${test_args[@]}" -x --tb=short 2>&1) && exit_code=$? || exit_code=$?

    if [ "${exit_code:-1}" -eq 124 ]; then
        echo "[FAIL] preflight-fast: time-box exceeded in touched-dir fallback (>60s)"
        echo "PRESUM: fail step=pytest-fast exit=1 checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
        exit 1
    fi

    if [ "${exit_code:-1}" -eq 0 ] || [ "${exit_code:-1}" -eq 5 ]; then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
echo "=== Pre-flight (fast) ==="
echo ""

run_always_gates
check_pytest_fast

echo ""
echo "=== All checks passed ==="
echo "PRESUM: pass checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
exit 0
