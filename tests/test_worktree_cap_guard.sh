#!/usr/bin/env bash
# tests/test_worktree_cap_guard.sh — fixture suite for
# scripts/lib/worktree-disk-guard.sh (D#2097).
#
# D#2097 removed the old worktree-cap check in scripts/pre-spawn-check.sh: it
# compared a cumulative, monotonically-growing on-disk directory count
# against the literal 8, which was actually the fleet concurrency cap
# (backend/fleet/concurrency.py's DEFAULT_FLEET_CAP) copied onto an unrelated
# disk metric, and it never blocked a spawn in its life. This suite covers
# what replaced it: scripts/lib/worktree-disk-guard.sh, a free-disk warning
# that is never allowed to fail a spawn.
#
# Prior art for a sourceable-and-tested spawn-path guard:
# scripts/lib/spec-ready-gate.sh + tests/test_spec_ready_gate.sh. This suite
# follows that shape -- source the real function, exercise it directly --
# rather than driving the guard indirectly through pre-spawn-check.sh.
#
# The guard's own free-space probe (_worktree_disk_guard_probe_free_kb) is an
# ordinary shell function, redefinable after sourcing, so the four required
# cases below stub it instead of touching a real filesystem. Case 0 is not
# one of those four -- it is a sanity check that the *real*, unstubbed probe
# still returns a plausible verdict against a real directory, so this suite
# is not testing the stub scaffolding alone.
#
# Usage: bash tests/test_worktree_cap_guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=tests/lib/blackboard-fixture.sh
source "$SCRIPT_DIR/lib/blackboard-fixture.sh"
# Called directly, not via command substitution -- see blackboard-fixture.sh's
# own header for why `x=$(blackboard_scratch_state_dir)` would lose the export.
blackboard_scratch_state_dir || { echo "FATAL: could not create scratch state dir" >&2; exit 1; }
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
SCRATCH_FIXTURE_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$SCRATCH_STATE_DIR" "$SCRATCH_FIXTURE_DIR"
}
trap cleanup EXIT

# shellcheck source=scripts/lib/worktree-disk-guard.sh
source "$REPO_ROOT/scripts/lib/worktree-disk-guard.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

FLOOR_GB=10
FLOOR_KB=$((FLOOR_GB * 1024 * 1024))

# _stub_probe_ok <kb> — redefine the probe to report a fixed free-KB value.
# _stub_probe_fail    — redefine the probe to simulate a failed stat.
# Both replace _worktree_disk_guard_probe_free_kb with an ordinary function
# definition (legal after sourcing); nothing here touches a real filesystem.
_stub_probe_ok() {
  local kb="$1"
  # shellcheck disable=SC2317  # reassigned below, not dead code
  eval "_worktree_disk_guard_probe_free_kb() { echo '$kb'; return 0; }"
}
_stub_probe_fail() {
  # shellcheck disable=SC2317
  _worktree_disk_guard_probe_free_kb() { return 1; }
}

echo "=== test_worktree_cap_guard ==="
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Case 0 (sanity, not one of the four required cases): the real, unstubbed
# probe against a real directory returns a plausible verdict -- proves the
# production df-based code path actually runs, not just the stub scaffolding
# exercised below.
# ═════════════════════════════════════════════════════════════════════════
echo "--- Case 0: real probe against a real directory ---"
V0=$(worktree_disk_guard_check "$SCRATCH_FIXTURE_DIR" "$FLOOR_GB")
RC0=$?
if [[ "$RC0" -eq 0 && ( "$V0" == "ok" || "$V0" == "warn" || "$V0" == "unknown" ) ]]; then
  _pass "real probe on a real directory returns a valid verdict ('$V0'), exit 0"
else
  _fail "real probe: expected exit 0 and one of ok/warn/unknown, got exit=$RC0 verdict='$V0'"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Case 1: plenty_free -> ok
# ═════════════════════════════════════════════════════════════════════════
echo "--- Case 1: plenty_free -> ok ---"
_stub_probe_ok $((FLOOR_KB * 5))
V1=$(worktree_disk_guard_check "$SCRATCH_FIXTURE_DIR" "$FLOOR_GB")
RC1=$?
if [[ "$V1" == "ok" && "$RC1" -eq 0 ]]; then
  _pass "plenty_free: verdict=ok, exit 0"
else
  _fail "plenty_free: expected verdict=ok exit=0, got verdict='$V1' exit=$RC1"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Case 2: low_free -> warn, spawn still allowed (exit status 0)
# ═════════════════════════════════════════════════════════════════════════
echo "--- Case 2: low_free -> warn, exit 0 ---"
_stub_probe_ok $((FLOOR_KB / 2))
V2=$(worktree_disk_guard_check "$SCRATCH_FIXTURE_DIR" "$FLOOR_GB")
RC2=$?
if [[ "$V2" == "warn" && "$RC2" -eq 0 ]]; then
  _pass "low_free: verdict=warn, exit 0 (spawn still allowed)"
else
  _fail "low_free: expected verdict=warn exit=0, got verdict='$V2' exit=$RC2"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Case 3: stat_unavailable (probe fails) -> unknown, spawn allowed, fail-open
# ═════════════════════════════════════════════════════════════════════════
echo "--- Case 3: stat_unavailable -> unknown, exit 0 (fail-open) ---"
_stub_probe_fail
V3=$(worktree_disk_guard_check "$SCRATCH_FIXTURE_DIR" "$FLOOR_GB")
RC3=$?
if [[ "$V3" == "unknown" && "$RC3" -eq 0 ]]; then
  _pass "stat_unavailable: verdict=unknown, exit 0 (fail-open)"
else
  _fail "stat_unavailable: expected verdict=unknown exit=0, got verdict='$V3' exit=$RC3"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Case 4: at_threshold — free space exactly at the floor.
# Boundary is documented and asserted explicitly here: worktree_disk_guard_check
# warns only when free is STRICTLY BELOW the floor (`-lt`), so free == floor
# is "ok". The boundary is >=, not >.
# ═════════════════════════════════════════════════════════════════════════
echo "--- Case 4: at_threshold_boundary_is_gte_ok (free == floor -> ok) ---"
_stub_probe_ok "$FLOOR_KB"
V4=$(worktree_disk_guard_check "$SCRATCH_FIXTURE_DIR" "$FLOOR_GB")
RC4=$?
if [[ "$V4" == "ok" && "$RC4" -eq 0 ]]; then
  _pass "at_threshold_boundary_is_gte_ok: free==floor -> verdict=ok (boundary is >=, not >)"
else
  _fail "at_threshold_boundary_is_gte_ok: expected verdict=ok exit=0 at free==floor, got verdict='$V4' exit=$RC4"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Negative control (Spec item 8): with the probe stubbed to report abundant
# free space, the low_free case's own assertion (verdict==warn) must fail --
# not the suite overall, this specific comparison. A guard that reports
# "warn" no matter what the probe says would pass Case 2 above for the wrong
# reason; this proves it does not.
# ═════════════════════════════════════════════════════════════════════════
echo "--- Negative control: low_free assertion must fail when probe reports abundant space ---"
_stub_probe_ok $((FLOOR_KB * 5))
V_NEG=$(worktree_disk_guard_check "$SCRATCH_FIXTURE_DIR" "$FLOOR_GB")
LOW_FREE_ASSERTION_WOULD_PASS="false"
[[ "$V_NEG" == "warn" ]] && LOW_FREE_ASSERTION_WOULD_PASS="true"
if [[ "$LOW_FREE_ASSERTION_WOULD_PASS" == "false" ]]; then
  _pass "negative_control: low_free's verdict==warn assertion correctly fails under an abundant-space stub (got verdict='$V_NEG') -- the guard can be made to not fire"
else
  _fail "negative_control: verdict was 'warn' even with abundant free space stubbed -- the guard cannot be made to fail, so Case 2 is not testing anything"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
