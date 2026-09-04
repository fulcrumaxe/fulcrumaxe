#!/usr/bin/env bash
# scripts/lib/preflight-common.sh — shared gates and helpers for preflight-fast.sh + preflight-full.sh
#
# Source this file, then call setup_process_group and the check_* functions.
# All callers must set REPO_ROOT, START_TS, CHECKS_RUN, CURRENT_SLUG before sourcing.

# ── Process-group cleanup trap ────────────────────────────────────────────────
# Ensures the entire preflight process group is reaped when the parent exits.
# Prevents pytest workers from re-parenting to init when the subagent dies.
#
# Design: kill -TERM -$$ sends SIGTERM to the whole process group. Since this
# script IS the group leader (set -m), we use `ps` to find child pids in the
# same group and kill only those — never the leader itself. This avoids the
# self-SIGTERM → exit 143 loop that a naive `kill -TERM -$$` causes.
#
# Coupling to watch: this trap only runs if the script reaches EXIT. A bare
# `timeout N` around a child that ignores SIGTERM does not bound anything —
# GNU timeout sends TERM once and then waits, so the script (and this trap)
# never gets control back. That is why every `timeout` call bounding a test
# or network step in this repo needs `--kill-after=<grace>`: without it, this
# reaper is correct but unreachable (D#1800).
setup_process_group() {
    set -m  # job control on — makes $$ the process-group leader
    trap '
        _EC=$?
        trap - EXIT INT TERM
        # Kill all processes in our process group EXCEPT the group leader ($$)
        PGID=$$
        CHILDREN=$(ps -eo pid,pgid --no-headers 2>/dev/null \
            | awk -v pg="$PGID" -v me="$$" '"'"'$2==pg && $1!=me {print $1}'"'"' \
            || true)
        if [ -n "$CHILDREN" ]; then
            kill -TERM $CHILDREN 2>/dev/null || true
            # Give them a moment, then SIGKILL stragglers
            sleep 0.3 2>/dev/null || true
            kill -KILL $CHILDREN 2>/dev/null || true
        fi
        exit $_EC
    ' EXIT INT TERM
}

# ── Output helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

# fail <check-name> [detail] [exit_code]
fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    if [ -n "${2:-}" ]; then
        echo "$2" | sed 's/^/    /'
    fi
    FAILED=1
    local _exit_code="${3:-1}"
    echo "PRESUM: fail step=${CURRENT_SLUG:-unknown} exit=${_exit_code} checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
    exit 1
}

# self_skip <check-name> <reason> — a gate calls this instead of pass()/fail()
# when it WANTED to verify something but couldn't, because its target script
# or tool is missing in a tree shape where it should be present (D#2271,
# D#2289). This is deliberately distinct from an ordinary "[SKIP]" — a legit
# skip is a positive decision (this diff/tree doesn't need this check, e.g.
# no Python files touched, or an open-source export tree that never ships
# tests/) and a gate reports that itself, directly, without calling this
# helper. self_skip records "no verification signal produced this run" by
# side effect (a counter and a name list used only for the human-readable
# summary, never for pass/fail logic) so run_always_gates() can assert that
# every gate that ran actually produced a verdict, without maintaining a
# list of gate names anywhere to check against. See check_spawn_guard_lint's
# retirement (archive/preflight-spawn-guard-lint-2026-09-03/) for the defect
# this exists to catch: it warned-and-skipped, silently, for ~4 months.
self_skip() {
    local name="$1" reason="$2"
    echo "[WARN] $name — $reason (self-skip: wanted to verify, could not)"
    SELF_SKIPPED_COUNT=$((SELF_SKIPPED_COUNT + 1))
    SELF_SKIPPED_NAMES+=("$name")
}

# ── Diff helpers ──────────────────────────────────────────────────────────────
# Both resolvers used to end in `|| true`, so an unresolvable diff base (e.g.
# a shallow CI checkout that never fetched origin/main — the default
# actions/checkout@v4 fetch-depth is 1) silently read as "zero files
# changed" rather than as an error. Every diff-scoped gate downstream then
# reported a legitimate-looking PASS/SKIP having verified nothing at all
# (AC-11, D#2289). Both functions now fail loudly (return 1, with a message
# on stderr) when NEITHER origin/main NOR HEAD~1 resolves, instead of
# swallowing the failure. A real "no changes in this diff" (git diff exits 0
# with empty output) is unaffected — this only catches git itself refusing
# to resolve either ref.
get_changed_files() {
    local out
    if out=$(git diff --name-only "origin/main" 2>/dev/null); then
        printf '%s\n' "$out"
        return 0
    fi
    if out=$(git diff --name-only "HEAD~1" 2>/dev/null); then
        printf '%s\n' "$out"
        return 0
    fi
    echo "get_changed_files: could not resolve a diff base — neither 'origin/main' nor 'HEAD~1' exists in this checkout. Refusing to report an empty diff as \"no files changed\". In CI this usually means the checkout needs a deeper fetch (fetch-depth) or an explicit fetch of the base ref." >&2
    return 1
}

get_changed_python_files() {
    local out
    out=$(get_changed_files) || return 1
    echo "$out" | grep '\.py$' || true
}

# ── Always-run gates ──────────────────────────────────────────────────────────

# Gate: Python syntax check on changed .py files
check_python_syntax() {
    CURRENT_CHECK="Python Syntax Validation"
    CURRENT_SLUG="python-syntax"
    ((CHECKS_RUN++)) || true
    local files
    if ! files=$(get_changed_python_files); then
        fail "$CURRENT_CHECK" "could not determine changed Python files — diff base unresolvable"
        return 1
    fi

    if [ -z "$files" ]; then
        pass "$CURRENT_CHECK (no Python files changed)"
        return 0
    fi

    local errors=""
    for file in $files; do
        if [ -f "$file" ]; then
            local out
            out=$(python3 -c "import ast; ast.parse(open('$file').read())" 2>&1) || \
                errors="${errors}\n  Syntax error in: $file\n  $out"
        fi
    done

    if [ -n "$errors" ]; then
        fail "$CURRENT_CHECK" "$errors"
    else
        pass "$CURRENT_CHECK"
    fi
}

# Gate: Python import resolution on changed .py files
check_python_imports() {
    CURRENT_CHECK="Python Import Resolution"
    CURRENT_SLUG="python-imports"
    ((CHECKS_RUN++)) || true
    local files
    if ! files=$(get_changed_python_files); then
        fail "$CURRENT_CHECK" "could not determine changed Python files — diff base unresolvable"
        return 1
    fi

    if [ -z "$files" ]; then
        pass "$CURRENT_CHECK (no Python files changed)"
        return 0
    fi

    local errors=""

    for file in $files; do
        if [ -f "$file" ]; then
            local module_path
            module_path=$(echo "$file" | sed 's/\//./g' | sed 's/\.py$//')
            if ! python3 -c "import sys; __import__('$module_path')" 2>/dev/null; then
                if ! python3 -m py_compile "$file" 2>/dev/null; then
                    errors="${errors}\n  Import error in: $file"
                fi
            fi
        fi
    done

    if [ -n "$errors" ]; then
        fail "$CURRENT_CHECK" "$errors"
    else
        pass "$CURRENT_CHECK"
    fi
}

# check_spawn_guard_lint was retired 2026-09-03 (D#2271/D#2289 AC-10): its
# target, dashboard/e2e/lint-spawn-guard.mjs, was archived 2026-05-11 along
# with the whole Puppeteer E2E suite it guarded, and the gate had been
# silently WARN-and-skip-passing ever since — exactly the "gate declined to
# gate, nobody noticed" defect this Spec exists to close. See
# archive/preflight-spawn-guard-lint-2026-09-03/README.md for the function
# body and the restore condition (only alongside restoring the E2E suite
# itself, which needs its own explicit decision per D#578).

# Gate: No hardcoded checkout paths (always — scans the whole tracked tree,
# not just the diff, since a stale/dangling allowlist entry can go bad
# without the triggering PR touching either file)
#
# The script and its fixture are deliberately excluded from the open-source
# export (open-source/lib/rsync-excludes.sh) — most of the allowlist's
# entries live outside MANIFEST.md's PATHS_START (tests/**, wiki/**, etc.),
# so an exported tree would report every one of them as dangling, a
# permanently-red gate for every export consumer and coldstart target
# (D#1877 review round 3). tests/ absence is used as the tree-shape probe
# for "this is that export tree" (open-source/lib/rsync-excludes.sh notes
# this script's whole allowlist lives outside tests/**, wiki/**, etc. — the
# same set that never ships in an export) — a real internal tree with
# tests/ present but this script missing is rot, not export shape, and is
# reported via self_skip() rather than a silent warn-skip (D#2271/D#2289
# AC-9; this is the same gate shape check_spawn_guard_lint's four months of
# silence should have been caught by, before it was retired above).
check_no_hardcoded_checkout_paths() {
    CURRENT_CHECK="No Hardcoded Checkout Paths"
    CURRENT_SLUG="no-hardcoded-checkout-paths"
    ((CHECKS_RUN++)) || true

    local checkout_paths_script="$REPO_ROOT/scripts/check-no-hardcoded-checkout-paths.sh"
    if [ ! -f "$checkout_paths_script" ]; then
        if [ ! -d "$REPO_ROOT/tests" ]; then
            echo "[SKIP] $CURRENT_CHECK (no tests/ directory — open-source export; this script and its fixture are excluded from that export)"
        else
            self_skip "$CURRENT_CHECK" "check-no-hardcoded-checkout-paths.sh not found, but tests/ is present"
        fi
        return 0
    fi

    local output
    if output=$(bash "$checkout_paths_script" 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Gate: No fixed /tmp paths in tests/ (D#2254) — a bash suite writing to a
# shared fixed /tmp name races every concurrent invocation of that suite.
#
# Skipped, not failed, when tests/ is absent: the check's allowlist
# (scripts/fixtures/allowed_fixed_tmp_literals.txt) names tests/*.sh paths
# by design, and tests/ does not ship in the open-source export
# (open-source/MANIFEST.md's PATHS_START) — every allowlist entry would
# read as dangling in an exported tree for a reason that has nothing to do
# with a real regression there. The tree-shape check runs FIRST: only once
# tests/ is confirmed present does a missing check script count as
# self_skip (rot) rather than legitimate export shape.
check_no_fixed_tmp_paths_in_tests() {
    CURRENT_CHECK="No Fixed /tmp Paths in Tests"
    CURRENT_SLUG="no-fixed-tmp-paths-in-tests"
    ((CHECKS_RUN++)) || true

    if [ ! -d "$REPO_ROOT/tests" ]; then
        echo "[SKIP] $CURRENT_CHECK (no tests/ directory — open-source export)"
        return 0
    fi

    local fixed_tmp_script="$REPO_ROOT/scripts/check-tests-fixed-tmp-paths.sh"
    if [ ! -f "$fixed_tmp_script" ]; then
        self_skip "$CURRENT_CHECK" "check-tests-fixed-tmp-paths.sh not found, but tests/ is present"
        return 0
    fi

    local output
    if output=$(bash "$fixed_tmp_script" 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Gate: No live in-repo .autonomous-team/ state paths in tests/ (D#2267) —
# a bash suite reading or writing the LIVE, checked-out .autonomous-team/
# tree (agent-feed.jsonl, hook-events/, stats/, ...) races every running
# agent's own writes to the same files, so its pass/fail count depends on
# host activity rather than the diff under review.
#
# Skipped, not failed, when tests/ is absent, for the exact same reason as
# check_no_fixed_tmp_paths_in_tests above: this check's allowlist
# (scripts/fixtures/allowed_live_state_literals.txt) names tests/*.sh paths
# by design, and tests/ does not ship in the open-source export. The
# tree-shape check runs FIRST: only once tests/ is confirmed present does a
# missing check script count as self_skip (rot) rather than legitimate
# export shape.
check_no_live_state_paths_in_tests() {
    CURRENT_CHECK="No Live In-Repo State Paths in Tests"
    CURRENT_SLUG="no-live-state-paths-in-tests"
    ((CHECKS_RUN++)) || true

    if [ ! -d "$REPO_ROOT/tests" ]; then
        echo "[SKIP] $CURRENT_CHECK (no tests/ directory — open-source export)"
        return 0
    fi

    local live_state_script="$REPO_ROOT/scripts/check-tests-live-state-paths.sh"
    if [ ! -f "$live_state_script" ]; then
        self_skip "$CURRENT_CHECK" "check-tests-live-state-paths.sh not found, but tests/ is present"
        return 0
    fi

    local output
    if output=$(bash "$live_state_script" 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Gate: every tracked `bun test` invocation of the ts-backend suite routes
# through the configured `bun run test` script (D#2276) — bun's per-test
# default timeout is 5000ms, and nothing else in the repo overrides it
# except that one script.
#
# Deliberately HARD-FAILS (not the tmp-paths precedent's [WARN]-and-skip)
# when the check script is missing while ts-backend/package.json is
# present: a missing timeout guard is not equivalent to "nothing to check"
# the way a missing tests/ directory is — it means the invariant this gate
# exists for is silently unenforced. Skipping is only correct when the
# ts-backend workspace itself is absent (the open-source export case).
check_bun_test_timeout() {
    CURRENT_CHECK="Bun Test Timeout Governs (D#2276)"
    CURRENT_SLUG="bun-test-timeout-governs"
    ((CHECKS_RUN++)) || true

    if [ ! -f "$REPO_ROOT/ts-backend/package.json" ]; then
        echo "[SKIP] $CURRENT_CHECK (no ts-backend/package.json — open-source export)"
        return 0
    fi

    local timeout_script="$REPO_ROOT/scripts/check-bun-test-timeout.sh"
    if [ ! -f "$timeout_script" ]; then
        fail "$CURRENT_CHECK" "check-bun-test-timeout.sh not found but ts-backend/package.json is present — hard failure, not a skip (D#2276)"
        return 1
    fi

    local output
    if output=$(bash "$timeout_script" 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Gate: Subsystems index completeness (only when backend/*.py changed)
check_subsystems_index() {
    CURRENT_CHECK="Subsystems Index Completeness"
    CURRENT_SLUG="subsystems-index"
    ((CHECKS_RUN++)) || true

    local changed_files
    if ! changed_files=$(get_changed_files); then
        fail "$CURRENT_CHECK" "could not determine changed files — diff base unresolvable"
        return 1
    fi

    if ! echo "$changed_files" | grep -qE '^backend/.*\.py$'; then
        echo "[SKIP] $CURRENT_CHECK (no backend/*.py files changed)"
        return 0
    fi

    local output
    if output=$(bash "$REPO_ROOT/scripts/check-subsystems-index.sh" 2>&1); then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Gate: Prompt drift detector (only when CLAUDE.md or spawn_templates touched)
check_prompt_drift() {
    CURRENT_CHECK="Prompt Drift Check"
    CURRENT_SLUG="prompt-drift"
    ((CHECKS_RUN++)) || true

    local changed_files
    if ! changed_files=$(get_changed_files); then
        fail "$CURRENT_CHECK" "could not determine changed files — diff base unresolvable"
        return 1
    fi

    if ! echo "$changed_files" | grep -qE '^(CLAUDE\.md|backend/spawn_templates\.py|backend/spawn_templates/)'; then
        echo "[SKIP] $CURRENT_CHECK (no CLAUDE.md or spawn_templates files changed)"
        return 0
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        self_skip "$CURRENT_CHECK" "python3 not found"
        return 0
    fi

    local output exit_code
    output=$(bash "$REPO_ROOT/scripts/check-prompt-drift.sh" 2>&1) && exit_code=$? || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "$CURRENT_CHECK"
    else
        fail "$CURRENT_CHECK" "$output"
    fi
}

# Run all always-on gates (call this from both fast and full scripts, and
# from the CI preflight job). Asserts EXECUTION, not identity (AC-9,
# D#2271/D#2289): every gate above either verifies something and calls
# pass()/fail(), makes a positive tree-shape/diff-scope decision that it
# doesn't apply here ("[SKIP] ..."), or — if its target/tool is missing in a
# shape where it should exist — calls self_skip(), which this function
# treats as a hard failure once all gates have run. There is no list of gate
# names checked here: adding a gate that self_skip()s, or removing one that
# used to run, changes SELF_SKIPPED_COUNT/CHECKS_RUN by construction, with
# no registration step and nothing here to edit.
run_always_gates() {
    SELF_SKIPPED_COUNT=0
    SELF_SKIPPED_NAMES=()

    check_python_syntax
    check_python_imports
    check_no_hardcoded_checkout_paths
    check_no_fixed_tmp_paths_in_tests
    check_no_live_state_paths_in_tests
    check_bun_test_timeout
    check_subsystems_index
    check_prompt_drift

    local ran_for_real=$((CHECKS_RUN - SELF_SKIPPED_COUNT))
    echo "[GATES] ${ran_for_real}/${CHECKS_RUN} always-on gates produced a verdict; ${SELF_SKIPPED_COUNT} self-skipped"
    if [ "$SELF_SKIPPED_COUNT" -gt 0 ]; then
        CURRENT_SLUG="self-skip"
        fail "Always-on gates" "${SELF_SKIPPED_COUNT} gate(s) self-skipped instead of verifying: ${SELF_SKIPPED_NAMES[*]}. A gate that cannot verify must not read as green — restore its target or retire the gate (see check_spawn_guard_lint's retirement above for the archive pattern)."
    fi
}
