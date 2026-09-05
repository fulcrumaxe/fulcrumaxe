#!/usr/bin/env bash
# scripts/sweep-stale-worktrees.sh — remove safely-eligible stale git worktrees.
#
# Predicate for "safe to remove" (ALL must hold):
#   1. > threshold commits behind origin/main (default 20, see policies.team_lead.claim_gate_stale_commits)
#   2. zero TRACKED changes (git status --short, ignoring untracked "??" lines)
#   3. not listed active in scripts/lib/worktree-registry.sh
#   4. directory mtime is at least 1 hour old
#
# Usage:
#   bash scripts/sweep-stale-worktrees.sh             # dry-run (DEFAULT): print candidates, zero changes
#   bash scripts/sweep-stale-worktrees.sh --dry-run   # explicit dry-run, same as bare invocation
#   bash scripts/sweep-stale-worktrees.sh --apply     # actually remove eligible worktrees
#   bash scripts/sweep-stale-worktrees.sh --yes       # alias for --apply
#
# Dry-run is the default and requires no flag. Real removal is destructive
# (git worktree remove --force + git branch -D) and requires the explicit
# --apply or --yes opt-in — this default was flipped after an unplanned bulk
# deletion showed that a wrapped script invoked bare is invisible to the
# sandbox hook's literal-command pattern matching (see D#1616 security review,
# tracked separately as D#1625). Never make real removal the bare-argument
# behavior again.
#
# Every REAL removal (--apply/--yes only) appends one row to the state-dir
# audit.jsonl (same convention as scripts/merge-and-hook.sh) so a bulk removal
# is forensically reconstructable after the fact.
#
# Worktrees with real tracked changes are never removed — they are listed in the
# summary output (and, on a real run, appended to an audit report under
# archive/stale-worktree-audit-2026-07-06/) for human/PM salvage-vs-discard review.
#
# This script is deliberately idempotent: a second consecutive real run should
# report 0 removals once the first run has cleared everything eligible.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# D#1809 Lane A — resolve self-exclusion BEFORE `cd "$REPO_ROOT"` below.
# _wtr_resolve_self_root (scripts/lib/worktree-registry.sh) calls
# `git rev-parse --show-toplevel` with no -C, so it depends on the cwd this
# script was invoked from. Resolving it after the cd would always see
# REPO_ROOT as "self" and never detect that the sweep is running from
# inside one of the worktrees it is scanning.
export _WTR_REPO_ROOT="$REPO_ROOT"
# shellcheck source=scripts/lib/worktree-registry.sh
source "$REPO_ROOT/scripts/lib/worktree-registry.sh" || {
  echo "[sweep-stale-worktrees] FATAL: cannot source worktree-registry.sh — refusing to run without the self-exclusion guard" >&2
  exit 1
}
_wtr_resolve_self_root

cd "$REPO_ROOT" || exit 1

# SECURITY: dry-run is the default. Real removal requires an explicit
# --apply or --yes flag. Never flip this default without an opt-in flag —
# see the usage block above for why (D#1616 security review).
DRY_RUN=true
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --apply|--yes) DRY_RUN=false ;;
    *) ;;
  esac
done

# D#1819: scripts/lib/worktree-claims.sh is the shared staleness definition
# consumed by both this sweep and the --touchpoints gate in spawn-agent.sh.
# IMPORTANT — the removal predicate below is deliberately UNCHANGED: it still
# uses only the commits-behind test (STALE_THRESHOLD), not the module's
# broader MERGED/wall-clock-STALE classification. Widening what this script
# actually *removes* is explicitly out of scope for D#1819 — see the Spec's
# "Explicitly out of scope" section. The module is sourced here only to (a)
# reuse the same threshold lookup instead of duplicating it, and (b) print an
# informational-only census line below so an operator can see how many more
# worktrees the shared definition would flag, without any of them being
# eligible for removal by this run.
# shellcheck source=scripts/lib/worktree-claims.sh
source "$REPO_ROOT/scripts/lib/worktree-claims.sh"

STALE_THRESHOLD="$(wtc_stale_commits_threshold)"

MIN_AGE_SECONDS=3600  # 1 hour

# D#1809 Lane A — resolved worktrees-dir root for the path-scope guard in
# _process_worktree, computed once (mirrors worktree-registry.sh:950-951).
RESOLVED_WORKTREES_DIR="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$REPO_ROOT/.claude/worktrees" 2>/dev/null || echo "$REPO_ROOT/.claude/worktrees")"

# Audit trail for real removals (CWE-778) — same convention as
# scripts/merge-and-hook.sh's audit.jsonl writes.
_AUDIT_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
_AUDIT_FILE="$_AUDIT_DIR/audit.jsonl"

echo "[sweep-stale-worktrees] threshold=${STALE_THRESHOLD} commits behind origin/main, min-age=${MIN_AGE_SECONDS}s, dry_run=${DRY_RUN}"

# ── 1. Prune dead registrations first ────────────────────────────────────────
git worktree prune 2>&1 | sed 's/^/[prune] /' || true

# ── 2. Refresh origin/main so rev-list counts are accurate ───────────────────
git fetch origin main --quiet 2>/dev/null || echo "[sweep-stale-worktrees] WARN: fetch origin main failed (using local ref)" >&2

# ── 3. Load active worktree ids from the registry (fail-closed: if the
#       registry call errors, treat NOTHING as protected-by-registry so the
#       mtime guard is the only remaining safety net — never invert this) ───
ACTIVE_IDS=""
if [[ -x "$REPO_ROOT/scripts/lib/worktree-registry.sh" ]] || [[ -f "$REPO_ROOT/scripts/lib/worktree-registry.sh" ]]; then
  ACTIVE_JSON=$(bash "$REPO_ROOT/scripts/lib/worktree-registry.sh" list --status active --json 2>/dev/null || echo "[]")
  ACTIVE_IDS=$(echo "$ACTIVE_JSON" | python3 -c "
import json, sys
try:
    entries = json.load(sys.stdin)
except Exception:
    entries = []
ids = []
for e in entries if isinstance(entries, list) else []:
    if isinstance(e, dict) and e.get('worktree_id'):
        ids.append(e['worktree_id'])
print('\n'.join(ids))
" 2>/dev/null || echo "")
fi

_is_active() {
  local wid="$1"
  [[ -z "$ACTIVE_IDS" ]] && return 1
  echo "$ACTIVE_IDS" | grep -qxF "$wid"
}

# ── 4. Walk worktrees, classify, act ─────────────────────────────────────────
REMOVED=()
SKIPPED_ACTIVE=()
SKIPPED_YOUNG=()
SKIPPED_FRESH=()
SKIPPED_DIRTY=()
SKIPPED_DIRTY_DETAIL=()

_wt_path=""
_wt_branch=""

_process_worktree() {
  local wt_path="$1" wt_branch="$2"

  [[ "$wt_path" == "$REPO_ROOT" ]] && return
  [[ ! -d "$wt_path" ]] && return

  local wt_id
  wt_id="$(basename "$wt_path")"

  if _is_active "$wt_id"; then
    SKIPPED_ACTIVE+=("$wt_id")
    return
  fi

  local now dir_mtime age
  now=$(date +%s)
  dir_mtime=$(stat -c %Y "$wt_path" 2>/dev/null || stat -f %m "$wt_path" 2>/dev/null || echo "$now")
  age=$(( now - dir_mtime ))
  if [[ "$age" -lt "$MIN_AGE_SECONDS" ]]; then
    SKIPPED_YOUNG+=("$wt_id (age=${age}s)")
    return
  fi

  local behind
  behind=$(git -C "$wt_path" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  if ! [[ "$behind" =~ ^[0-9]+$ ]]; then behind=0; fi
  if [[ "$behind" -le "$STALE_THRESHOLD" ]]; then
    SKIPPED_FRESH+=("$wt_id (behind=$behind)")
    return
  fi

  # Tracked-changes check: ignore "??" untracked lines, only M/A/D/R/C in either column.
  local tracked
  tracked=$(git -C "$wt_path" status --short 2>/dev/null | awk '{
    code=substr($0,1,2)
    if (code ~ /[MADRC]/) print
  }')
  if [[ -n "$tracked" ]]; then
    SKIPPED_DIRTY+=("$wt_id")
    SKIPPED_DIRTY_DETAIL+=("$wt_id | $wt_path | $(echo "$tracked" | tr '\n' ';' | sed 's/;$//')")
    return
  fi

  # D#1809 Lane A — path-scope and self-exclusion refusals. These run last,
  # right before the eligible action, so they gate both --apply and
  # --dry-run reporting. Mirrors scripts/lib/worktree-registry.sh:950-963
  # (path scope) and :103-155 (fail-closed self-exclusion) rather than
  # inventing a second shape of either guard.
  local resolved_wt_path
  resolved_wt_path="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$wt_path" 2>/dev/null || echo "$wt_path")"

  if [[ "$resolved_wt_path" != "${RESOLVED_WORKTREES_DIR}/"* || "$resolved_wt_path" == "$RESOLVED_WORKTREES_DIR" ]]; then
    echo "sweep-self-exclusion-refused (path outside worktrees dir): $wt_id ($resolved_wt_path)" >&2
    return
  fi

  # D#2120: already resolved above -- tell _wtr_is_self so it doesn't spawn
  # a second, redundant realpath for the same string.
  if _wtr_is_self "$resolved_wt_path" 1; then
    echo "sweep-self-exclusion-refused (self): $wt_id" >&2
    return
  fi

  # Eligible.
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would remove: $wt_id (behind=$behind, path=$wt_path, branch=$wt_branch)"
    REMOVED+=("$wt_id (behind=$behind)")
  else
    if git worktree remove --force "$wt_path" 2>&1 | sed "s/^/[remove $wt_id] /"; then
      REMOVED+=("$wt_id (behind=$behind)")
      _audit_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
      mkdir -p "$_AUDIT_DIR" 2>/dev/null || true
      printf '%s\n' "{\"kind\":\"stale_worktree_removed\",\"worktree_id\":\"$wt_id\",\"path\":\"$wt_path\",\"branch\":\"$wt_branch\",\"behind\":$behind,\"reason\":\"stale-worktree-sweep\",\"timestamp\":\"$_audit_ts\"}" >> "$_AUDIT_FILE"
      if [[ -n "$wt_branch" ]]; then
        git branch -D "$wt_branch" >/dev/null 2>&1 || true
      fi
    else
      echo "[sweep-stale-worktrees] WARN: failed to remove $wt_path" >&2
    fi
  fi
}

while IFS= read -r line; do
  if [[ "$line" =~ ^worktree\ (.+)$ ]]; then
    # Capture the match BEFORE flushing — _process_worktree runs its own
    # regex matches internally, which clobbers the global BASH_REMATCH.
    _new_wt_path="${BASH_REMATCH[1]}"
    # Flush previous entry before starting a new one.
    if [[ -n "$_wt_path" ]]; then
      _process_worktree "$_wt_path" "$_wt_branch"
    fi
    _wt_path="$_new_wt_path"
    _wt_branch=""
  elif [[ "$line" =~ ^branch\ refs/heads/(.+)$ ]]; then
    _wt_branch="${BASH_REMATCH[1]}"
  fi
done < <(git worktree list --porcelain 2>/dev/null)
if [[ -n "$_wt_path" ]]; then
  _process_worktree "$_wt_path" "$_wt_branch"
fi

# ── 5. Write dirty-worktree audit report (real runs only, non-empty) ────────
if [[ "$DRY_RUN" != "true" ]] && [[ "${#SKIPPED_DIRTY_DETAIL[@]}" -gt 0 ]]; then
  AUDIT_DIR="$REPO_ROOT/archive/stale-worktree-audit-2026-07-06"
  mkdir -p "$AUDIT_DIR"
  {
    echo "# Stale worktree audit — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
    echo "Worktrees that are stale (>${STALE_THRESHOLD} commits behind origin/main) but"
    echo "have real tracked changes. The sweep never auto-removes these — a human or"
    echo "PM must decide salvage vs. discard."
    echo ""
    for d in "${SKIPPED_DIRTY_DETAIL[@]}"; do
      echo "- $d"
    done
  } >> "$AUDIT_DIR/report.md"
fi

# ── 6. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "[sweep-stale-worktrees] summary:"
echo "  removed (or would-remove): ${#REMOVED[@]}"
echo "  skipped (active/registered): ${#SKIPPED_ACTIVE[@]}"
echo "  skipped (younger than 1h): ${#SKIPPED_YOUNG[@]}"
echo "  skipped (fresh, <= threshold behind): ${#SKIPPED_FRESH[@]}"
echo "  skipped (dirty, tracked changes): ${#SKIPPED_DIRTY[@]}"
if [[ "${#SKIPPED_DIRTY[@]}" -gt 0 ]]; then
  echo "  dirty ids: ${SKIPPED_DIRTY[*]}"
fi
echo "-${#REMOVED[@]} stale worktrees removed"

# ── 7. Informational-only: shared-definition census (D#1819) ────────────────
# Reports how many worktrees the shared MERGED|ABANDONED|STALE definition in
# scripts/lib/worktree-claims.sh (wall-clock-aware, merged-branch-aware,
# no-PR-ever-aware as of D#2155 PR-a) now sees, purely for operator
# visibility. This number is NOT fed back into REMOVED/eligibility above —
# the removal predicate stays exactly as it was before D#1819. A
# newly-visible candidate here still requires a supervised decision (or a
# future, separately-approved Spec) before it can be removed.
#
# D#2155 PR-a: this grep must list ABANDONED alongside MERGED|STALE — the
# older two-way pattern would silently undercount once wtc_classify started
# emitting a third terminal class.
_CENSUS_COUNT=$(bash "$REPO_ROOT/scripts/lib/worktree-claims.sh" census 2>/dev/null | grep -cE ' (MERGED|ABANDONED|STALE) ' || echo 0)
echo "  [informational] scripts/lib/worktree-claims.sh census: ${_CENSUS_COUNT} worktrees now classified MERGED|ABANDONED|STALE (not acted on by this run — removal predicate unchanged)"

exit 0
