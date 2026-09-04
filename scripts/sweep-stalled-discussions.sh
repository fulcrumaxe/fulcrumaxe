#!/usr/bin/env bash
# scripts/sweep-stalled-discussions.sh — detect stalled Discussions and re-route via spawn queue.
#
# A Discussion is "stalled" when its body contains <!-- STATUS:DISCUSSING SINCE:<ISO8601> -->
# and the age since that timestamp exceeds STALLED_DISCUSSION_THRESHOLD_HOURS (default: 24).
#
# For each stalled Discussion:
#   1. Posts a one-time nudge comment (marker: <!-- stalled-sweeper:nudge -->) — idempotent
#   2. Enqueues a project-manager spawn via backend/spawn_queue.py — skips if already queued
#
# Usage:
#   bash scripts/sweep-stalled-discussions.sh
#
# Environment:
#   STALLED_DISCUSSION_THRESHOLD_HOURS  — age threshold in hours (default: 24)
#   DRY_RUN                             — if non-empty, print actions but make no writes
#
# Exits 0 always (failures are logged, not fatal).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
REPO_OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"

THRESHOLD_HOURS="${STALLED_DISCUSSION_THRESHOLD_HOURS:-24}"
DRY_RUN="${DRY_RUN:-}"

log() { echo "[$(date +%H:%M:%S)] sweep-stalled-discussions: $*" >&2; }
team_log() {
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$1" 2>/dev/null || true
}

if [ -n "$DRY_RUN" ]; then
  log "DRY-RUN mode — no GraphQL mutations or queue writes will occur" >&2
fi

# ── Fetch all open Discussions with body + comments ──────────────────────────
log "Fetching open Discussions from GitHub..."
DISC_JSON=$(gh api graphql -f query='
query {
  repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") {
    discussions(first:100, states:OPEN) {
      nodes {
        id
        number
        title
        body
        updatedAt
        comments(first:100) {
          nodes {
            body
          }
        }
      }
    }
  }
}' --jq '.data.repository.discussions.nodes' 2>/dev/null) || {
  log "ERROR: GraphQL fetch failed — exiting cleanly"
  exit 0
}

if [ -z "$DISC_JSON" ] || [ "$DISC_JSON" = "null" ]; then
  log "No open Discussions found — nothing to do"
  team_log "sweep-stalled-discussions: 0 stalled"
  exit 0
fi

# ── Parse and process via Python inline block ─────────────────────────────────
DISC_TMP=$(mktemp /tmp/stalled-disc-XXXXXX.json)
printf '%s' "$DISC_JSON" > "$DISC_TMP"

python3 - <<'PYEOF' "$REPO_ROOT" "$REPO" "$THRESHOLD_HOURS" "${DRY_RUN}" "$SCRIPT_DIR" "$DISC_TMP"
import json, os, re, subprocess, sys
from datetime import datetime, timezone, timedelta

repo_root      = sys.argv[1]
repo           = sys.argv[2]
threshold_hrs  = float(sys.argv[3])
dry_run        = bool(sys.argv[4])
script_dir     = sys.argv[5]
disc_tmp       = sys.argv[6]

NUDGE_MARKER = "<!-- stalled-sweeper:nudge -->"

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)

def team_log(msg):
    run(["bash", f"{script_dir}/rotate-team-log.sh", "comment", msg])

# Load discussions from temp file
try:
    with open(disc_tmp) as f:
        discussions = json.load(f)
    os.unlink(disc_tmp)
except Exception as e:
    print(f"ERROR loading discussions JSON: {e}", file=sys.stderr)
    discussions = []

now_utc = datetime.now(timezone.utc)
threshold_delta = timedelta(hours=threshold_hrs)

stalled = []           # list of (disc, age_hours)
nudges_posted = 0
spawns_enqueued = 0

# ── Identify stalled discussions ─────────────────────────────────────────────
STATUS_RE = re.compile(
    r'<!--\s*STATUS:DISCUSSING(?:\s+[^>]*)?\s+SINCE:([^\s>]+)', re.IGNORECASE
)

for disc in discussions:
    body = disc.get("body") or ""
    m = STATUS_RE.search(body)
    if not m:
        continue  # not in DISCUSSING state

    since_str = m.group(1).rstrip("-->").strip()
    try:
        since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
    except ValueError:
        # Fall back to updatedAt
        updated = disc.get("updatedAt") or ""
        if not updated:
            continue
        try:
            since_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue

    age = now_utc - since_dt
    if age < threshold_delta:
        continue  # not stalled yet

    stalled.append((disc, age.total_seconds() / 3600))

if not stalled:
    print("0 stalled discussions — nothing to do")
    team_log("sweep-stalled-discussions: 0 stalled")
    sys.exit(0)

# Sort by age descending so the oldest is first in the summary
stalled.sort(key=lambda x: x[1], reverse=True)
oldest_disc, oldest_hrs = stalled[0]

print(f"{len(stalled)} stalled discussion(s) found (threshold: {threshold_hrs}h)")

# ── Load current spawn queue for dedup check ─────────────────────────────────
def get_queued_discussions(role="project-manager"):
    """Return set of discussion numbers already pending/active for role."""
    queued = set()
    for subcmd in ("pending", "active"):
        result = run(["python3", f"{repo_root}/backend/spawn_queue.py", subcmd])
        if result.returncode != 0 or not result.stdout.strip():
            continue
        # Each output line may be JSON or a text representation; try JSON parse
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if item.get("role") == role and item.get("discussion") is not None:
                    queued.add(int(item["discussion"]))
            except (json.JSONDecodeError, KeyError):
                # Try to extract discussion number from text if JSON unavailable
                dm = re.search(r'"discussion":\s*(\d+)', line)
                rm = re.search(r'"role":\s*"([^"]+)"', line)
                if dm and rm and rm.group(1) == role:
                    queued.add(int(dm.group(1)))
    return queued

queued_discussions = get_queued_discussions("project-manager")

# ── Process each stalled discussion ─────────────────────────────────────────
for disc, age_hrs in stalled:
    disc_id     = disc["id"]
    disc_num    = disc["number"]
    disc_title  = disc["title"]
    age_h       = int(age_hrs)
    age_label   = f"{age_h}h"

    print(f"\n  Discussion #{disc_num}: '{disc_title}' — stalled {age_label}")

    # ── Idempotency: check for existing nudge comment ────────────────────────
    comments = disc.get("comments", {}).get("nodes", []) or []
    already_nudged = any(NUDGE_MARKER in (c.get("body") or "") for c in comments)

    if already_nudged:
        print(f"    nudge already posted — skipping comment")
    elif dry_run:
        print(f"    [DRY-RUN] would post nudge comment on Discussion #{disc_num}")
    else:
        nudge_body = (
            f"{NUDGE_MARKER}\n"
            f"Stalled in DISCUSSING for {age_label} — re-spawning project-manager to drive consensus."
        )
        result = run([
            "gh", "api", "graphql",
            "-f", f"""query=mutation AddComment($id:ID!, $body:String!) {{
  addDiscussionComment(input:{{discussionId:$id, body:$body}}) {{
    comment {{ id }}
  }}
}}""",
            "-f", f"id={disc_id}",
            "-f", f"body={nudge_body}",
        ])
        if result.returncode == 0:
            print(f"    nudge comment posted on Discussion #{disc_num}")
            nudges_posted += 1
        else:
            print(f"    ERROR posting nudge: {result.stderr.strip()[:120]}", file=sys.stderr)

    # ── Enqueue project-manager spawn ────────────────────────────────────────
    if disc_num in queued_discussions:
        print(f"    project-manager spawn already queued for Discussion #{disc_num} — skipping")
    elif dry_run:
        print(f"    [DRY-RUN] would enqueue project-manager spawn for Discussion #{disc_num}")
    else:
        prompt_ctx = (
            f"Discussion #{disc_num} ('{disc_title}') has been stalled in DISCUSSING "
            f"for {age_label}. Drive consensus and advance it to SPEC_READY."
        )
        result = run([
            "python3", f"{repo_root}/backend/spawn_queue.py", "enqueue",
            "--priority", "15",
            "--requested-by", "sweep-stalled-discussions",
            "project-manager", str(disc_num), prompt_ctx,
        ])
        if result.returncode == 0:
            print(f"    project-manager spawn enqueued for Discussion #{disc_num}")
            spawns_enqueued += 1
            queued_discussions.add(disc_num)  # avoid re-enqueue in same run
        else:
            print(f"    ERROR enqueuing: {result.stderr.strip()[:120]}", file=sys.stderr)

# ── Team-log summary ─────────────────────────────────────────────────────────
summary = (
    f"sweep-stalled-discussions: {len(stalled)} stalled "
    f"(oldest: #{oldest_disc['number']} for {int(oldest_hrs)}h), "
    f"{nudges_posted} nudges posted, {spawns_enqueued} spawns enqueued"
)
print(f"\n{summary}")
if not dry_run:
    team_log(summary)
else:
    print("[DRY-RUN] would post team-log summary", file=sys.stderr)
PYEOF

exit 0
