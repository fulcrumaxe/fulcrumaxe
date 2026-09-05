#!/usr/bin/env bash
# scripts/start-the-day.sh — morning ritual for Team Lead.
#
# Run this at session start. It:
#   1. Pulls main fresh, restores HEAD if drifted
#   2. Verifies external state dir survived overnight
#   3. Runs morning sweeps (budget, subscription, run-analyst, team_status)
#   4. Prints the day's plan
#
# Output is designed for Team Lead (Claude) to read at session start.
# A human user can also read it for a status snapshot.
#
# Usage:
#   bash scripts/start-the-day.sh
#   bash scripts/start-the-day.sh --no-sweeps  # skip slow checks (run-analyst)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
REPO_OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"
# REPO_OWNER/REPO_NAME feed the discussions GraphQL below and belong on the
# Discussion plane. The two `gh pr list` calls do not — they read PRs.
# _require_code_repo prints nothing and returns 1 rather than handing `gh` an
# empty --repo, which gh resolves from the checkout instead of rejecting.
CODE_REPO="$(_require_code_repo "start-the-day")" || exit 1

SKIP_SWEEPS=false
for arg in "$@"; do
  case "$arg" in
    --no-sweeps) SKIP_SWEEPS=true ;;
  esac
done

# ── GH_TOKEN: prefer installation token (15k/hr) over user PAT (5k/hr) ───────
# shellcheck source=scripts/lib/gh-token.sh
source "$REPO_ROOT/scripts/lib/gh-token.sh" || true

# ── Auth precondition: fail loudly, once, before any sweep trusts gh ─────────
# D#1787: a `gh` account that can't resolve $REPO doesn't get better on
# retry, and every downstream call silently swallowing its error text turns
# into fabricated numbers. Check once, centrally, right here.
# shellcheck source=scripts/lib/gh-precondition.sh
source "$REPO_ROOT/scripts/lib/gh-precondition.sh"
assert_gh_can_see_repo "$REPO" || exit 1

echo "==============================================================="
echo "Team Lead — Start the Day"
echo "Date: $(date '+%Y-%m-%d %H:%M %Z')"
echo "==============================================================="

# ── 0. Loop run-readiness check ──────────────────────────────────────────────
# The 2026-05-15 morning failure: Team Lead read a stale plan and didn't notice
# that /loop hadn't run for >24h. The TUI was showing stale data everywhere
# because no loop iteration had written fresh metric_event / loop-metrics rows.
# The ritual flagged "/health/loop returns error" as a yellow warning but didn't
# explain the consequence or the correct next action.
#
# This section runs FIRST and is loud, because it dictates the next action the
# Team Lead must take. Three signals are checked:
#   1. loop-metrics.jsonl freshness (last canonical /loop iteration)
#   2. metric_event freshness in stats.duckdb (last subsystem-sweep write)
#   3. crontab state (must be disabled — Team Lead is the driver)
echo ""
echo "## 0. Loop run-readiness"
echo ""

LOOP_METRICS_PATH="$REPO_ROOT/.autonomous-team/loop-metrics.jsonl"
# Create placeholder if missing so the check always has a file to inspect.
# The file is empty on a fresh install; the first /loop run writes the first row.
if [[ ! -f "$LOOP_METRICS_PATH" ]]; then
  mkdir -p "$(dirname "$LOOP_METRICS_PATH")"
  touch "$LOOP_METRICS_PATH"
  echo "  [info] Created placeholder loop-metrics.jsonl (no /loop iterations yet)"
fi
if [[ -f "$LOOP_METRICS_PATH" ]]; then
  LAST_TS=$(tail -1 "$LOOP_METRICS_PATH" | python3 -c "import sys,json;print(json.loads(sys.stdin.read()).get('timestamp',''))" 2>/dev/null || echo "")
  if [[ -n "$LAST_TS" ]]; then
    AGE_MIN=$(python3 -c "from datetime import datetime,timezone; t=datetime.fromisoformat('$LAST_TS'.replace('Z','+00:00')); print(int((datetime.now(timezone.utc)-t).total_seconds()/60))" 2>/dev/null || echo "?")
    if [[ "$AGE_MIN" == "?" ]]; then
      echo "  ⚠️  loop-metrics.jsonl: could not parse last timestamp"
    elif [[ "$AGE_MIN" -lt 60 ]]; then
      echo "  ✅ Last /loop iteration: ${AGE_MIN}m ago — TUI tiles fresh"
    elif [[ "$AGE_MIN" -lt 180 ]]; then
      echo "  ⚠️  Last /loop iteration: ${AGE_MIN}m ago — TUI tiles getting stale"
      echo "      → Run canonical 8-step /loop now (see CLAUDE.md and memory feedback_canonical_loop_8_steps)"
    else
      echo "  🔴 Last /loop iteration: ${AGE_MIN}m ago — TUI is SHOWING STALE DATA"
      echo "      → Required next action: run canonical 8-step /loop as Team Lead"
      echo "      → Do NOT re-enable cron — Team Lead is the loop driver"
      echo "        (session 85514482 user directive: 'Claude Opus stays Team Lead, cron stays dead')"
    fi
  else
    echo "  ⚠️  loop-metrics.jsonl exists but last line unreadable"
  fi
else
  echo "  🔴 loop-metrics.jsonl missing — no /loop iterations have ever recorded"
  echo "      → Run canonical 8-step /loop as Team Lead to bootstrap"
fi

# metric_event freshness from duckdb
METRIC_AGE=$(python3 -c "
import duckdb
from datetime import datetime, timezone
try:
    db = duckdb.connect('.autonomous-team/stats.duckdb', read_only=True)
    r = db.execute('SELECT MAX(ts) FROM metric_event').fetchone()
    if r[0]:
        age = int((datetime.now(timezone.utc) - r[0].replace(tzinfo=timezone.utc)).total_seconds()/60)
        print(age)
    else:
        print('empty')
except Exception:
    print('error')
" 2>/dev/null)
if [[ "$METRIC_AGE" == "empty" || "$METRIC_AGE" == "error" ]]; then
  echo "  ⚠️  stats.duckdb metric_event: no rows or read error"
elif [[ "$METRIC_AGE" -lt 60 ]]; then
  echo "  ✅ Last metric_event write: ${METRIC_AGE}m ago"
elif [[ "$METRIC_AGE" -lt 180 ]]; then
  echo "  ⚠️  Last metric_event write: ${METRIC_AGE}m ago — KPI tiles getting stale"
else
  echo "  🔴 Last metric_event write: ${METRIC_AGE}m ago — KPI tiles will show stale"
  echo "      → Run canonical /loop step 7.5 (subsystem sweep) to refresh"
fi

# crontab check — must be disabled
CRON_ACTIVE=$(crontab -l 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -E "run-loop-iteration|loop-watchdog" | head -1)
if [[ -n "$CRON_ACTIVE" ]]; then
  echo "  🔴 Cron has ACTIVE /loop or watchdog entries — should be disabled"
  echo "      Active line: $CRON_ACTIVE"
  echo "      → Comment out the line: crontab -e   (Team Lead is the loop driver, not cron)"
else
  echo "  ✅ Cron loop entries disabled (correct — Team Lead drives /loop manually)"
fi

# Snapshot refresh timer — this is what keeps the loop snapshot under MAX_AGE.
# It regenerates a read-only status blob; it does not run the loop, which is why
# the cron check above is still correct and still green.
if systemctl --user is-active --quiet loop-snapshot-refresh.timer 2>/dev/null; then
  echo "  ✅ loop-snapshot-refresh.timer active (snapshot stays under 600s)"
else
  echo "  ⚠️  loop-snapshot-refresh.timer not active — the loop snapshot will go stale"
  echo "      → bash scripts/install-snapshot-timer.sh"
fi

# Warm the snapshot so the first read of the day is fresh even if the timer's
# next tick is minutes away. This is a warm-up, not the trigger — one call per
# session goes stale ten minutes later, which is the bug this replaced.
if bash "$REPO_ROOT/scripts/refresh-loop-snapshot.sh" >/dev/null 2>&1; then
  echo "  ✅ Loop snapshot refreshed"
else
  echo "  ⚠️  Loop snapshot refresh failed — run scripts/refresh-loop-snapshot.sh to see why"
fi

# Reminder of the canonical 8-step loop
echo ""
echo "  ℹ️  How /loop runs: 8 canonical steps from CLAUDE.md, executed by Team Lead"
echo "     in this session (NOT via bash run-loop-iteration.sh or trigger.py)."
echo "     Steps: 0=preflight, 1=repo, 2=team-log, 3=Discussions, 4=PRs,"
echo "            5=ACT, 6=auto-merge, 7=heartbeat, 7.5=subsystem-sweep,"
echo "            8=now.md update, 9=ScheduleWakeup (skip in interactive sessions)."

# ── 1. Pull main fresh, restore HEAD if drifted ──────────────────────────────
echo ""
echo "## 1. Sync to fresh main"

# D#1759 (verb) + D#2075 (gate) — two independent defects in what used to be:
#   BRANCH=$(git branch --show-current); [[ != main ]] && symbolic-ref
#   git reset "--mix""ed" origin/main (redirected to swallow output)
#
#   - a "mixed"-mode reset is documented to leave the working directory
#     unchanged; it only moves HEAD/index. So this section reported success
#     while 37
#     tracked files (including hooks/sandbox_rules.py) sat stale on disk for
#     three weeks — HEAD matched origin/main, content did not (D#1759).
#   - the BRANCH != main check is correct in the primary checkout but
#     inverted in a linked worktree, where sitting on worktree-agent-<id> IS
#     the healthy state. It fired precisely when nothing was wrong and
#     repointed a live executor's HEAD onto the shared main ref (D#2075).
#
# Fix: gate on checkout IDENTITY (not branch name) before touching anything,
# then use a verb that actually updates content.
# shellcheck source=scripts/lib/repo-root-resolve.sh
source "$SCRIPT_DIR/lib/repo-root-resolve.sh"

# _resolve_main_repo_root() walks git-common-dir up to the checkout a linked
# working tree was branched from (equal to _resolve_repo_root() outside one).
# Comparing it against the AMBIENT toplevel — not the script's own
# file-anchored idea of its own root — is what actually catches "this shell
# is standing in a worktree", regardless of how the script was invoked. It is
# also immune to AUTONOMOUS_TEAM_REPO_ROOT: that env var only steers where
# _resolve_repo_root() starts looking, but git-common-dir is filesystem truth
# for whatever real path it's handed, so the climb to the true main root
# still happens even if the starting point was spoofed.
_SYNC_MAIN_ROOT="$(_resolve_main_repo_root)"
_SYNC_AMBIENT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
: "${_SYNC_AMBIENT_ROOT:=$REPO_ROOT}"

if [[ "$_SYNC_AMBIENT_ROOT" != "$_SYNC_MAIN_ROOT" ]]; then
  echo "  🔴 REFUSING: this checkout ($_SYNC_AMBIENT_ROOT) looks like a linked"
  echo "     worktree, not the main checkout ($_SYNC_MAIN_ROOT). Syncing here"
  echo "     would repoint this worktree's HEAD onto the shared main ref —"
  echo "     that's D#2075, and it already happened to a live executor once."
  echo "     → Run start-the-day.sh from the main checkout instead"
  echo "       ($_SYNC_MAIN_ROOT), or escalate to Team Lead. Don't try to fix"
  echo "       this yourself with git symbolic-ref/reset here — both are"
  echo "       refused from inside a worktree, and that's exactly why."
  exit 1
fi

# Every git call below is scoped with -C to the resolved main root rather
# than left to act on whatever tree the shell happens to be standing in, so
# an odd invocation (e.g. an absolute path to this same script run from
# somewhere else) can't silently retarget it either.
BRANCH="$(git -C "$_SYNC_MAIN_ROOT" branch --show-current 2>/dev/null)"
if [[ "$BRANCH" != "main" ]]; then
  echo "  HEAD was on '$BRANCH' — restoring to main"
  git -C "$_SYNC_MAIN_ROOT" symbolic-ref HEAD refs/heads/main
fi

# `--mixed` moved the ref and index only and left the working tree stale —
# the whole D#1759 bug. `pull --ff-only` is the verb ~90 entries in this
# repo's own main reflog already use, and it actually updates content. It is
# deliberately NOT `--hard`: the primary checkout is permanently dirty by
# design (.autonomous-team/config.json, wiki/Changelog.md,
# wiki/Project-Status.md are hook-generated), so a local edit that collides
# with an incoming commit must abort loudly and name the remedy rather than
# be silently discarded (--hard) or silently left desynced (the old --mixed
# behavior). A local edit that does NOT collide survives untouched, same as
# any other git merge.
if PULL_OUT="$(git -C "$_SYNC_MAIN_ROOT" pull --ff-only origin main 2>&1)"; then
  echo "$PULL_OUT" | tail -8
else
  echo "$PULL_OUT" | tail -10
  echo ""
  echo "  🔴 pull --ff-only failed — most likely a local edit in this"
  echo "     permanently-dirty checkout collides with an incoming commit."
  echo "     Nothing was reset or discarded."
  echo "     → git -C \"$_SYNC_MAIN_ROOT\" stash   (or commit the local edit)"
  echo "       then re-run this script, then 'git stash pop' if you stashed."
  exit 1
fi
echo "  HEAD: $(git -C "$_SYNC_MAIN_ROOT" rev-parse --short HEAD) — $(git -C "$_SYNC_MAIN_ROOT" log -1 --format=%s)"

# Clear auto-pull-blocked marker if present — start-of-day sync just succeeded
_APB_MARKER="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}/auto-pull-blocked"
if [[ -f "$_APB_MARKER" ]]; then
  rm -f "$_APB_MARKER"
  echo "  [FIX] Cleared auto-pull-blocked marker (unmerged paths resolved)"
fi

# ── 1b. Working-tree vs origin/main content divergence (D#1763) ─────────────
# Runs after the sync block above so origin/main is fresh. This catches what
# HEAD-only checks can't: D#1759 reverted 37 tracked files while HEAD stayed
# exactly at origin/main. A commit-count check reported healthy; only a
# content diff caught it. Deliberately NOT moved earlier — it keys on `git
# diff HEAD`, and a checkout that is merely behind is clean against HEAD, so
# running it before the sync would silently stop catching this cause at all.
echo ""
echo "## 1b. Working-tree divergence check"
DIVERGENCE_JSON=$(python3 backend/repo_divergence.py check --repo-root "$REPO_ROOT")
DIVERGENCE_RC=$?
echo "$DIVERGENCE_JSON"
if [[ $DIVERGENCE_RC -ne 0 ]]; then
  echo ""
  echo "  🔴 ALARM: working tree has diverged from HEAD in a critical path — this is"
  echo "     the D#1759 signature (HEAD correct, tree content wrong). Stopping —"
  echo "     nothing downstream today is trustworthy until this is resolved."
  exit 1
fi

# ── 2. Verify external state dir intact ──────────────────────────────────────
echo ""
echo "## 2. External state dir health"

EXT=${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}
if [[ -d "$EXT" ]]; then
  echo "  ✅ External state dir: $EXT"
  for f in stats.duckdb state.db audit.jsonl circuit-breaker-history.jsonl blackboard; do
    if [[ -e "$EXT/$f" ]]; then
      size=$(du -sh "$EXT/$f" 2>/dev/null | cut -f1)
      echo "    $f: $size"
    else
      echo "    ⚠️  $f: MISSING"
    fi
  done
else
  echo "  ❌ External state dir does not exist!"
  echo "  Run: bash scripts/setup-state-dir.sh (if shipped) or recreate manually"
fi

# Verify .autonomous-team/ symlinks point to external dir
echo ""
echo "  Symlinks in .autonomous-team/:"
SYMLINK_COUNT=0
for f in stats.duckdb state.db audit.jsonl circuit-breaker-history.jsonl blackboard; do
  if [[ -L ".autonomous-team/$f" ]]; then
    target=$(readlink ".autonomous-team/$f")
    if [[ "$target" == "$EXT"* ]]; then
      echo "    ✅ $f → $target"
      SYMLINK_COUNT=$((SYMLINK_COUNT + 1))
    else
      echo "    ⚠️  $f → $target (wrong target)"
    fi
  elif [[ -e ".autonomous-team/$f" ]]; then
    echo "    ⚠️  $f exists as REAL file (not symlink) — rerun setup-state-dir.sh"
  else
    echo "    -   $f: not present"
  fi
done

if [[ "$SYMLINK_COUNT" -lt 4 ]]; then
  echo ""
  echo "  ⚠️  Less than 4 symlinks found. Re-establish:"
  echo "      bash scripts/setup-state-dir.sh   # if that script exists"
fi

# ── Self-heal: 8 automated fix-ups before any sweeps ────────────────────────
echo ""
echo "## Self-heal checks"

SELFHEAL_WARNS=()

# 1. State directory — idempotent, creates external dir + symlinks if missing
if bash scripts/setup-state-dir.sh >/dev/null 2>&1; then
  echo "  [OK] State dir"
else
  SELFHEAL_WARNS+=("setup-state-dir failed")
  echo "  [WARN] setup-state-dir failed — runtime state may be missing"
fi

# 2. Sandbox hook — installed AND correct schema (project-local since D#1814)
SANDBOX_REINSTALLED=false
if ! grep -q '"PreToolUse"' "$REPO_ROOT/.claude/settings.json" 2>/dev/null; then
  echo "  [FIX] Sandbox hook missing — installing..."
  if bash scripts/install-sandbox-hook.sh >/dev/null 2>&1; then
    SANDBOX_REINSTALLED=true
    echo "  [OK] Sandbox hook installed"
  else
    SELFHEAL_WARNS+=("install-sandbox-hook failed")
    echo "  [WARN] install-sandbox-hook.sh failed — sandbox inactive"
  fi
else
  echo "  [OK] Sandbox hook entry present"
fi
# Smoke test: send a block-worthy command; hook must exit 2
SANDBOX_TEST_RESULT=$(echo '{"tool_name":"Bash","tool_input":{"command":"git checkout main"},"cwd":"/tmp/wt-fake"}' \
  | python3 hooks/sandbox.py 2>/dev/null; echo $?)
if [ "$SANDBOX_TEST_RESULT" -eq 2 ]; then
  echo "  [OK] Sandbox blocks correctly"
else
  echo "  [WARN] Sandbox not blocking git-checkout — re-run: bash scripts/install-sandbox-hook.sh"
  SELFHEAL_WARNS+=("sandbox smoke test failed (exit=$SANDBOX_TEST_RESULT)")
fi

# 3. Expected gate values — flip to true if unset/false
GATES_FLIPPED=()
for gate in phased_orchestration phased_code_review live_run_analyst release_manager docs_writer; do
  v=$(python3 backend/control_plane.py get "gates.$gate" 2>/dev/null | tr -d '"' || echo "")
  if [ "$v" != "true" ]; then
    if python3 backend/control_plane.py set "gates.$gate" true >/dev/null 2>&1; then
      GATES_FLIPPED+=("$gate")
    else
      SELFHEAL_WARNS+=("could not set gates.$gate=true")
    fi
  fi
done
if [ "${#GATES_FLIPPED[@]}" -gt 0 ]; then
  echo "  [FIX] Flipped gates to true: ${GATES_FLIPPED[*]}"
else
  echo "  [OK] Gate values correct"
fi

# 4. Dashboard ports — start if any are unbound (one call starts all four)
PORTS_DOWN=()
for port in 5173 18099 8765 8420; do
  if ! ss -tlnp 2>/dev/null | grep -q ":$port "; then
    PORTS_DOWN+=("$port")
  fi
done
if [ "${#PORTS_DOWN[@]}" -gt 0 ]; then
  echo "  [FIX] Dashboard ports not bound (${PORTS_DOWN[*]}) — starting dashboard in background..."
  bash scripts/start-dashboard.sh >/dev/null 2>&1 &
  sleep 5
  STILL_DOWN=()
  for port in "${PORTS_DOWN[@]}"; do
    if ! ss -tlnp 2>/dev/null | grep -q ":$port "; then
      STILL_DOWN+=("$port")
    fi
  done
  if [ "${#STILL_DOWN[@]}" -gt 0 ]; then
    SELFHEAL_WARNS+=("dashboard ports still unbound after start: ${STILL_DOWN[*]}")
    echo "  [WARN] Dashboard ports still unbound: ${STILL_DOWN[*]}"
  else
    echo "  [OK] Dashboard started"
  fi
else
  echo "  [OK] Dashboard ports bound"
fi

# 5. chrome-devtools MCP — warn if --headless flag missing (manual fix only)
if claude mcp list 2>/dev/null | grep -q "chrome-devtools"; then
  if claude mcp list 2>/dev/null | grep "chrome-devtools" | grep -q "\-\-headless"; then
    echo "  [OK] chrome-devtools MCP has --headless"
  else
    echo "  [WARN] chrome-devtools MCP missing --headless flag"
    echo "         Fix: claude mcp remove chrome-devtools -s local && claude mcp add chrome-devtools npx -- -y chrome-devtools-mcp@latest --headless --isolated"
    SELFHEAL_WARNS+=("chrome-devtools MCP missing --headless")
  fi
else
  echo "  [WARN] chrome-devtools MCP not configured"
  echo "         Fix: claude mcp add chrome-devtools npx -- -y chrome-devtools-mcp@latest --headless --isolated -s local"
  SELFHEAL_WARNS+=("chrome-devtools MCP not found")
fi

# 6. Reap stale polling shells aged >30min (until...sleep patterns)
# etime format from ps: [[DD-]HH:]MM:SS or HH:MM:SS — match minutes ≥ 30 or any hours/days
REAPED_PIDS=()
while IFS= read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  if [ -n "$pid" ] && [ "$pid" != "$$" ]; then
    kill "$pid" 2>/dev/null && REAPED_PIDS+=("$pid") || true
  fi
done < <(ps -eo pid,etime,command 2>/dev/null \
  | grep -E 'until[[:space:]]|sleep[[:space:]].*until' \
  | awk '
    {
      etime = $2
      # match: DD-HH:MM:SS or HH:MM:SS or MM:SS where MM>=30
      if (etime ~ /^[0-9]+-/)          { print $1 }  # days prefix = definitely old
      else if (etime ~ /^[0-9]+:[0-9]+:[0-9]+$/) { print $1 }  # HH:MM:SS = old
      else if (etime ~ /^[0-9]+:[0-9]+$/) {
        split(etime, parts, ":")
        if (parts[1]+0 >= 30) { print $1 }  # MM >= 30
      }
    }' \
  | grep -v "^$$\$" 2>/dev/null || true)
if [ "${#REAPED_PIDS[@]}" -gt 0 ]; then
  echo "  [FIX] Reaped ${#REAPED_PIDS[@]} stale polling shell(s): ${REAPED_PIDS[*]}"
else
  echo "  [OK] No stale polling shells"
fi

# 7. /health/loop smoke test (only if port 18099 is bound)
if ss -tlnp 2>/dev/null | grep -q ":18099 "; then
  health=$(curl -sf http://127.0.0.1:18099/health/loop 2>/dev/null || echo "")
  if [ -z "$health" ]; then
    echo "  [WARN] /health/loop returned empty — loop timeline may be stale"
    SELFHEAL_WARNS+=("/health/loop returned empty")
  elif echo "$health" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d.get('status') not in ('error',None) else 1)" 2>/dev/null; then
    echo "  [OK] /health/loop healthy"
  else
    echo "  [WARN] /health/loop returns error status — check loop-metrics.jsonl freshness"
    SELFHEAL_WARNS+=("/health/loop returns error")
  fi
else
  echo "  [SKIP] /health/loop smoke test (port 18099 not bound)"
fi

# 8. Reap zombie puppeteer Chrome processes from overnight test runs
bash "$REPO_ROOT/scripts/reap-zombie-chromes.sh" 2>/dev/null \
  && echo "  [OK] Puppeteer chrome reaper ran" \
  || echo "  [WARN] Puppeteer chrome reaper failed (non-fatal)"

# 9. Sweep hook-events/ to prevent monotonic growth
SWEEP_OUT=$(bash "$REPO_ROOT/scripts/sweep-hook-events.sh" 2>&1) && SWEEP_OK=true || SWEEP_OK=false
if [[ "$SWEEP_OK" == "true" ]]; then
  LOCKS_DEL=$(echo "$SWEEP_OUT" | grep "stale locks deleted" | grep -oE '[0-9]+' | tail -1 || echo "0")
  ORPHANS_DEL=$(echo "$SWEEP_OUT" | grep "orphan markers removed" | grep -oE '[0-9]+' | tail -1 || echo "0")
  DONE_DEL=$(echo "$SWEEP_OUT" | grep "done/ entries removed" | grep -oE '[0-9]+' | tail -1 || echo "0")
  GZ_CNT=$(echo "$SWEEP_OUT" | grep "blocks files gzipped" | grep -oE '[0-9]+' | tail -1 || echo "0")
  echo "  [OK] hook-events sweep: -${LOCKS_DEL} locks, -${ORPHANS_DEL} orphans, -${DONE_DEL} done entries, ${GZ_CNT} blocks gzipped"
else
  echo "  [WARN] hook-events sweep failed (non-fatal)"
  SELFHEAL_WARNS+=("hook-events sweep failed")
fi

# 9b. Sweep stale git worktrees to prevent the claim-gate false-positive
# incident from reaccumulating (D#1616). Dry-run here by design — bulk
# removal of accumulated worktrees is a deliberate Team Lead action, not
# something that silently happens on every morning ritual.
STALE_WT_OUT=$(bash "$REPO_ROOT/scripts/sweep-stale-worktrees.sh" --dry-run 2>&1) && STALE_WT_OK=true || STALE_WT_OK=false
if [[ "$STALE_WT_OK" == "true" ]]; then
  STALE_WT_COUNT=$(echo "$STALE_WT_OUT" | grep -oE '^-[0-9]+ stale worktrees removed' | grep -oE '[0-9]+' || echo "0")
  echo "  [OK] stale-worktree sweep (dry-run): -${STALE_WT_COUNT} candidate(s) — run 'bash scripts/sweep-stale-worktrees.sh --apply' to actually remove"
else
  echo "  [WARN] stale-worktree sweep failed (non-fatal)"
  SELFHEAL_WARNS+=("stale-worktree sweep failed")
fi

# 10. Summary: green / yellow / red
echo ""
if [ "${#SELFHEAL_WARNS[@]}" -eq 0 ]; then
  echo "  Status: GREEN — all self-heal checks passed, no manual intervention needed"
else
  echo "  Status: YELLOW — ${#SELFHEAL_WARNS[@]} issue(s) need attention:"
  for w in "${SELFHEAL_WARNS[@]}"; do
    echo "    - $w"
  done
fi

# ── 3. Morning sweeps ────────────────────────────────────────────────────────
echo ""
echo "## 3. Morning sweeps"

if [[ "$SKIP_SWEEPS" == "true" ]]; then
  echo "  (skipped via --no-sweeps)"
else
  echo ""
  echo "  Budget:"
  python3 backend/budget.py status 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'    spent: {d.get(\"spent\")} / {d.get(\"ceiling\")} ({len(d.get(\"agents\",[]))} agents)')" \
    2>/dev/null || echo "    (budget.py failed)"

  echo ""
  echo "  Subscription:"
  python3 backend/subscription_usage.py 2>/dev/null | sed 's/^/    /' || echo "    (subscription_usage.py failed)"
  python3 backend/subscription_usage.py --weekly 2>/dev/null | head -3 | sed 's/^/    /' || true

  echo ""
  echo "  Self-observe gate:"
  echo -n "    "; python3 backend/control_plane.py get gates.self_observe_enforcement 2>/dev/null || echo "(unset)"

  echo ""
  echo "  Path A gates:"
  echo -n "    phased_orchestration: "; python3 backend/control_plane.py get gates.phased_orchestration 2>/dev/null || echo "(unset)"
  echo -n "    phased_code_review: "; python3 backend/control_plane.py get gates.phased_code_review 2>/dev/null || echo "(unset)"

  # D#2271: CI-gate-decline streak — merges since the last verified CI pass.
  # Silent when 0 (nothing to say); backend/gate_streak.py owns the logic.
  GATE_STREAK_LINE=$(python3 backend/gate_streak.py --render 2>/dev/null || true)
  if [ -n "$GATE_STREAK_LINE" ]; then
    echo ""
    echo "$GATE_STREAK_LINE"
  fi

  echo ""
  echo "  Open PRs:"
  # Check gh's own exit code directly (not the exit code of a pipeline whose
  # last stage is something that always succeeds) so a failed call reports
  # its actual error text instead of a guessed cause.
  PR_LIST_RAW=$(gh pr list --repo "$CODE_REPO" --state open --json number,title,labels --limit 20 2>&1)
  PR_LIST_RC=$?
  if [[ $PR_LIST_RC -ne 0 ]]; then
    echo "    (gh pr list failed, exit ${PR_LIST_RC}: ${PR_LIST_RAW:0:200})"
  else
    printf '%s' "$PR_LIST_RAW" | python3 -c "
import json, sys
try:
    prs = json.load(sys.stdin)
    if not prs:
        print('    (none)')
    for p in prs:
        labels = [l['name'] for l in p.get('labels',[])]
        flags = ' '.join([f'[{l[:18]}]' for l in labels if 'review' in l or 'needs' in l])
        print(f'    #{p[\"number\"]}: {p[\"title\"][:70]} {flags}')
except Exception as e:
    print(f'    (error: {e})')
"
  fi

  echo ""
  echo "  Recent run-analyst findings (last 12h):"
  source "$SCRIPT_DIR/lib/run-analyst-sweep.sh"
  run_analyst_sweep

  echo ""
  echo "  Stats freshness:"
  python3 -c "
import duckdb
try:
    conn = duckdb.connect('.autonomous-team/stats.duckdb', read_only=True)
    r = conn.execute('SELECT COUNT(DISTINCT metric), MAX(ts) FROM metric_event').fetchone()
    print(f'    {r[0]} unique metrics, latest write: {r[1]}')
except Exception as e:
    print(f'    (duckdb read failed: {e})')
" 2>/dev/null

  echo ""
  echo "  SPEC_READY Discussions (count):"
  # Same shared parse as the loop selector — a `contains("STATUS:SPEC_READY")`
  # jq filter counted prose mentions as ready and could not see a BLOCKED-BY at
  # all, so the morning count disagreed with what the loop would actually spawn.
  #
  # hasDiscussionsEnabled rides along on the same query (zero extra round
  # trips) because a repo with Discussions turned off returns the exact same
  # shape as a genuinely empty queue — {"nodes":[]}, no GraphQL error — so
  # "0 SPEC_READY" used to read identically for "nothing's ready yet" and
  # "Discussions was never enabled" (D#2217). Distinguish them explicitly
  # instead of reporting a disabled feature as an empty queue.
  gh api graphql -f query='query { repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") { hasDiscussionsEnabled discussions(first:50, states:OPEN) { nodes { number title body } } } }' 2>/dev/null \
    | python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from backend.blocked_by import partition_spec_ready
data = json.load(sys.stdin)
repo = data.get("data", {}).get("repository") or {}
if repo.get("hasDiscussionsEnabled") is False:
    print("    ⚠️  GitHub Discussions is DISABLED for this repo — that, not an empty queue, is why this reads 0.")
    print("        Enable it: gh api -X PATCH repos/'"$REPO_OWNER"'/'"$REPO_NAME"' -F has_discussions=true")
else:
    nodes = repo.get("discussions", {}).get("nodes", [])
    ready, blocked = partition_spec_ready(nodes)
    print(f"    {len(ready)} SPEC_READY (spawnable)")
    for num, reasons in blocked:
        print(f"    D#{num} SPEC_READY but BLOCKED — {reasons}")
' "$REPO_ROOT" 2>/dev/null || echo "    (graphql failed)"

  echo ""
  echo "  Dial State:"
  python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}')
_VERB_LABELS = {1: 'ask', 2: 'propose-confirm', 3: 'propose-timeout', 4: 'announce', 5: 'act'}
try:
    from backend.dial_registry import list_directives
    from datetime import datetime, timezone
    directives = list_directives()
    for d in directives:
        cls = d['class']
        lvl = d['level']
        ceil = d['ceiling']
        verb = _VERB_LABELS.get(lvl, str(lvl))
        active_timed = []
        for directive in d.get('directives', []):
            ttl = directive.get('ttl_until')
            if ttl:
                try:
                    exp = datetime.fromisoformat(ttl)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) < exp:
                        revert_ts = exp.strftime('%Y-%m-%dT%H:%MZ')
                        active_timed.append(revert_ts)
                except Exception:
                    pass
        timed_str = ''
        if active_timed:
            timed_str = ' [reverts at ' + ', '.join(active_timed) + ']'
        print(f'    {cls:<25} {verb} (level {lvl}/{ceil}){timed_str}')
except Exception as e:
    print(f'    (dial_registry unavailable: {e})')
" 2>/dev/null || echo "    (dial_registry unavailable)"
fi

# ── Auto-generate today's plan if absent ─────────────────────────────────────
TODAY=$(date '+%Y-%m-%d')
PLAN_TODAY=".autonomous-team/PLAN-${TODAY}.md"
[ -f "$PLAN_TODAY" ] || bash "$REPO_ROOT/scripts/auto-plan.sh" --date "$TODAY" 2>&1 | sed 's/^/  /'

# ── 4. The day's plan ────────────────────────────────────────────────────────
echo ""
echo "## 4. Today's Plan"
echo ""

# Find the most recent PLAN-YYYY-MM-DD.md

# Fall back to latest plan if today's doesn't exist
if [[ -f "$PLAN_TODAY" ]]; then
  PLAN_FILE="$PLAN_TODAY"
else
  PLAN_FILE=$(ls -t .autonomous-team/PLAN-*.md 2>/dev/null | head -1)
fi

if [[ -n "$PLAN_FILE" && -f "$PLAN_FILE" ]]; then
  echo "  Plan: $PLAN_FILE"
  echo ""
  cat "$PLAN_FILE"
else
  echo "  ⚠️  No PLAN-*.md file found in .autonomous-team/"
  echo "  Team Lead should generate one for today before acting."
fi

# ── 5. Plan staleness check ──────────────────────────────────────────────────
# Catches the 2026-05-15 failure mode: an end-of-day plan listed D#854 sub-items
# as "remaining" when all 9 had merged the evening before. Operator spawned
# 4 duplicate executors based on the stale plan. See memory
# feedback_check_merged_before_spawn.md.
#
# Strategy:
#   - Extract every D#NNN reference from today's plan
#   - For each, count merged PRs that mention the discussion in title/body
#   - Flag any D#NNN referenced in the plan that has merged PRs (likely DONE)
#   - Surface open Discussions NOT mentioned in the plan (work the plan missed)
echo ""
echo "## 5. Plan staleness check"
echo ""
if [[ -n "$PLAN_FILE" && -f "$PLAN_FILE" ]]; then
  PLAN_DISCS=$(grep -oE 'D#[0-9]+' "$PLAN_FILE" | sort -u | sed 's/D#//')
  if [[ -z "$PLAN_DISCS" ]]; then
    echo "  (no D#NNN references in plan — nothing to verify)"
  else
    STALE_COUNT=0
    SWEEP_FAILED=false
    for N in $PLAN_DISCS; do
      MERGED_PRS=$(gh pr list --repo "$CODE_REPO" \
        --state merged --limit 30 --search "D#${N}" \
        --json number,title --jq 'length' 2>/dev/null)
      MERGED_RC=$?
      DISC_STATE=$(gh api graphql -F num="$N" -f query='query($num:Int!) { repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") { discussion(number:$num) { closed } } }' --jq '.data.repository.discussion.closed' 2>/dev/null)
      DISC_RC=$?
      # A failed gh call is not "0 merged PRs" or "state unknown, treat as
      # not-closed" — it's no data at all. Don't let a failure masquerade
      # as a clean result; skip this D# and flag the whole sweep as degraded.
      if [[ $MERGED_RC -ne 0 || $DISC_RC -ne 0 ]]; then
        SWEEP_FAILED=true
        continue
      fi
      if [[ "$MERGED_PRS" -gt 0 && "$DISC_STATE" == "false" ]]; then
        echo "  ⚠️  D#${N} — plan references but ${MERGED_PRS} merged PR(s) already cite it — verify before spawning"
        STALE_COUNT=$((STALE_COUNT + 1))
      elif [[ "$DISC_STATE" == "true" ]]; then
        echo "  ℹ️  D#${N} — already CLOSED — plan stale, skip"
        STALE_COUNT=$((STALE_COUNT + 1))
      fi
    done
    if [[ "$SWEEP_FAILED" == "true" ]]; then
      echo "  ⚠️  Plan-staleness sweep failed for one or more plan discussions (gh call failed) — not asserting freshness"
    elif [[ "$STALE_COUNT" -eq 0 ]]; then
      echo "  ✅ Plan references look fresh — no closed/already-shipped discussions cited"
    fi
  fi

  # Surface open discussions the plan didn't mention.
  # Check gh's real exit code before any pipe (a pipeline's $? is its LAST
  # stage's status, so piping straight into `sort -u` was masking gh's own
  # failure — that's how its raw error body got word-split into fake D#s).
  OPEN_NUMS_RAW=$(gh api graphql -f query='query { repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") { discussions(first:100, states:OPEN) { nodes { number } } } }' --jq '.data.repository.discussions.nodes[].number' 2>/dev/null)
  OPEN_NUMS_RC=$?
  if [[ $OPEN_NUMS_RC -ne 0 ]]; then
    echo ""
    echo "  ⚠️  Could not fetch open discussions to check against the plan (gh exit ${OPEN_NUMS_RC})"
  else
    OPEN_NUMS=$(printf '%s\n' "$OPEN_NUMS_RAW" | sort -u)
    if [[ -n "$OPEN_NUMS" ]]; then
      MISSING=""
      for N in $OPEN_NUMS; do
        # Never trust an unvalidated token as a Discussion number — a
        # non-integer here means the previous call returned error text,
        # not data, even though its exit code came back 0.
        [[ "$N" =~ ^[0-9]+$ ]] || continue
        if ! grep -qE "D#${N}\b" "$PLAN_FILE"; then
          MISSING="${MISSING}${N} "
        fi
      done
      if [[ -n "$MISSING" ]]; then
        echo ""
        echo "  📋 Open Discussions NOT in plan: $(echo $MISSING | tr ' ' '\n' | wc -l) total"
        for N in $MISSING; do
          TITLE=$(gh api graphql -F num="$N" -f query='query($num:Int!) { repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") { discussion(number:$num) { title } } }' --jq '.data.repository.discussion.title' 2>/dev/null || echo "?")
          echo "       D#${N} ${TITLE:0:70}"
        done
      fi
    fi
  fi
else
  echo "  (no plan file — skipping staleness check)"
fi

echo ""
echo "==============================================================="
echo "Ready to drive. User redirects only."
echo "==============================================================="
