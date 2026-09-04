#!/usr/bin/env bash
# scripts/sweep-loop-runs.sh — retention sweep for .autonomous-team/loop-runs/<project>/*.log
#
# Deletes .log files with mtime older than policies.loop_runs.retention_days (default 30).
# Idempotent — exits 0 when the directory is missing or empty.
#
# Usage:
#   bash scripts/sweep-loop-runs.sh [--dry-run] [--runs-dir <path>]
#
# Options:
#   --dry-run     List what would be removed without making changes.
#   --runs-dir    Override the loop-runs directory path (default: auto-detected).
#
# Note: .autonomous-team/loop-runs/ files are runtime caches (not project files),
# so the archive-not-delete rule does NOT apply here. Delete is correct — consistent
# with sweep-hook-events.sh and backend/replay.py _prune().

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=""
RUNS_DIR_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN="1"; shift ;;
    --runs-dir)   RUNS_DIR_OVERRIDE="$2"; shift 2 ;;
    *) echo "[sweep-loop-runs] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Resolve retention_days from control plane (fall back to 30) ──────────────

RETENTION_DAYS="$(python3 "$REPO_ROOT/backend/control_plane.py" get policies.loop_runs.retention_days 2>/dev/null | tr -d '"' || true)"
if [[ -z "$RETENTION_DAYS" ]] || ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  RETENTION_DAYS=30
fi

# ── Locate loop-runs directory ────────────────────────────────────────────────

if [[ -n "$RUNS_DIR_OVERRIDE" ]]; then
  RUNS_DIR="$RUNS_DIR_OVERRIDE"
else
  RUNS_DIR="$REPO_ROOT/.autonomous-team/loop-runs"
fi

if [[ ! -d "$RUNS_DIR" ]]; then
  echo "[sweep-loop-runs] loop-runs directory not found: $RUNS_DIR — nothing to sweep"
  exit 0
fi

# ── Safety cap ────────────────────────────────────────────────────────────────
MAX_DELETIONS=5000
deleted=0

dry() { [[ "$DRY_RUN" == "1" ]]; }

echo "[sweep-loop-runs] Sweeping $RUNS_DIR (retention: ${RETENTION_DAYS}d)"

# ── Delete *.log files older than retention_days, across all project subdirs ──

while IFS= read -r -d '' log_file; do
  if [[ $deleted -ge $MAX_DELETIONS ]]; then
    echo "[sweep-loop-runs] WARN: hit safety cap ($MAX_DELETIONS); will finish on next run"
    break
  fi

  if dry; then
    echo "[sweep-loop-runs] DRY-RUN: would delete: ${log_file#"$RUNS_DIR/"}"
  else
    echo "[sweep-loop-runs] delete: ${log_file#"$RUNS_DIR/"}"
    rm -f "$log_file" 2>/dev/null || true
  fi
  ((deleted++)) || true
done < <(find "$RUNS_DIR" -mindepth 2 -maxdepth 2 -type f -name "*.log" \
           -mtime +"$RETENTION_DAYS" -print0 2>/dev/null || true)

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if dry; then
  echo "[sweep-loop-runs] DRY-RUN summary (no changes made):"
else
  echo "[sweep-loop-runs] Sweep complete:"
fi
echo "  log files removed: $deleted  (older than ${RETENTION_DAYS}d)"
