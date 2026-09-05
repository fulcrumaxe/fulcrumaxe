#!/usr/bin/env bash
# scripts/sweep-stuck-prs.sh — detect stuck PRs and re-route them via spawn queue.
#
# A PR is "stuck" when it has `code-review-needs-fix` and hasn't been updated
# in STUCK_PR_THRESHOLD_MINUTES (default: 30). This sweeper:
#   1. Detects stuck PRs via scripts/lib/stuck-pr-detect.sh
#   2. Enqueues a fresh executor spawn (via spawn_queue.py enqueue) for each stuck PR
#      with respawn count < 2
#   3. On count >= 2: escalates via team-log + `needs-boss` label instead of re-enqueueing
#   4. Persists respawn counts in .autonomous-team/stuck-pr-respawns.json
#
# Usage:
#   bash scripts/sweep-stuck-prs.sh
#
# Environment:
#   STUCK_PR_THRESHOLD_MINUTES  — age threshold in minutes (default: 30)
#   DRY_RUN                     — if non-empty, print actions but do not enqueue/escalate
#
# Exits 0 always (failures are logged, not fatal).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# The CODE plane: every use of $REPO in this file is `gh pr list`.
#
# This assignment used to be `_resolve_repo`, and it also used to be silently
# discarded: sourcing lib/gh-label.sh a few lines below assigned its own
# un-namespaced REPO on top of this one, so the value actually used by
# `gh pr list --repo "$REPO"` came from gh-label.sh, not from here. Both are the
# code plane now, and gh-label.sh's variable is namespaced (_GH_LABEL_REPO), so
# this assignment is the one that survives and it is the right plane.
REPO="$(_require_code_repo "sweep-stuck-prs")" || exit 1

RESPAWNS_FILE="$REPO_ROOT/.autonomous-team/stuck-pr-respawns.json"
DRY_RUN="${DRY_RUN:-}"

# Source helpers
source "$SCRIPT_DIR/lib/gh-label.sh"
source "$SCRIPT_DIR/lib/stuck-pr-detect.sh"

log() { echo "[$(date +%H:%M:%S)] sweep-stuck-prs: $*" >&2; }
team_log() {
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$1" 2>/dev/null || true
}

# ── Known labels: any label outside this set on an open PR is flagged ────────
# Update this list whenever a new automation label is introduced.
_KNOWN_LABELS=(
  "code-review-passed"
  "code-review-needs-fix"
  "needs-re-review"
  "acceptance-passed"
  "acceptance-failed"
  "acceptance-test-passed"
  "security-passed"
  "security-needs-fix"
  "security-issue"
  "security-review-needs-fix"
  "security-review-passed"
  "security-review-triggered"
  "browser-test-passed"
  "debater-confirmed"
  "needs-boss"
  "do-not-merge"
  "wip"
  "team-log"
  "SPEC_READY"
  "DISCUSSING"
  "DONE"
  # Standard GitHub default labels
  "bug"
  "enhancement"
  "duplicate"
  "documentation"
  "good first issue"
  "help wanted"
  "invalid"
  "question"
  "wontfix"
)

_sweep_unknown_labels() {
  local open_prs
  open_prs=$(gh pr list --repo "$REPO" --state open --json number,labels 2>/dev/null || echo "[]")
  echo "$open_prs" | python3 - <<'PYEOF'
import json, sys, os

data = json.load(sys.stdin)
known = set(os.environ.get("_KNOWN_LABELS_CSV", "").split(","))

for pr in data:
    num = pr["number"]
    for lbl in pr.get("labels", []):
        name = lbl.get("name", "")
        if name and name not in known:
            print(f"[merge-gate] unknown-label pr=#{num} labels=[{name}]", flush=True)
PYEOF
}

export _KNOWN_LABELS_CSV
_KNOWN_LABELS_CSV=$(IFS=,; echo "${_KNOWN_LABELS[*]}")
_sweep_unknown_labels

# ── Ensure respawns state file exists ────────────────────────────────────────
if [ ! -f "$RESPAWNS_FILE" ]; then
  echo "{}" > "$RESPAWNS_FILE"
fi

# ── Detect stuck PRs ─────────────────────────────────────────────────────────
STUCK_JSON=$(list_stuck_prs "${STUCK_PR_THRESHOLD_MINUTES:-30}")
STUCK_COUNT=$(echo "$STUCK_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

if [ "$STUCK_COUNT" -eq 0 ]; then
  echo "0 stuck PRs found"
  exit 0
fi

log "$STUCK_COUNT stuck PR(s) found"
echo "$STUCK_COUNT stuck PRs found"

# ── Process each stuck PR ─────────────────────────────────────────────────────
# Write STUCK_JSON to a temp file to avoid the pipe-vs-heredoc stdin conflict.
STUCK_TMP=$(mktemp /tmp/stuck-prs-XXXXXX.json)
printf '%s' "$STUCK_JSON" > "$STUCK_TMP"

python3 - <<PYEOF "$RESPAWNS_FILE" "$REPO_ROOT" "$REPO" "${DRY_RUN}" "$SCRIPT_DIR" "$STUCK_TMP"
import json, os, subprocess, sys, tempfile
from datetime import datetime, timezone

respawns_file = sys.argv[1]
repo_root     = sys.argv[2]
repo          = sys.argv[3]
dry_run       = bool(sys.argv[4])
script_dir    = sys.argv[5]
stuck_tmp     = sys.argv[6]

# Load stuck PRs from temp file (avoids pipe-vs-heredoc stdin conflict)
try:
    with open(stuck_tmp) as f:
        data = json.load(f)
    os.unlink(stuck_tmp)
except Exception:
    data = []

# Load current respawn state
try:
    with open(respawns_file) as f:
        state = json.load(f)
except Exception:
    state = {}
MAX_RESPAWNS = 2  # at count >= 2, escalate instead of re-enqueue

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)

def team_log(msg):
    run(["bash", f"{script_dir}/rotate-team-log.sh", "comment", msg])

def apply_label_rest(pr, label):
    run([
        "gh", "api", "-X", "POST",
        f"repos/{repo}/issues/{pr}/labels",
        "-f", f"labels[]={label}",
        "--repo", repo,
    ])

def get_discussion_for_pr(pr_num):
    """Parse Discussion number from PR body or branch name."""
    result = run(["gh", "pr", "view", str(pr_num),
                  "--repo", repo,
                  "--json", "body,headRefName"])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        body = data.get("body", "") or ""
        branch = data.get("headRefName", "") or ""
        # Look for "Discussion #N" or "discussion-N-" pattern
        import re
        m = re.search(r'[Dd]iscussion\s*#(\d+)', body)
        if m:
            return int(m.group(1))
        m = re.search(r'discussion-(\d+)-', branch)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None

def get_reviewer_feedback(pr_num):
    """Fetch last 5 PR comments as context."""
    result = run(["gh", "pr", "view", str(pr_num),
                  "--repo", repo,
                  "--json", "comments"])
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
        comments = data.get("comments", [])[-5:]
        return "\n\n".join(c.get("body", "") for c in comments)
    except Exception:
        return ""

now_iso = datetime.now(timezone.utc).isoformat()

for pr_info in data:
    pr_num   = pr_info["number"]
    age_min  = pr_info["age_minutes"]
    key      = str(pr_num)

    entry    = state.get(key, {"count": 0, "last_respawn": None})
    count    = entry.get("count", 0)

    print(f"  PR #{pr_num}  age={age_min:.0f}min  respawns={count}")

    if count >= MAX_RESPAWNS:
        # Escalate
        msg = f"[{datetime.now().strftime('%H:%M')}] sweeper: PR #{pr_num} stuck ({count} respawns) — needs human attention"
        print(f"    -> escalate: {msg}")
        if not dry_run:
            team_log(msg)
            apply_label_rest(pr_num, "needs-boss")
        else:
            print(f"    [DRY-RUN] would post team-log and apply needs-boss label")
        continue

    # Enqueue respawn
    disc_num = get_discussion_for_pr(pr_num)
    feedback = get_reviewer_feedback(pr_num)

    # Build context string
    context_lines = [
        f"PR #{pr_num} has been stuck with code-review-needs-fix for {age_min:.0f} minutes.",
        "",
        "Reviewer feedback to address:",
        feedback or "(no comments found — check PR directly)",
        "",
        f"Fix all issues and push to the same branch.",
    ]
    if disc_num:
        context_lines.insert(0, f"Implementing Discussion #{disc_num}.")
    context_str = "\n".join(context_lines)

    if dry_run:
        print(f"    [DRY-RUN] would enqueue executor respawn for PR #{pr_num}")
        count += 1
    else:
        disc_arg = str(disc_num) if disc_num else ""
        cmd = [
            "python3", f"{repo_root}/backend/spawn_queue.py", "enqueue",
            "--requested-by", "sweep-stuck-prs",
            "--pr", str(pr_num),
        ]
        if disc_arg:
            cmd.extend(["executor", disc_arg, context_str])
        else:
            cmd.extend(["executor", context_str])

        result = run(cmd)
        if result.returncode == 0:
            print(f"    -> enqueued respawn (count now {count + 1})")
            count += 1
            team_log(f"[{datetime.now().strftime('%H:%M')}] sweeper: PR #{pr_num} stuck ({age_min:.0f}min) — enqueued executor respawn #{count}")
        else:
            print(f"    -> enqueue failed: {result.stderr.strip()[:120]}", file=sys.stderr)

    # Persist updated counter
    state[key] = {"count": count, "last_respawn": now_iso}

# Write state back atomically
import tempfile
tmp = respawns_file + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=2)
os.replace(tmp, respawns_file)
PYEOF

exit 0
