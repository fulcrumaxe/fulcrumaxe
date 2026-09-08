#!/usr/bin/env bash
# tests/test_preflight_changed_files.sh — regression test for D#2394:
# get_changed_python_files silently reporting zero files on a large diff.
#
# Root cause: get_changed_python_files used to capture the full changed-file
# list into a local shell variable (`out=$(get_changed_files)`), then pipe
# that variable through `echo "$out" | grep ... || true`. Past ~128KiB the
# variable made every external command subsequently run in that shell fail
# with E2BIG; grep failing was swallowed by `|| true` and read as "no Python
# files changed" instead of "could not determine". Measured on PR #2393:
# 715 files changed, reported as 0.
#
# The acceptance criteria in D#2394 ask for behaviours, not greps, so these
# tests override get_changed_files() with a synthetic large payload rather
# than shelling out to build an actual multi-thousand-file git diff.
#
# HARD RULE: do NOT call claude, _start_loop_run, or trigger /loop.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
t_ok()   { echo "  [OK] $1";   ((PASS++)) || true; }
t_fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

echo "=== test_preflight_changed_files ==="

# preflight-common.sh's header says callers must set these before sourcing.
# get_changed_files/get_changed_python_files don't use them, but source it
# under the same contract as every real caller.
START_TS=$SECONDS
CHECKS_RUN=0
CURRENT_SLUG=""
# shellcheck source=scripts/lib/preflight-common.sh
source "$REPO_ROOT/scripts/lib/preflight-common.sh"

# ── AC1 + AC2 groundwork: build a synthetic >128KiB path list ───────────────
# Includes one modified .py file so we can assert it survives.
build_large_list() {
    local i
    for i in $(seq 1 3000); do
        printf 'some/fairly/deeply/nested/fake/path/dir_%05d/file_%05d.txt\n' "$i" "$i"
    done
    printf 'backend/_synthetic_target_2394.py\n'
}

LIST_BYTES=$(build_large_list | wc -c)
echo ""
echo "--- groundwork: synthetic list size ---"
if [ "$LIST_BYTES" -gt 131072 ]; then
    t_ok "synthetic list is ${LIST_BYTES} bytes (>128KiB) — matches the D#2394 trigger size"
else
    t_fail "synthetic list is only ${LIST_BYTES} bytes — too small to reproduce the bug (need >131072)"
fi

# ── AC1: get_changed_python_files returns the modified .py file ─────────────
echo ""
echo "--- AC1: modified .py file survives a >128KiB diff ---"

get_changed_files() { build_large_list; }

FILES=$(get_changed_python_files)
RC=$?

if [ "$RC" -eq 0 ]; then
    t_ok "get_changed_python_files returns 0 on a >128KiB diff"
else
    t_fail "get_changed_python_files returned $RC on a >128KiB diff"
fi

if printf '%s\n' "$FILES" | grep -qx 'backend/_synthetic_target_2394.py'; then
    t_ok "modified .py file is present in the result (not silently dropped)"
else
    t_fail "modified .py file MISSING from result: [$FILES]"
fi

# ── AC2: the calling shell's external commands still work afterward ─────────
echo ""
echo "--- AC2: external commands in the calling shell still work after the call ---"

if echo probe | grep -q probe; then
    t_ok "grep still works in the calling shell after a >128KiB get_changed_python_files call"
else
    t_fail "grep failed in the calling shell after a >128KiB get_changed_python_files call"
fi

if echo probe | sed 's/probe/ok/' >/dev/null 2>&1; then
    t_ok "sed still works in the calling shell after a >128KiB get_changed_python_files call"
else
    t_fail "sed failed in the calling shell after a >128KiB get_changed_python_files call"
fi

if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    t_ok "git still works in the calling shell after a >128KiB get_changed_python_files call"
else
    t_fail "git failed in the calling shell after a >128KiB get_changed_python_files call"
fi

# ── AC3: a real grep error is distinguishable from "no matches" ─────────────
echo ""
echo "--- AC3: grep error (exit >=2) does not report success ---"

get_changed_files() { printf 'unrelated/file.txt\n'; }

# Force grep itself to fail the way a real error would (exit 2), via a
# shell-function override — the pattern get_changed_python_files uses is
# fixed and not attacker/caller-controlled, so this is the only way to
# exercise the exit-2 path without patching grep itself.
grep() { return 2; }
FILES3=$(get_changed_python_files)
RC3=$?
unset -f grep

if [ "$RC3" -ne 0 ]; then
    t_ok "get_changed_python_files returns non-zero when grep errors (exit 2)"
else
    t_fail "get_changed_python_files returned 0 (success) despite a grep error — indistinguishable from 'no matches'"
fi

# Sanity: ordinary "no .py files changed" (real grep, exit 1) still returns
# 0 with empty output — this must not regress while fixing the exit-2 case.
get_changed_files() { printf 'unrelated/file.txt\n'; }
FILES3B=$(get_changed_python_files)
RC3B=$?
if [ "$RC3B" -eq 0 ] && [ -z "$FILES3B" ]; then
    t_ok "'no matches' (real grep, exit 1) still returns 0 with empty output"
else
    t_fail "'no matches' case broke: rc=$RC3B files=[$FILES3B]"
fi

# ── AC4: small-diff behaviour is unchanged ───────────────────────────────────
echo ""
echo "--- AC4: small-diff behaviour is unchanged ---"

get_changed_files() { printf 'backend/foo.py\nbackend/bar.txt\nfrontend/app.tsx\n'; }
FILES4=$(get_changed_python_files)
RC4=$?
if [ "$RC4" -eq 0 ] && [ "$FILES4" = "backend/foo.py" ]; then
    t_ok "small diff still returns exactly the changed .py file"
else
    t_fail "small diff behaviour changed: rc=$RC4 files=[$FILES4]"
fi

# ── Regression guard: an unresolvable diff base still propagates as failure ─
echo ""
echo "--- diff-base-unresolvable still propagates as a failure (unchanged) ---"

get_changed_files() { return 1; }
get_changed_python_files >/dev/null 2>&1
RC5=$?
if [ "$RC5" -ne 0 ]; then
    t_ok "unresolvable diff base still propagates as a get_changed_python_files failure"
else
    t_fail "unresolvable diff base was swallowed — get_changed_python_files returned 0"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
