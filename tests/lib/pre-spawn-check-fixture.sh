#!/usr/bin/env bash
# tests/lib/pre-spawn-check-fixture.sh — isolated workspace for exercising the
# real scripts/pre-spawn-check.sh without touching the checked-out
# .autonomous-team/agent-feed.jsonl (D#2267).
#
# The problem this solves
# ------------------------
# scripts/pre-spawn-check.sh, scripts/agent-feed-append.sh, and
# backend/agent_feed.py each resolve their own REPO_ROOT / _REPO_ROOT from
# their own invoked-file location (SCRIPT_DIR/.. or __file__.parent.parent),
# with no env override. Invoking the real, in-place copies of these files
# means every one of them resolves the SAME live
# $REPO_ROOT/.autonomous-team/agent-feed.jsonl every running agent's own
# feed writes land in — reading it races real concurrent activity, and a
# careless full-file rewrite (a read-filter-rewrite cleanup helper, say) can
# silently drop a real agent's concurrent append.
#
# This mirrors tests/lib/repo-root-fixture.sh's approach (materialise a
# private root, copy the real code under test into it byte-for-byte, invoke
# the copy) rather than adding a test-only env override to any of these
# production files. Extracted here, rather than duplicated inline in every
# suite that exercises pre-spawn-check.sh, because
# tests/test_pre_spawn_check_block_events.sh already had a working copy of
# this exact logic before D#2267 -- a second near-identical copy in a
# sibling suite is the mistake D#2119 already removed once elsewhere; this
# extraction keeps there being exactly one.
#
# Usage (see either test_pre_spawn_check_*.sh for a full example):
#   source ".../lib/pre-spawn-check-fixture.sh"
#   TMPDIR_BASE=$(mktemp -d)
#   trap 'rm -rf "$TMPDIR_BASE"' EXIT
#   ws=$(make_workspace)          # requires $TMPDIR_BASE set by the caller
#   install_sandbox "$ws"         # requires $SCRIPTS_DIR and $REPO_ROOT set by the caller
#   run_psc_sandboxed "$ws" --role executor --discussion 999
#   psc_exit "$ws"; psc_stderr "$ws"; psc_log "$ws"
#   feed_line_count "$ws/.autonomous-team/agent-feed.jsonl"
#
# make_workspace() and install_sandbox() are implicit-global-argument style
# (like tests/lib/blackboard-fixture.sh's export-based convention): the
# caller must set $TMPDIR_BASE (a scratch parent dir it owns and cleans up)
# before calling make_workspace, and $SCRIPTS_DIR / $REPO_ROOT (the real
# checkout's paths) before calling install_sandbox.

# make_workspace — create one isolated scratch workspace under $TMPDIR_BASE
# with the directory skeleton pre-spawn-check.sh and its dependents expect
# (.autonomous-team, backend, scripts). Prints the workspace path on stdout.
make_workspace() {
  local ws
  ws=$(mktemp -d "$TMPDIR_BASE/ws-XXXXXX")
  mkdir -p "$ws/.autonomous-team"
  mkdir -p "$ws/backend"
  mkdir -p "$ws/scripts"
  echo "$ws"
}

# install_sandbox <ws> — copy pre-spawn-check.sh and everything it
# unconditionally sources/execs into $ws, so that when it runs from there
# its own SCRIPT_DIR/REPO_ROOT resolve inside $ws instead of the real
# checkout. backend/*.py mocks a caller places at $ws/backend then take
# effect, because pre-spawn-check.sh reaches them via "$REPO_ROOT/backend/...".
#
# rotate-team-log.sh is stubbed (not copied) so callers can assert on the
# text handed to it without touching a real team-log issue.
# external_intake_gate.py is stubbed to always-allow: the real one shells
# out to `gh` against a live Discussion, which no unit test should ever do.
install_sandbox() {
  local ws="$1"
  mkdir -p "$ws/scripts/lib"
  cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$ws/scripts/pre-spawn-check.sh"
  cp "$SCRIPTS_DIR/agent-feed-append.sh" "$ws/scripts/agent-feed-append.sh"
  cp "$SCRIPTS_DIR/lib/state-dir.sh" "$ws/scripts/lib/state-dir.sh"
  cp "$SCRIPTS_DIR/lib/hook-event.sh" "$ws/scripts/lib/hook-event.sh"
  cp "$SCRIPTS_DIR/lib/worktree-registry.sh" "$ws/scripts/lib/worktree-registry.sh"
  cp "$REPO_ROOT/backend/agent_feed.py" "$ws/backend/agent_feed.py"

  cat > "$ws/scripts/rotate-team-log.sh" << 'STUBEOF'
#!/usr/bin/env bash
# Test stub -- records argv instead of touching a real team-log issue.
printf '%s\n' "$@" >> "${ROTATE_LOG_CAPTURE:-/dev/null}"
exit 0
STUBEOF
  chmod +x "$ws/scripts/rotate-team-log.sh"

  cat > "$ws/scripts/lib/external_intake_gate.py" << 'STUBEOF'
#!/usr/bin/env python3
# Test stub -- the real gate shells out to `gh` against a live Discussion,
# which no unit test should do. Always allow.
import json
print(json.dumps({"blocked": False, "reason": "test_stub_always_allow"}))
STUBEOF
  chmod +x "$ws/scripts/lib/external_intake_gate.py"
}

# run_psc_sandboxed <ws> [args...] — run the sandboxed pre-spawn-check.sh.
# Always passes --no-register (skips the fleet/per-project concurrency
# sections, which need real backend.fleet.* modules this sandbox does not
# provide) and a unique --event-id (required once DRY_RUN is off). Captures
# stdout/stderr/exit code into $ws/psc.*.
run_psc_sandboxed() {
  local ws="$1"
  shift
  local rc=0
  # The `|| rc=$?` is load-bearing under `set -euo pipefail`: pre-spawn-check.sh
  # is *expected* to exit non-zero on every blocked path, and without this guard
  # that non-zero status would abort the caller right here instead of being
  # captured for its assertions.
  ROTATE_LOG_CAPTURE="$ws/rotate-team-log.captured" \
    bash "$ws/scripts/pre-spawn-check.sh" --no-register --event-id "evt-$$-$RANDOM" "$@" \
    > "$ws/psc.stdout" 2> "$ws/psc.stderr" || rc=$?
  echo "$rc" > "$ws/psc.exit"
}

feed_line_count() {
  local f="$1"
  [[ -f "$f" ]] && wc -l < "$f" || echo "0"
}

psc_exit()   { cat "$1/psc.exit"; }
psc_stderr() { cat "$1/psc.stderr" 2>/dev/null || true; }
psc_log()    { cat "$1/rotate-team-log.captured" 2>/dev/null || true; }
