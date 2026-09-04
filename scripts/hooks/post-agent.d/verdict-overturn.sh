#!/usr/bin/env bash
# scripts/hooks/post-agent.d/verdict-overturn.sh — Verdict-overturn producer.
#
# Sourced by post-agent-hook.sh AFTER the complete_run step, so agent_run has
# this run's final verdict.
#
# Detects when the current run's verdict (needs-fix or fail) on a PR contradicts
# an earlier pass/done verdict from a DIFFERENT role on the same PR.  When
# detected, records one downstream_needs_fix overturn event via verdict_overturn.py.
#
# Expects these variables to be set by the caller:
#   REPO_ROOT   — absolute path to repo root
#   PR          — PR number (may be empty; script no-ops when absent)
#   ROLE        — current agent's role
#   VERDICT     — current agent's verdict
#
# Only the downstream_needs_fix kind is emitted in PR1.

# No-op if no PR number
[[ -z "${PR:-}" ]] && return 0

# Only fire when current verdict indicates a contradiction
case "${VERDICT:-}" in
  needs-fix|fail) ;;
  *) return 0 ;;
esac

# Look up whether a DIFFERENT role previously marked pass/done on this same PR.
# Query the agent_run table in stats.duckdb.
_VO_RESULT=$(python3 - "${REPO_ROOT}" "${PR}" "${ROLE}" <<'_PYEOF' 2>/dev/null || true)
import sys, json

repo_root, pr_str, current_role = sys.argv[1:4]

try:
    import sys as _sys
    _sys.path.insert(0, repo_root)
    from pathlib import Path
    from backend.stats_writer import _db_path
    from backend.stats_connection import get_read_connection
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(0)

db = _db_path()
if not Path(str(db)).exists():
    sys.exit(0)

try:
    pr_int = int(pr_str)
except ValueError:
    sys.exit(0)

try:
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, verdict, agent_id
            FROM agent_run
            WHERE pr = ?
              AND verdict IN ('pass', 'done')
              AND role != ?
              AND end_ts IS NOT NULL
            ORDER BY end_ts ASC
            """,
            [pr_int, current_role],
        ).fetchall()
    finally:
        conn.close()
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(0)

if not rows:
    sys.exit(0)

# Return the earliest prior pass/done from a different role
prior_role, prior_verdict, prior_agent_id = rows[0]
print(json.dumps({
    "prior_role": prior_role,
    "prior_verdict": prior_verdict,
    "prior_agent_id": prior_agent_id,
}))
_PYEOF

if [[ -z "$_VO_RESULT" ]]; then
  return 0
fi

# Extract fields from JSON
_VO_PRIOR_ROLE=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('prior_role',''))" "$_VO_RESULT" 2>/dev/null || true)
_VO_PRIOR_VERDICT=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('prior_verdict',''))" "$_VO_RESULT" 2>/dev/null || true)

if [[ -z "$_VO_PRIOR_ROLE" ]]; then
  return 0
fi

# Build evidence_ref path: pr-artifacts/<pr>/<sha>.jsonl (best-effort; may not exist yet)
_VO_SHA=$(git -C "$REPO_ROOT" ls-remote origin "refs/heads/*" 2>/dev/null | head -1 | cut -c1-8 || true)
_VO_EVIDENCE_REF=".autonomous-team/pr-artifacts/${PR}/${_VO_SHA:-unknown}.jsonl"

python3 - "$REPO_ROOT" "$PR" "$_VO_PRIOR_ROLE" "$_VO_PRIOR_VERDICT" "$ROLE" "$_VO_EVIDENCE_REF" <<'_PYEOF' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
from backend.verdict_overturn import record_overturn

pr          = int(sys.argv[2])
prior_role  = sys.argv[3]
prior_v     = sys.argv[4]
contra_src  = sys.argv[5]
evidence    = sys.argv[6]

record_overturn(
    pr=pr,
    prior_role=prior_role,
    prior_verdict=prior_v,
    contradicting_source=contra_src,
    kind="downstream_needs_fix",
    evidence_ref=evidence,
)
print(f"[verdict-overturn] recorded downstream_needs_fix: {prior_role} pr={pr}")
_PYEOF
