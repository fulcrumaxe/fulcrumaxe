#!/usr/bin/env bash
# scripts/hooks/post-agent.d/reap-worktrees-throttle.sh
#
# Runs the worktree reaper, throttled to at most once per hour.
#
# Sourced by post-agent-hook.sh (step 6f) — expects these caller variables:
#   SCRIPT_DIR, REPO_ROOT
#
# THE MEASURED PROBLEM (D#2155): this call used to run unconditionally after
# EVERY agent spawn, justified by a comment claiming "< 100ms". Measured on
# this host at N=198 spawns: 16,460ms per call — roughly 165x that estimate,
# and worst under exactly the "nothing to reap" condition the old comment
# named (0 of 198 reaped that day). Combined with the claim scan, that was
# ~36s of dead weight on every single agent completion, paid unknowingly.
#
# THIS IS A THROTTLE, NOT A REMOVAL: reap-worktrees.sh is the sole emitter of
# the orphan_worktree_rate metric, so dropping the call would kill it. Hourly
# is a cadence choice, not something elapsed_s or the rate math requires —
# elapsed_s (reap-worktrees.sh:151-183) times a single reap call's own
# duration (~16s), not the gap since the previous invocation, so it has no
# bearing on how often this should fire. Hourly was picked because it sits
# well inside stats_freshness_watchdog.py's staleness margins (warns at 2h,
# bugs at 24h) while cutting the unconditional per-spawn cost this Discussion
# measured; the metric was already spawn-triggered rather than cron-driven,
# so idle-period staleness beyond that is pre-existing and unrelated to this
# change.
#
# Scheduling this as a cron/scheduled job instead was considered and
# rejected: gates.scheduled_jobs is off and every entry in
# scripts/schedule/jobs.yaml is enabled:false, so a scheduled emitter would
# emit nothing under the current substrate. The throttle has to live on the
# hot path it's throttling.
#
# What this file does NOT touch: which worktrees are eligible for reaping,
# or anything else about reap-worktrees.sh's removal behaviour. Only how
# often it runs.
#
# The throttle stamp lives under AUTONOMOUS_TEAM_STATE_DIR, not the repo —
# CLAUDE.md's Runtime State Directory rule: all mutable runtime state lives
# outside the working tree, because an untracked file inside it is exactly
# what makes a worktree unreapable (the problem family this whole Spec is
# about). REPO_ROOT here is always the main checkout (reap-worktrees.sh
# requires that structurally), so this was dirtying the primary repo on
# every throttled run, not a worktree being reaped — still a real defect,
# just not a self-defeating one.
#
# Non-fatal: failures must not block the hook exit code.

_REAP_STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-${HOME}/.fulcrumaxe-state}"
_REAP_STAMP="${REAP_STAMP_OVERRIDE:-$_REAP_STATE_DIR/.last-worktree-reap}"
_REAP_INTERVAL="${REAP_INTERVAL_OVERRIDE:-3600}"  # 1 hour — see rationale above
_RUN_REAP=false
_REAP_STAMP_AGE="never"

if [[ ! -f "$_REAP_STAMP" ]]; then
  _RUN_REAP=true
else
  _REAP_STAMP_AGE=$(python3 -c "
import os, time
try:
    print(int(time.time() - os.path.getmtime('$_REAP_STAMP')))
except Exception:
    print(99999)
" 2>/dev/null || echo "99999")
  if [[ "$_REAP_STAMP_AGE" -ge "$_REAP_INTERVAL" ]] 2>/dev/null; then
    _RUN_REAP=true
  fi
fi

if [[ "$_RUN_REAP" == "true" ]]; then
  # Touch first so two spawns finishing at nearly the same moment don't both
  # see the throttle as open and double-fire.
  mkdir -p "$(dirname "$_REAP_STAMP")" 2>/dev/null || true
  touch "$_REAP_STAMP" 2>/dev/null || true
  echo "[post-agent-hook] reap_worktrees: running (last run: ${_REAP_STAMP_AGE})"
  bash "$SCRIPT_DIR/reap-worktrees.sh" --quiet 2>/dev/null || true
else
  # The skip must be observable, not silent — this line is what the D#2155
  # panel meant by that; it lands in the same post-agent-hook log every
  # other step writes to.
  echo "[post-agent-hook] reap_worktrees: skipped — throttled, last run ${_REAP_STAMP_AGE}s ago (< ${_REAP_INTERVAL}s)"
fi
