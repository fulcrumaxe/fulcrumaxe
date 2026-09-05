#!/usr/bin/env bash
# tests/test_reap_worktrees_throttle.sh — D#2155 PR-b discriminating test.
#
# scripts/post-agent-hook.sh:529-530 used to call reap-worktrees.sh
# unconditionally after every spawn (measured 16,460ms per call — see the
# comment on the "6f. Worktree reaper" block for the full writeup). This
# test proves the hourly throttle actually throttles: it extracts the "6f."
# block VERBATIM from the real, on-disk scripts/post-agent-hook.sh and runs
# it twice in a row (simulating two back-to-back agent completions) against
# a stub reap-worktrees.sh that just counts calls.
#
#   pre-fix (unthrottled):  2 runs -> reaper invoked 2 times  -> FAILS below
#   post-fix (throttled):   2 runs -> reaper invoked 1 time   -> PASSES below
#
# Because this test reads the block off disk rather than a frozen
# `git show HEAD:...` snapshot, it discriminates correctly under
# `git stash` too. Verified by hand:
#   git stash -u && bash tests/test_reap_worktrees_throttle.sh   # -> FAIL
#   git stash pop && bash tests/test_reap_worktrees_throttle.sh  # -> PASS
# (`-u` matters: the throttle module is a new, untracked file — a plain
# `git stash` without it would leave the module on disk and the pre-fix run
# would spuriously pass.)
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_FILE="$REPO_ROOT_REAL/scripts/post-agent-hook.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

TMPDIR_ROOT=$(mktemp -d /tmp/test-reap-throttle-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Extract the "6f. Worktree reaper" block verbatim from the real, on-disk
# post-agent-hook.sh, up to (not including) the "7. Log to team-log" block
# that follows it.
# ---------------------------------------------------------------------------
BLOCK_FILE="$TMPDIR_ROOT/block.sh"
awk '
  /^# ── 6f\. Worktree reaper/ { grab=1 }
  grab && /^# ── 7\. Log to team-log/ { exit }
  grab { print }
' "$HOOK_FILE" > "$BLOCK_FILE"

if [[ ! -s "$BLOCK_FILE" ]]; then
  echo "FAIL: could not locate the '6f. Worktree reaper' block in $HOOK_FILE (marker text changed?)"
  exit 1
fi

echo "=== extracted block ==="
cat "$BLOCK_FILE"
echo "========================"
echo ""

# ---------------------------------------------------------------------------
# Sandbox: SCRIPT_DIR/reap-worktrees.sh is a stub that appends one line per
# call. hooks/ is a real symlink to the real repo's scripts/hooks/, so a
# post-fix throttle module under hooks/post-agent.d/ is exercised exactly as
# post-agent-hook.sh would see it; a stashed (pre-fix) tree has no such
# module and the extracted block's own unconditional call runs instead.
#
# REAP_STAMP_OVERRIDE keeps the throttle stamp inside this test's own tmpdir
# instead of the real AUTONOMOUS_TEAM_STATE_DIR (post-fix default) or the
# sandboxed REPO_ROOT (pre-fix, before this env var existed at all) — this
# test must never touch either real location.
# ---------------------------------------------------------------------------
SANDBOX="$TMPDIR_ROOT/sandbox"
mkdir -p "$SANDBOX"
ln -s "$REPO_ROOT_REAL/scripts/hooks" "$SANDBOX/hooks"

STAMP="$TMPDIR_ROOT/.last-worktree-reap"
CALL_LOG="$TMPDIR_ROOT/calls.log"
cat > "$SANDBOX/reap-worktrees.sh" <<'STUB'
#!/usr/bin/env bash
echo "call" >> "$CALL_LOG"
exit 0
STUB
chmod +x "$SANDBOX/reap-worktrees.sh"

# Run the extracted block twice, as two separate spawns each would: fresh
# hook-event id each time, so hook_event_has_step never short-circuits it.
: > "$CALL_LOG"
(
  export SCRIPT_DIR="$SANDBOX"
  export REPO_ROOT="$SANDBOX"
  export CALL_LOG="$CALL_LOG"
  export REAP_STAMP_OVERRIDE="$STAMP"
  hook_event_has_step() { return 1; }
  hook_event_mark_step() { :; }
  # shellcheck source=/dev/null
  source "$BLOCK_FILE"
  # shellcheck source=/dev/null
  source "$BLOCK_FILE"
)

CALLS=$(wc -l < "$CALL_LOG" | tr -d ' ')
echo "reap-worktrees.sh invocations across 2 back-to-back post-agent-hook runs: $CALLS"

if [[ "$CALLS" -eq 1 ]]; then
  _pass "throttled: reaper invoked exactly once across 2 back-to-back runs within the hour"
else
  _fail "expected exactly 1 reaper invocation (hourly throttle), got $CALLS"
fi

# ---------------------------------------------------------------------------
# A third run against a stamp file already 2 hours old must run again — this
# is a throttle, not a one-shot latch.
# ---------------------------------------------------------------------------
if [[ -f "$STAMP" ]]; then
  TWO_HOURS_AGO=$(( $(date +%s) - 7200 ))
  touch -d "@${TWO_HOURS_AGO}" "$STAMP" 2>/dev/null || touch -t "$(date -d "@${TWO_HOURS_AGO}" +%Y%m%d%H%M.%S 2>/dev/null)" "$STAMP" 2>/dev/null || true
  : > "$CALL_LOG"
  (
    export SCRIPT_DIR="$SANDBOX"
    export REPO_ROOT="$SANDBOX"
    export CALL_LOG="$CALL_LOG"
    export REAP_STAMP_OVERRIDE="$STAMP"
    hook_event_has_step() { return 1; }
    hook_event_mark_step() { :; }
    # shellcheck source=/dev/null
    source "$BLOCK_FILE"
  )
  CALLS_AFTER_STALE=$(wc -l < "$CALL_LOG" | tr -d ' ')
  if [[ "$CALLS_AFTER_STALE" -eq 1 ]]; then
    _pass "not a one-shot latch: a >1hr-old stamp runs the reaper again"
  else
    _fail "expected the reaper to run again once its stamp is >1hr stale, got $CALLS_AFTER_STALE calls"
  fi
else
  echo "  (skip: pre-fix block never wrote a throttle stamp, so there's nothing to age — case 1 above already failed this correctly)"
fi

echo ""
echo "==========================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "==========================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
