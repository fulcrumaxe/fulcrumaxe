#!/usr/bin/env bash
# loop-bootstrap/scripts/start-the-day.sh — morning ritual for Team Lead.
#
# Project-agnostic version installed by bootstrap.sh.
# Reads project identity from .autonomous-team/project.json.
#
# Run this at session start. It:
#   1. Pulls the project's default branch fresh
#   2. Verifies external state dir survived overnight
#   3. Runs morning sweeps (budget, open PRs, SPEC_READY count)
#   4. Prints the day's plan
#
# Usage:
#   bash scripts/start-the-day.sh
#   bash scripts/start-the-day.sh --no-sweeps

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── Load repo-resolve helper (repo slug, owner/name split) ───────────────────
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
REPO_SLUG="$(_resolve_repo)"
REPO_OWNER="${REPO_SLUG%%/*}"
REPO_NAME="${REPO_SLUG##*/}"

# ── Load state-dir helper (AUTONOMOUS_TEAM_STATE_DIR from project.json) ───────
# shellcheck source=scripts/lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh"

SKIP_SWEEPS=false
for arg in "$@"; do
  case "$arg" in
    --no-sweeps) SKIP_SWEEPS=true ;;
  esac
done

# ── Load project.json ─────────────────────────────────────────────────────────
PROJECT_JSON="$REPO_ROOT/.autonomous-team/project.json"
if [[ ! -f "$PROJECT_JSON" ]]; then
  echo "ERROR: .autonomous-team/project.json not found — run bootstrap.sh first" >&2
  exit 1
fi

PROJECT_NAME=$(python3 -c "import json; d=json.load(open('$PROJECT_JSON')); print(d.get('project_name','unknown'))" 2>/dev/null || echo "unknown")
STATE_DIR=$(python3 -c "import json; d=json.load(open('$PROJECT_JSON')); print(d.get('state_dir',''))" 2>/dev/null || echo "")

# Fall back to env var if project.json doesn't have state_dir
if [[ -z "$STATE_DIR" ]]; then
  STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.${PROJECT_NAME}-state}"
fi

# Detect default branch: 1) remote HEAD, 2) project.json branch_pattern, 3) "main"
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || true)
if [[ -z "$DEFAULT_BRANCH" ]]; then
  # Try project.json's default_branch field (explicit override)
  DEFAULT_BRANCH=$(python3 -c "
import json, sys
try:
    d = json.load(open('$PROJECT_JSON'))
    print(d.get('default_branch', '') or '')
except Exception:
    print('')
" 2>/dev/null || true)
fi
if [[ -z "$DEFAULT_BRANCH" ]]; then
  DEFAULT_BRANCH="main"
fi

echo "==============================================================="
echo "Team Lead — Start the Day"
echo "Project: $PROJECT_NAME"
echo "Repo: ${REPO_SLUG:-<not set>}"
echo "Date: $(date '+%Y-%m-%d %H:%M %Z')"
echo "==============================================================="

# ── 0. Loop run-readiness check ──────────────────────────────────────────────
echo ""
echo "## 0. Loop run-readiness"
echo ""

LOOP_METRICS_PATH="$REPO_ROOT/.autonomous-team/loop-metrics.jsonl"
# Create placeholder if missing — bootstrap.sh should have done this, but
# defend against installations that ran bootstrap before this fix.
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
      echo "  WARNING: loop-metrics.jsonl: could not parse last timestamp"
    elif [[ "$AGE_MIN" -lt 60 ]]; then
      echo "  OK: Last /loop iteration: ${AGE_MIN}m ago"
    elif [[ "$AGE_MIN" -lt 180 ]]; then
      echo "  WARNING: Last /loop iteration: ${AGE_MIN}m ago — getting stale"
      echo "      -> Run canonical 8-step /loop now"
    else
      echo "  RED: Last /loop iteration: ${AGE_MIN}m ago — STALE"
      echo "      -> Required: run canonical 8-step /loop as Team Lead"
    fi
  else
    echo "  WARNING: loop-metrics.jsonl exists but last line unreadable"
  fi
else
  echo "  WARNING: loop-metrics.jsonl missing — no /loop iterations have recorded"
  echo "      -> Run canonical 8-step /loop as Team Lead to bootstrap"
fi

echo ""
echo "  How /loop runs: 8 canonical steps from CLAUDE.md, executed by Team Lead."
echo "  Steps: 0=preflight, 1=repo, 2=team-log, 3=Discussions, 4=PRs,"
echo "         5=ACT, 6=auto-merge, 7=heartbeat, 8=now.md update."

# ── 1. Pull default branch fresh ─────────────────────────────────────────────
echo ""
echo "## 1. Sync to fresh $DEFAULT_BRANCH"

git fetch origin "$DEFAULT_BRANCH" -q 2>&1 | tail -2 || true
BRANCH=$(git branch --show-current 2>/dev/null || echo "")
if [[ "$BRANCH" != "$DEFAULT_BRANCH" ]]; then
  echo "  HEAD was on '$BRANCH' — restoring to $DEFAULT_BRANCH"
  git symbolic-ref HEAD "refs/heads/$DEFAULT_BRANCH" 2>/dev/null || true
fi
git reset --mixed "origin/$DEFAULT_BRANCH" >/dev/null 2>&1 || true
echo "  HEAD: $(git rev-parse --short HEAD 2>/dev/null) — $(git log -1 --format=%s 2>/dev/null)"

# ── 2. Verify external state dir intact ──────────────────────────────────────
echo ""
echo "## 2. External state dir health"

if [[ -d "$STATE_DIR" ]]; then
  echo "  OK: External state dir: $STATE_DIR"
  for f in stats.duckdb state.db audit.jsonl; do
    if [[ -e "$STATE_DIR/$f" ]]; then
      size=$(du -sh "$STATE_DIR/$f" 2>/dev/null | cut -f1 || echo "?")
      echo "    $f: $size"
    else
      echo "    WARNING: $f: MISSING"
    fi
  done
else
  echo "  ERROR: External state dir does not exist: $STATE_DIR"
  echo "  Run: bash scripts/setup-state-dir.sh"
fi

echo ""
echo "  Symlinks in .autonomous-team/:"
for f in stats.duckdb state.db audit.jsonl; do
  if [[ -L "$REPO_ROOT/.autonomous-team/$f" ]]; then
    target=$(readlink "$REPO_ROOT/.autonomous-team/$f")
    echo "    OK: $f -> $target"
  elif [[ -e "$REPO_ROOT/.autonomous-team/$f" ]]; then
    echo "    WARNING: $f exists as REAL file (not symlink)"
  else
    echo "    -   $f: not present"
  fi
done

# ── 3. Morning sweeps ─────────────────────────────────────────────────────────
echo ""
echo "## 3. Morning sweeps"

if [[ "$SKIP_SWEEPS" == "true" ]]; then
  echo "  (skipped via --no-sweeps)"
else
  if [[ -n "$REPO_SLUG" ]]; then
    echo ""
    echo "  Open PRs:"
    gh pr list --repo "$REPO_SLUG" --state open \
      --json number,title,labels --limit 20 2>/dev/null \
      | python3 -c "
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
" 2>/dev/null || echo "    (gh pr list failed)"

    echo ""
    echo "  SPEC_READY Discussions (count):"
    # hasDiscussionsEnabled rides along on the same query (zero extra round
    # trips) because a repo with Discussions turned off returns the exact
    # same shape as a genuinely empty queue -- {"nodes":[]}, no GraphQL
    # error -- so "0 SPEC_READY" used to read identically for "nothing's
    # ready yet" and "Discussions was never enabled" (D#2217). Distinguish
    # them explicitly instead of reporting a disabled feature as an empty
    # queue.
    SPEC_READY_RESULT=$(gh api graphql \
      -f query="query { repository(owner:\"${REPO_OWNER}\", name:\"${REPO_NAME}\") { hasDiscussionsEnabled discussions(first:50, states:OPEN) { nodes { number title body } } } }" \
      --jq 'if .data.repository.hasDiscussionsEnabled == false then "DISABLED" else ([.data.repository.discussions.nodes[] | select(.body | contains("STATUS:SPEC_READY"))] | length | tostring) end' \
      2>/dev/null)
    if [[ -z "$SPEC_READY_RESULT" ]]; then
      echo "    (graphql failed)"
    elif [[ "$SPEC_READY_RESULT" == "DISABLED" ]]; then
      echo "    warning: GitHub Discussions is DISABLED for this repo -- that, not an empty queue, is why this reads 0."
      REPO_ENABLE_HINT="repos/${REPO_OWNER}/${REPO_NAME}"
      echo "        Enable it: gh api -X PATCH ${REPO_ENABLE_HINT} -F has_discussions=true"
    else
      echo "    ${SPEC_READY_RESULT} SPEC_READY"
    fi
  else
    echo "  WARNING: repo not set in project.json — skipping GitHub sweeps"
  fi

  echo ""
  echo "  Stats freshness:"
  DUCKDB_PATH="$STATE_DIR/stats.duckdb"
  if [[ -f "$DUCKDB_PATH" ]]; then
    python3 -c "
import duckdb
try:
    conn = duckdb.connect('$DUCKDB_PATH', read_only=True)
    r = conn.execute('SELECT COUNT(DISTINCT metric), MAX(ts) FROM metric_event').fetchone()
    print(f'    {r[0]} unique metrics, latest write: {r[1]}')
except Exception as e:
    print(f'    (duckdb read failed: {e})')
" 2>/dev/null || echo "    (duckdb not available)"
  else
    echo "    (no stats.duckdb found in $STATE_DIR)"
  fi
fi

# ── Auto-generate today's plan if absent ─────────────────────────────────────
TODAY=$(date '+%Y-%m-%d')
PLAN_TODAY="$REPO_ROOT/.autonomous-team/PLAN-${TODAY}.md"
if [[ ! -f "$PLAN_TODAY" ]]; then
  if [[ -f "$SCRIPT_DIR/generate-initial-plan.py" ]]; then
    echo ""
    echo "  No plan for today — generating from open Discussions..."
    PLAN_ARGS=("$REPO_ROOT" "--date" "$TODAY")
    [[ -n "$REPO_SLUG" ]] && PLAN_ARGS+=("--repo" "$REPO_SLUG")
    python3 "$SCRIPT_DIR/generate-initial-plan.py" "${PLAN_ARGS[@]}" 2>&1 | sed 's/^/  /' || true
  fi
fi

# ── 4. The day's plan ────────────────────────────────────────────────────────
echo ""
echo "## 4. Today's Plan"
echo ""

if [[ -f "$PLAN_TODAY" ]]; then
  PLAN_FILE="$PLAN_TODAY"
else
  PLAN_FILE=$(ls -t "$REPO_ROOT/.autonomous-team"/PLAN-*.md 2>/dev/null | head -1 || echo "")
fi

if [[ -n "$PLAN_FILE" && -f "$PLAN_FILE" ]]; then
  echo "  Plan: $PLAN_FILE"
  echo ""
  cat "$PLAN_FILE"
else
  echo "  WARNING: No PLAN-*.md file found in .autonomous-team/"
  echo "  Team Lead should generate one for today before acting."
fi

# ── 5. Plan staleness check ──────────────────────────────────────────────────
echo ""
echo "## 5. Plan staleness check"
echo ""

if [[ -n "${PLAN_FILE:-}" && -f "$PLAN_FILE" && -n "$REPO_SLUG" ]]; then
  PLAN_DISCS=$(grep -oE 'D#[0-9]+' "$PLAN_FILE" 2>/dev/null | sort -u | sed 's/D#//' || echo "")

  if [[ -z "$PLAN_DISCS" ]]; then
    echo "  (no D#NNN references in plan — nothing to verify)"
  else
    STALE_COUNT=0
    for N in $PLAN_DISCS; do
      DISC_STATE=$(gh api graphql -F num="$N" \
        -f query="query(\$num:Int!) { repository(owner:\"${REPO_OWNER}\", name:\"${REPO_NAME}\") { discussion(number:\$num) { closed } } }" \
        --jq '.data.repository.discussion.closed' 2>/dev/null || echo "unknown")
      if [[ "$DISC_STATE" == "true" ]]; then
        echo "  INFO: D#${N} — already CLOSED — plan stale, skip"
        STALE_COUNT=$((STALE_COUNT + 1))
      fi
    done
    if [[ "$STALE_COUNT" -eq 0 ]]; then
      echo "  OK: Plan references look fresh"
    fi
  fi
else
  echo "  (no plan file or repo not set — skipping staleness check)"
fi

echo ""
echo "==============================================================="
echo "Ready to drive. User redirects only."
echo "==============================================================="
