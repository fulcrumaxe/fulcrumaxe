#!/usr/bin/env bash
# scripts/lib/reconcile-grace.sh — pick the agent_run reconcile grace window
# based on whether the SubagentStop hook is registered.
#
# D#1655: interactive Claude Code sessions never get `end_ts` written on
# subagent completion because the SubagentStop hook isn't wired for that
# session type — spawn-agent.sh always falls back to the 30-min loop grace
# window, so a normal burst of review/audit spawns hits the concurrency cap
# even though most of those agents finished minutes ago.
#
# This helper reuses the same SubagentStop-hook detection already inlined in
# spawn-agent.sh section 3c: when the hook IS registered (headless/loop path,
# real completion telemetry), keep the existing 30-min window unchanged; when
# it is NOT registered (interactive session, or the settings file is missing/
# unreadable), use a short 5-min window so caps self-heal.
#
# D#2107: the hook can be registered in EITHER .claude/settings.json or
# .claude/settings.local.json (settings.json is the tracked file in this
# repo; settings.local.json is the untracked/machine-local override some
# checkouts use instead). Passing either one in is not enough on its own —
# whichever one you were NOT handed also gets checked, by looking for its
# sibling of the same name in the same directory. So the two are unioned:
# registered in either file means "registered".
#
# D#2131: registration alone is not proof of delivery — the hook was found
# registered and essentially never firing (3 production invocations against
# 92 agent_run starts in one session). The registered branch below now also
# checks recent agent_run history for rows NOT closed by a known non-hook
# writer (reconciled-stale / superseded / swept-test-fixture); below a
# threshold that's "registered but dead" and the short window applies. An
# unreadable history fails open to the short window too, never the long one.
#
# Usage:
#   source scripts/lib/reconcile-grace.sh
#   window=$(reconcile_grace_window "$REPO_ROOT/.claude/settings.local.json")
#
# Prints a single integer (minutes) to stdout, plus one stderr line naming
# the window chosen and why. Never errors — a missing or malformed settings
# file (on either side of the pair) is treated as "not registered there",
# and neither file registering the hook is treated as "no hook registered",
# i.e. the short window, matching the fail-safe direction (self-heal caps
# rather than jam).

# _reconcile_grace_build_candidates <settings_path> <repo_root>
# Sets the global array _RG_CANDIDATES to the settings file(s) to check:
# the given path (if any) plus its sibling settings.json <-> settings.local.json
# in the same directory. Falls back to repo_root/.claude's pair when no path
# is given.
_reconcile_grace_build_candidates() {
  local settings_path="$1"
  local repo_root="$2"
  _RG_CANDIDATES=()

  if [[ -n "$settings_path" ]]; then
    _RG_CANDIDATES+=("$settings_path")
    local dir base
    dir="$(dirname -- "$settings_path")"
    base="$(basename -- "$settings_path")"
    case "$base" in
      settings.local.json) _RG_CANDIDATES+=("$dir/settings.json") ;;
      settings.json)       _RG_CANDIDATES+=("$dir/settings.local.json") ;;
    esac
  else
    _RG_CANDIDATES+=("$repo_root/.claude/settings.json" "$repo_root/.claude/settings.local.json")
  fi
}

# _reconcile_grace_find_registered <path> [path...]
# Prints the first path that registers SubagentStop and exits 0; exits 1 with
# no output if none do. Never raises on a missing or malformed file.
_reconcile_grace_find_registered() {
  python3 -c "
import json, sys

def registered(path):
    try:
        with open(path) as f:
            s = json.load(f)
    except Exception:
        return False
    hooks = s.get('hooks', {}).get('SubagentStop', [])
    if not isinstance(hooks, list):
        return False
    return any(grp.get('hooks') for grp in hooks if isinstance(grp, dict))

for p in sys.argv[1:]:
    if registered(p):
        print(p)
        sys.exit(0)
sys.exit(1)
" "$@" 2>/dev/null
}

# reconcile_grace_hook_registered <settings_path>
# Boolean-only check (0/1 exit) for whether SubagentStop is registered in
# the given settings file or its settings.json/settings.local.json sibling.
# Exposed so callers that only need the yes/no answer (spawn-agent.sh
# section 3c's log line) don't have to carry their own copy of the
# detection logic.
reconcile_grace_hook_registered() {
  local settings_path="${1:-}"
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  _reconcile_grace_build_candidates "$settings_path" "$repo_root"
  _reconcile_grace_find_registered "${_RG_CANDIDATES[@]}" >/dev/null
}

# _reconcile_grace_hook_liveness <repo_root> <lookback> <threshold>
# Prints "<is_live> <hook_closed> <total>" for the last <lookback> closed
# agent_run rows, or nothing (exit 1) on any failure — the caller fails open
# to the short window in that case. The python try/except is the sole
# traceback guard; callers must not rely on shell-level stderr redirection.
_reconcile_grace_hook_liveness() {
  local repo_root="$1"
  local lookback="$2"
  local threshold="$3"
  python3 - "$repo_root" "$lookback" "$threshold" <<'PYEOF'
import sys
repo_root, lookback, threshold = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
sys.path.insert(0, repo_root)
try:
    import duckdb
    from backend.agent_run_tracker import _db_path
    from backend.agent_run_verdicts import is_agent_reported
    db_path = _db_path()
    if not db_path.exists():
        sys.exit(1)
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(
        "SELECT verdict FROM agent_run WHERE end_ts IS NOT NULL "
        "ORDER BY end_ts DESC LIMIT ?",
        [lookback],
    ).fetchall()
    conn.close()
    total = len(rows)
    if total == 0:
        sys.exit(1)
    hook_closed = sum(1 for (verdict,) in rows if is_agent_reported(verdict))
    is_live = 1 if (hook_closed / total) >= threshold else 0
    print(f"{is_live} {hook_closed} {total}")
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
}

reconcile_grace_window() {
  local settings_path="${1:-}"
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

  local interactive_default=5
  local loop_default=30
  local liveness_lookback_default=50
  local liveness_threshold_default=0.5

  local win_interactive
  win_interactive=$(python3 "$repo_root/backend/control_plane.py" get policies.team_lead.agent_run_stale_after_min_interactive 2>/dev/null | tr -d '"' || echo "$interactive_default")
  [[ "$win_interactive" =~ ^[0-9]+$ ]] || win_interactive=$interactive_default

  local win_loop
  win_loop=$(python3 "$repo_root/backend/control_plane.py" get policies.team_lead.agent_run_stale_after_min 2>/dev/null | tr -d '"' || echo "$loop_default")
  [[ "$win_loop" =~ ^[0-9]+$ ]] || win_loop=$loop_default

  _reconcile_grace_build_candidates "$settings_path" "$repo_root"

  local hook_file window reason
  if hook_file=$(_reconcile_grace_find_registered "${_RG_CANDIDATES[@]}"); then
    local liveness_lookback
    liveness_lookback=$(python3 "$repo_root/backend/control_plane.py" get policies.team_lead.reconcile_grace_liveness_lookback 2>/dev/null | tr -d '"' || echo "$liveness_lookback_default")
    [[ "$liveness_lookback" =~ ^[0-9]+$ ]] || liveness_lookback=$liveness_lookback_default

    local liveness_threshold
    liveness_threshold=$(python3 "$repo_root/backend/control_plane.py" get policies.team_lead.reconcile_grace_liveness_threshold 2>/dev/null | tr -d '"' || echo "$liveness_threshold_default")
    [[ "$liveness_threshold" =~ ^[0-9]+(\.[0-9]+)?$ ]] || liveness_threshold=$liveness_threshold_default

    local liveness_out
    if liveness_out=$(_reconcile_grace_hook_liveness "$repo_root" "$liveness_lookback" "$liveness_threshold") && [[ -n "$liveness_out" ]]; then
      local is_live hook_closed total
      read -r is_live hook_closed total <<< "$liveness_out"
      if [[ "$is_live" == "1" ]]; then
        window="$win_loop"
      else
        window="$win_interactive"
      fi
      reason="SubagentStop hook found in $hook_file — $hook_closed/$total recent closed rows hook-closed (threshold $liveness_threshold)"
    else
      # Fail-open: unreadable/empty history is not evidence of liveness.
      window="$win_interactive"
      reason="SubagentStop hook found in $hook_file but recent agent_run history is unreadable — defaulting to short window"
    fi
  else
    window="$win_interactive"
    reason="SubagentStop hook not found in: ${_RG_CANDIDATES[*]}"
  fi

  echo "[reconcile-grace] window=${window}min reason=${reason}" >&2
  echo "$window"
}
