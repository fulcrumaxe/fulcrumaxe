#!/usr/bin/env bash
# compute-pr-defect-density.sh — delayed defect density metric
#
# For a given PR, count bugs filed in the following N days that cite the PR
# and emit defect_density_per_pr to stats.duckdb.
#
# Usage:
#   bash scripts/compute-pr-defect-density.sh --pr <N> [--discussion <N>] [--window-days <N>]
#
# Typically called twice per PR:
#   • 24h after merge  (--window-days 1)
#   • 7d  after merge  (--window-days 7, default)
#
# Safe to re-run: DuckDB INSERT OR IGNORE deduplicates by (ts, metric, tags).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
_REPO="$(_resolve_repo)"
_REPO_OWNER="${_REPO%%/*}"
_REPO_NAME="${_REPO##*/}"

PR=""
DISCUSSION=""
WINDOW_DAYS=7

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)           PR="$2";           shift 2 ;;
    --discussion)   DISCUSSION="$2";   shift 2 ;;
    --window-days)  WINDOW_DAYS="$2";  shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 --pr <N> [--discussion <N>] [--window-days <N>]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "Error: --pr is required" >&2
  exit 1
fi

# Count bug Discussions filed after merge that mention this PR
BUG_COUNT=$(python3 - <<PYEOF
import subprocess, json, sys, datetime

pr = "$PR"
window_days = int("$WINDOW_DAYS")

# Fetch Discussions from the last N+1 days that contain a reference to this PR
# and are tagged [Bug]
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=window_days)).isoformat()

try:
    result = subprocess.run(
        [
            "gh", "api", "graphql",
            "--repo", "$_REPO",
            "-f", """query {
  repository(owner:"$_REPO_OWNER", name:"$_REPO_NAME") {
    discussions(first:100, orderBy:{field:CREATED_AT, direction:DESC}) {
      nodes {
        number
        title
        createdAt
        body
      }
    }
  }
}""",
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
except Exception as e:
    print(0)
    sys.exit(0)

discussions = data.get("data", {}).get("repository", {}).get("discussions", {}).get("nodes", [])
pr_ref = f"#{pr}"
pr_ref_full = f"PR #{pr}"

count = 0
for d in discussions:
    created = d.get("createdAt", "")
    if created < cutoff:
        continue
    title = d.get("title", "")
    body = d.get("body", "") or ""
    # Must be a bug Discussion
    if not title.startswith("[Bug]"):
        continue
    # Must reference this PR
    if pr_ref in body or pr_ref_full in body or pr_ref in title:
        count += 1

print(count)
PYEOF
)

echo "[defect-density] PR #${PR}: ${BUG_COUNT} bugs filed in ${WINDOW_DAYS}d window"

TAGS_JSON="{\"pr\":\"${PR}\",\"window_days\":\"${WINDOW_DAYS}\"}"
if [[ -n "$DISCUSSION" ]]; then
  TAGS_JSON="{\"pr\":\"${PR}\",\"discussion\":\"${DISCUSSION}\",\"window_days\":\"${WINDOW_DAYS}\"}"
fi

python3 - <<PYEOF
import sys
sys.path.insert(0, "$REPO_ROOT/backend")
from stats_writer import record
import json

record(
    metric="defect_density_per_pr",
    value=float("$BUG_COUNT"),
    unit="count",
    tags=$TAGS_JSON,
    source="compute-pr-defect-density",
)
print("[defect-density] recorded defect_density_per_pr=$BUG_COUNT for PR #$PR (${WINDOW_DAYS}d window)")
PYEOF
