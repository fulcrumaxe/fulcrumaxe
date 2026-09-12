#!/usr/bin/env bash
# scripts/sweep-label-lane.sh — report-only sweep: name the agent_run that was
# live when each merge-gate PASS label on a PR was applied. (D#2550 PR 2)
#
# WHAT THIS IS
# ------------
# For a given PR, reads its GitHub timeline once, finds every `labeled` event
# whose label is a key of MERGE_GATE_LABEL_LANE (scripts/lib/merge-gate-labels.sh),
# and reports one of three outcomes per label:
#
#   match          the role live in agent_run at the label's timestamp is the
#                  label's lane role, or "team-lead" (the documented exemption
#                  merge_gate_check_label_lane also honors)
#   mismatch       no lane-eligible role was live, but exactly one other
#                  known, non-orphan role was — that role is named
#   unattributable no agent_run row covers the label's timestamp for this PR,
#                  no lane-eligible role was live and more than one other
#                  role disagrees on who was, or the only role live is the
#                  tracker's own "orphan-unmatched" stamp
#
# Reviewer roles in this architecture run concurrently by design — a real PR
# can have code-reviewer, security-reviewer and acceptance-tester windows all
# overlapping for minutes, each applying only its own label. So more than one
# role being live at a label's timestamp is normal, not ambiguity: whenever
# the label's OWN lane role (or team-lead) was among the roles live at that
# moment, that is the match, regardless of which other roles also happened to
# be running. Ambiguity is reserved for the case that actually has no honest
# answer — no lane-eligible role live, and two or more *other*, different
# roles disagreeing about who was.
#
# Report-only: this script never applies, removes, or edits a label, never
# merges or closes anything, and never blocks. It is a detector, not a gate.
#
# WHY THE JOIN KEY IS agent_run.role, NOT THE TIMELINE ACTOR
# ------------------------------------------------------------
# Every role authenticates as the same GitHub identity — three specialists
# independently measured this across 33 `labeled` events on 15 merged PRs and
# found one constant actor, zero variance. A check keyed on that actor
# discriminates nothing; it can't tell one role's label apply from another's.
# So this script deliberately never reads a timeline event's actor. What it
# reads instead is agent_run.role, written by the Team Lead's own spawn
# wrapper via its start_run() call in stats.duckdb *before* the agent runs,
# from the Team Lead's own --role flag — not something the running agent
# asserts about itself. backend/agent_run_tracker.py protects that value with two
# invariants this sweep depends on:
#   no-clobber  — role/discussion are absent from complete_run()'s DO UPDATE
#                 SET, so a role recorded at start_run() time always wins.
#   no-guessing — an unrecognised role is stamped "orphan-unmatched" rather
#                 than invented; this script treats that stamp as
#                 unattributable, never as a match.
#
# NOT built on: a new WORKTREE_ID-shaped env var (readable by the very agent
# that would need to forge it) or .autonomous-team/worktrees.json (0 entries
# in production — nothing but test fixtures ever call
# worktree_registry register()). Both are the same defect this Discussion
# diagnosed, respelled, and neither is a join source here.
#
# COST SHAPE
# ----------
# Bash + one REST call per PR + one read-only DuckDB query. No agent spawn:
# cost-analyst priced a daily sweep-AGENT shape at roughly 350M cache-read
# tokens/month against ~915 zero-agent-token REST calls/month for the same
# job run as bash. This script is meant to be invoked from an existing sweep
# slot, the merging phase of a loop step, or by hand — never from a new cron
# entry, and this script itself never spawns anything: no call to the spawn
# wrapper, no loop trigger, no interactive model invocation, no orchestration
# tool call of any kind.
#
# UNTRUSTED INPUT
# ----------------
# The code plane accepts outside contributions, so a PR's title, body, and
# branch name are attacker-influenced text. This script never reads any of
# them. The only fields it takes off a timeline event are the event type, the
# label name, and the label's timestamp — and the only fields it ever prints
# are a fixed allowlist: PR number, label name, label timestamp, resolved
# role, agent_run id, outcome. Findings are meant for the Discussion plane;
# this script performs no write to the code plane at all.
#
# USAGE
# -----
#   bash scripts/sweep-label-lane.sh --pr <PR_NUMBER>
#
# One line of output per merge-gate pass label found on the PR's timeline:
#   pr=<n> label=<label> ts=<iso8601> role=<role|(none)> agent_run=<id|(none)> outcome=<match|mismatch|unattributable>
#
# EXIT CODES
# ----------
#   0  ran; no label reported mismatch or unattributable (including the case
#      where the PR carries no merge-gate pass label at all — nothing to
#      attribute is not a failure)
#   1  ran; at least one label reported mismatch
#   2  usage error (missing/non-numeric --pr)
#   3  could not read the PR timeline (gh api failed or returned nothing)
#   4  ran; no mismatch, but at least one label reported unattributable —
#      kept distinct from 0 so "ran, found nothing to attribute" is
#      distinguishable from "ran, clean" (D#2550 AC 8)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# shellcheck source=lib/merge-gate-labels.sh
source "$SCRIPT_DIR/lib/merge-gate-labels.sh"

_sweep_label_lane_usage() {
  echo "usage: sweep-label-lane.sh --pr <PR_NUMBER>" >&2
}

PR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)
      PR="${2:-}"
      shift 2
      ;;
    -h|--help)
      _sweep_label_lane_usage
      exit 0
      ;;
    *)
      echo "sweep-label-lane: unrecognized argument: $1" >&2
      _sweep_label_lane_usage
      exit 2
      ;;
  esac
done

if [[ -z "$PR" || ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "sweep-label-lane: --pr <PR_NUMBER> is required and must be a positive integer" >&2
  _sweep_label_lane_usage
  exit 2
fi

CODE_REPO="$(_require_code_repo "sweep-label-lane")" || exit 1

# ── One timeline call, outside any per-label loop (AC 11) ───────────────────
TIMELINE_JSON="$(gh api "repos/${CODE_REPO}/issues/${PR}/timeline" --paginate 2>/dev/null)"
if [[ -z "$TIMELINE_JSON" ]]; then
  echo "sweep-label-lane: could not read timeline for PR #${PR} (gh api failed or returned nothing)" >&2
  exit 3
fi

# ── Lane map as JSON, built once from the single source of truth ────────────
# (scripts/lib/merge-gate-labels.sh's MERGE_GATE_LABEL_LANE, sourced above —
# this script never restates which role owns which label).
_sweep_lane_json() {
  local first=1 out="{"
  local label
  for label in "${!MERGE_GATE_LABEL_LANE[@]}"; do
    [[ $first -eq 1 ]] || out+=","
    out+="\"${label}\":\"${MERGE_GATE_LABEL_LANE[$label]}\""
    first=0
  done
  out+="}"
  printf '%s' "$out"
}
LANE_JSON="$(_sweep_lane_json)"

SCRATCH="$(mktemp -d)" || {
  echo "sweep-label-lane: mktemp -d failed" >&2
  exit 1
}
trap 'rm -rf "$SCRATCH"' EXIT

TIMELINE_FILE="$SCRATCH/timeline.json"
printf '%s' "$TIMELINE_JSON" > "$TIMELINE_FILE"

JOIN_SCRIPT="$SCRATCH/join.py"
cat > "$JOIN_SCRIPT" <<'PYEOF'
# Joins a PR's `labeled` timeline events (already filtered to merge-gate pass
# labels by the caller's LANE map) against agent_run rows for that PR, and
# prints the fixed-allowlist report described in sweep-label-lane.sh's header.
#
# Everything this reads comes from environment variables and files the caller
# (sweep-label-lane.sh) wrote — never from stdin, so there is no ambiguity
# between "the program" and "the data" on the same stream.
import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.environ["SWEEP_REPO_ROOT"]
sys.path.insert(0, REPO_ROOT)

from backend import state_paths  # noqa: E402

# Mirrors backend/agent_run_tracker.py's own _ORPHAN_ROLE constant. Not
# imported from there: that module's import surface pulls in duckdb-writer
# machinery this read-only report has no use for, and the value is a stable,
# documented part of the no-guessing invariant this sweep depends on.
ORPHAN_ROLE = "orphan-unmatched"

PR = int(os.environ["SWEEP_PR"])
LANE = json.loads(os.environ["SWEEP_LANE_JSON"])

with open(os.environ["SWEEP_TIMELINE_FILE"], "r", encoding="utf-8") as fh:
    timeline = json.load(fh)


def _labeled_events(events):
    """Yield (label_name, created_at) for each `labeled` event whose label is
    a merge-gate pass label. Reads exactly these three fields off each event
    — event type, label.name, created_at — and nothing else. A PR's title,
    body, branch name, or any other timeline field never reaches this
    function or anything downstream of it (AC 12)."""
    if not isinstance(events, list):
        return
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "labeled":
            continue
        label = ev.get("label")
        if not isinstance(label, dict):
            continue
        name = label.get("name")
        ts = ev.get("created_at")
        if isinstance(name, str) and name in LANE and isinstance(ts, str) and ts:
            yield name, ts


def _parse_ts(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


events = list(_labeled_events(timeline))

rows = []
db = state_paths.STATS_DB
if events and db.exists():
    try:
        import duckdb  # noqa: PLC0415

        conn = duckdb.connect(str(db), read_only=True)
        try:
            cur = conn.execute(
                "SELECT agent_id, role, start_ts, end_ts FROM agent_run"
                " WHERE pr = ? ORDER BY start_ts",
                [PR],
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        rows = []

now = datetime.now(timezone.utc)
parsed_rows = [
    (
        agent_id,
        role,
        _parse_ts(start_ts),
        _parse_ts(end_ts) if end_ts is not None else now,
    )
    for agent_id, role, start_ts, end_ts in rows
]

def _nearest(candidates):
    """Among (agent_id, role, start_ts) candidates, name the one that started
    most recently at-or-before the label — the run most likely still live and
    closest to the event."""
    return max(candidates, key=lambda c: c[2])[0]


results = []
for label, ts in events:
    labeled_at = _parse_ts(ts)
    # start_ts <= labeled_at <= coalesce(end_ts, now()) — the join the Spec
    # specifies. An open (end_ts IS NULL) row is treated as still live.
    candidates = [
        (agent_id, role, start_ts)
        for agent_id, role, start_ts, end_ts in parsed_rows
        if start_ts <= labeled_at <= end_ts
    ]

    if not candidates:
        # No agent_run row at all — the honest third outcome, never a silent
        # pass (AC 8).
        results.append((label, ts, None, None, "unattributable"))
        continue

    lane_role = LANE.get(label)

    # This architecture runs its reviewer roles concurrently by design — the
    # real PR #189 run has code-reviewer, security-reviewer and
    # acceptance-tester windows all overlapping each other for minutes at a
    # time, each applying only its own label. So more than one role having a
    # live window at a label's timestamp is normal, not evidence of
    # ambiguity: check first whether the label's OWN lane role (or
    # "team-lead", the documented exemption) was among the roles live at that
    # moment. If so, that is the match, regardless of which other roles also
    # happened to be running.
    eligible = [c for c in candidates if c[1] == lane_role or c[1] == "team-lead"]
    if eligible:
        # Prefer naming the lane role itself over team-lead when both
        # happened to be live; within a role, prefer the nearest start.
        lane_hits = [c for c in eligible if c[1] == lane_role]
        pool = lane_hits or eligible
        role = pool[0][1]
        agent_id = _nearest(pool)
        results.append((label, ts, role, agent_id, "match"))
        continue

    # No eligible role was live. What else was live decides mismatch vs
    # unattributable — never a coin flip between two different wrong
    # answers, and an orphaned row is never invented into a real one
    # (no-guessing).
    non_orphan = [c for c in candidates if c[1] != ORPHAN_ROLE]
    if not non_orphan:
        # Only orphan-unmatched rows were live.
        agent_id = _nearest(candidates)
        results.append((label, ts, ORPHAN_ROLE, agent_id, "unattributable"))
        continue

    distinct_wrong_roles = {c[1] for c in non_orphan}
    if len(distinct_wrong_roles) > 1:
        # Two or more different roles, neither the lane role, disagree on
        # who was live. Not a coin flip.
        results.append((label, ts, None, None, "unattributable"))
        continue

    role = next(iter(distinct_wrong_roles))
    agent_id = _nearest([c for c in non_orphan if c[1] == role])
    results.append((label, ts, role, agent_id, "mismatch"))

exit_code = 0
for label, ts, role, agent_id, outcome in results:
    role_field = role if role else "(none)"
    agent_field = agent_id if agent_id else "(none)"
    print(
        f"pr={PR} label={label} ts={ts} role={role_field} "
        f"agent_run={agent_field} outcome={outcome}"
    )
    if outcome == "mismatch":
        exit_code = 1
    elif outcome == "unattributable" and exit_code == 0:
        exit_code = 4

sys.exit(exit_code)
PYEOF

SWEEP_REPO_ROOT="$REPO_ROOT" SWEEP_PR="$PR" SWEEP_LANE_JSON="$LANE_JSON" SWEEP_TIMELINE_FILE="$TIMELINE_FILE" \
  python3 "$JOIN_SCRIPT"
exit $?
