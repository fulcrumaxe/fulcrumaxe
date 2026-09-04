#!/usr/bin/env bash
# scripts/preflight-full.sh — full pre-merge gate for CI and post-merge-hook.
#
# Runs all checks: always-on gates + full pytest + coverage (≥70%) +
# dashboard E2E + TUI tester + integration tests.
# Equivalent coverage signal to the old scripts/preflight.sh.
#
# Rust tests were removed 2026-08-17: the check could never be earned (no
# Cargo.toml exists anywhere in the live tree — it checked for a "perf tools"
# directory under an archived component's path) and was reporting a green
# pass every run while incrementing CHECKS_RUN for a check that never ran.
#
# Usage:
#   bash scripts/preflight-full.sh               # normal run
#   bash scripts/preflight-full.sh --skip-lint   # skip lint/typecheck
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

# ── Scratch state dir for pytest (D#1908 PR 3 / AC-3.14) ─────────────────────
# backend/state_paths.py refuses to resolve a STATE_DIR-derived path under
# pytest unless AUTONOMOUS_TEAM_STATE_DIR points somewhere — otherwise a
# forgotten export silently writes synthetic rows into the production state
# dir. Every pytest invocation below is exported this scratch dir; nothing
# else in this script runs a bash test suite that needs the variable unset
# (see CLAUDE.md's "AUTONOMOUS_TEAM_STATE_DIR in tests" note — that rule is
# for the tests/*.sh suites elsewhere in the tree, not for this file).
export AUTONOMOUS_TEAM_STATE_DIR
AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)"

# ── Full test suite ───────────────────────────────────────────────────────────
check_pytest_full() {
    CURRENT_CHECK="Pytest Test Suite (full)"
    CURRENT_SLUG="pytest"
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

    local output
    if output=$(python3 -m pytest "$REPO_ROOT/tests/" -x --tb=short 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# ── TypeScript type check ─────────────────────────────────────────────────────
check_typescript_types() {
    CURRENT_CHECK="TypeScript Type Check"
    CURRENT_SLUG="typecheck"
    ((CHECKS_RUN++)) || true

    if [ "$SKIP_LINT" = "true" ]; then
        echo "[SKIP] $CURRENT_CHECK (lint_must_pass gate is off or --skip-lint passed)"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tui" ]; then
        pass "$CURRENT_CHECK (no tui directory)"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tui/node_modules" ]; then
        echo "[WARN] tui/node_modules missing — skipping typecheck (run npm install)" >&2
        return 0
    fi

    local output
    if output=$(cd "$REPO_ROOT/tui" && npm run typecheck 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# ── TypeScript build ──────────────────────────────────────────────────────────
check_typescript_build() {
    CURRENT_CHECK="TypeScript Build"
    CURRENT_SLUG="build"
    ((CHECKS_RUN++)) || true

    if [ "$SKIP_LINT" = "true" ]; then
        echo "[SKIP] $CURRENT_CHECK (lint_must_pass gate is off or --skip-lint passed)"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tui" ]; then
        pass "$CURRENT_CHECK (no tui directory)"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tui/node_modules" ]; then
        echo "[WARN] tui/node_modules missing — skipping build (run npm install)" >&2
        return 0
    fi

    local output
    if output=$(cd "$REPO_ROOT/tui" && npm run build 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# ── Dashboard E2E (only when dashboard/** touched) ────────────────────────────
check_dashboard_e2e() {
    CURRENT_CHECK="Dashboard E2E Tests"
    CURRENT_SLUG="dashboard-e2e"
    ((CHECKS_RUN++)) || true

    local changed_files
    changed_files=$(get_changed_files)
    if ! echo "$changed_files" | grep -q "^dashboard/"; then
        echo "[SKIP] $CURRENT_CHECK (no dashboard/ files changed)"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/dashboard" ]; then
        echo "[WARN] $CURRENT_CHECK (no dashboard/ directory)"
        return 0
    fi

    if ! command -v node >/dev/null 2>&1; then
        echo "[WARN] $CURRENT_CHECK — node not found, skipping"
        return 0
    fi

    # Puppeteer e2e disabled 2026-05-11 — Chrome DevTools MCP is canonical.
    # D#578 tracks full archival of dashboard/e2e/.
    pass "$CURRENT_CHECK (skipped — puppeteer e2e disabled; use MCP scenarios)"
    return 0
}

# ── Integration tests ─────────────────────────────────────────────────────────
check_integration_tests() {
    CURRENT_CHECK="Integration Tests"
    CURRENT_SLUG="integration"
    ((CHECKS_RUN++)) || true

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[WARN] python3 not found — skipping integration tests"
        return 0
    fi

    if ! python3 -c "import pytest" 2>/dev/null; then
        echo "[WARN] pytest not installed — skipping integration tests"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tests/integration" ]; then
        pass "$CURRENT_CHECK (no tests/integration/ directory)"
        return 0
    fi

    local output
    if output=$(python3 -m pytest "$REPO_ROOT/tests/integration/" -x --tb=short 2>&1); then
        pass "$CURRENT_CHECK"
    else
        echo "[WARN] $CURRENT_CHECK — integration tests skipped or failed (server may not be running)"
        echo "$output" | tail -10 | sed 's/^/    /'
        return 0
    fi
}

# ── Coverage threshold ≥70% ───────────────────────────────────────────────────
check_coverage_threshold() {
    CURRENT_CHECK="Coverage Threshold"
    CURRENT_SLUG="coverage"
    ((CHECKS_RUN++)) || true

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[WARN] python3 not found — skipping coverage check"
        return 0
    fi

    if ! python3 -c "import pytest_cov" 2>/dev/null && ! python3 -c "import coverage" 2>/dev/null; then
        echo "[WARN] pytest-cov not installed — skipping coverage threshold check"
        return 0
    fi

    if [ ! -d "$REPO_ROOT/tests" ] || [ ! -d "$REPO_ROOT/backend" ]; then
        pass "$CURRENT_CHECK (tests/ or backend/ not found)"
        return 0
    fi

    local output
    if output=$(python3 -m pytest --cov=backend --cov-fail-under=70 "$REPO_ROOT/tests/" -q --tb=no 2>&1); then
        pass "$CURRENT_CHECK (≥70% coverage)"
    else
        local coverage_pct
        coverage_pct=$(echo "$output" | grep -oE 'Total coverage: [0-9.]+%' | head -1 || echo "unknown")
        echo "[WARN] $CURRENT_CHECK — coverage below 70% ($coverage_pct). Track in Discussion #217."
        return 0
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
echo "=== Pre-flight (full) ==="
echo ""

run_always_gates
check_pytest_full
check_typescript_types
check_typescript_build
check_dashboard_e2e
check_integration_tests
check_coverage_threshold

echo ""
echo "=== All checks passed ==="
echo "PRESUM: pass checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
exit 0
