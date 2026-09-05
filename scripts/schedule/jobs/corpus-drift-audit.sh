#!/usr/bin/env bash
# corpus-drift-audit.sh — weekly corpus drift audit job.
#
# Runs python3 scripts/corpus-drift-audit.py --since 30d, writes the report
# to wiki/Corpus-Drift-Report.md and a dated JSON snapshot to
# $STATE_DIR/corpus-drift/<YYYY-MM-DD>.json.
#
# After writing the report, posts a one-line summary to the team log.
#
# Scheduled: Sunday 06:00 UTC (see scripts/schedule/jobs.yaml).
# Gate: gates.corpus_drift_audit (default false).  Set to true to enable.
# token_ceiling: 0 — pure Python, no agent spawn.
#
# Exit codes: 0 = success or gate-off, 1 = audit failure

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"

# ── 1. Control plane gate ─────────────────────────────────────────────────────
GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.corpus_drift_audit 2>/dev/null || echo "false")
if [[ "$GATE" != "true" ]]; then
  echo "[corpus-drift-audit] gate=off — skipping (set gates.corpus_drift_audit=true to enable)"
  exit 0
fi

echo "[corpus-drift-audit] gate=on — running audit"

# ── 2. Run the audit ──────────────────────────────────────────────────────────
AUDIT_OUTPUT=$(python3 "$REPO_ROOT/scripts/corpus-drift-audit.py" --since 30d 2>&1)
AUDIT_RC=$?

echo "$AUDIT_OUTPUT"

if [[ $AUDIT_RC -ne 0 ]]; then
  echo "[corpus-drift-audit] audit script exited $AUDIT_RC" >&2
  exit 1
fi

# ── 3. Extract summary line ───────────────────────────────────────────────────
HEALTHY=$(echo "$AUDIT_OUTPUT" | grep -oE 'healthy\s*:\s*[0-9]+' | grep -oE '[0-9]+' | tail -1 || echo "?")
WATCH=$(echo "$AUDIT_OUTPUT" | grep -oE 'watch\s*:\s*[0-9]+' | grep -oE '[0-9]+' | tail -1 || echo "?")
DRIFT=$(echo "$AUDIT_OUTPUT" | grep -oE 'drift\s*:\s*[0-9]+' | grep -oE '[0-9]+' | tail -1 || echo "?")
NA=$(echo "$AUDIT_OUTPUT" | grep -oE 'n/a\s*:\s*[0-9]+' | grep -oE '[0-9]+' | tail -1 || echo "?")

SUMMARY="corpus-drift-audit complete: healthy=$HEALTHY watch=$WATCH drift=$DRIFT n/a=$NA"
echo "[corpus-drift-audit] $SUMMARY"

# ── 4. Post to team log ───────────────────────────────────────────────────────
bash "$REPO_ROOT/scripts/rotate-team-log.sh" comment \
  "[$(date -u +%H:%M)] corpus-drift-audit: $SUMMARY" 2>/dev/null \
  || echo "[corpus-drift-audit] WARN: failed to post to team log (non-fatal)" >&2

echo "[corpus-drift-audit] done"
exit 0
