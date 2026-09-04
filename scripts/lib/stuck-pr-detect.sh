#!/usr/bin/env bash
# scripts/lib/stuck-pr-detect.sh — shared helper for detecting stuck PRs.
#
# A PR is "stuck" when it is open, has the label `code-review-needs-fix`,
# does NOT have `code-review-passed`, and has not been updated in the last
# STUCK_PR_THRESHOLD_MINUTES minutes (default: 30).
#
# Usage (source this file, then call the function):
#   source scripts/lib/stuck-pr-detect.sh
#   list_stuck_prs [threshold_minutes]
#
# Output: JSON array of objects, one per stuck PR:
#   [{"number": 42, "updated_at": "2026-05-10T06:00:00Z", "age_minutes": 37}, ...]
#
# Repo is resolved from project.json → STUCK_PR_REPO env → repo-resolve fallback.

# shellcheck source=repo-resolve.sh
source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"
STUCK_PR_REPO="${STUCK_PR_REPO:-$(_resolve_repo)}"

list_stuck_prs() {
  local threshold="${1:-${STUCK_PR_THRESHOLD_MINUTES:-30}}"
  local now_epoch
  now_epoch=$(date -u +%s)

  # Fetch open PRs with code-review-needs-fix label (includes updatedAt for age calc)
  local raw
  raw=$(gh pr list \
    --repo "$STUCK_PR_REPO" \
    --state open \
    --label "code-review-needs-fix" \
    --json number,updatedAt,labels \
    2>/dev/null) || { echo "[]"; return 0; }

  if [ -z "$raw" ] || [ "$raw" = "null" ]; then
    echo "[]"
    return 0
  fi

  # Filter: exclude PRs that also have code-review-passed, then compute age
  # Write raw to a temp file so python3 can read it cleanly
  local tmp_file
  tmp_file=$(mktemp /tmp/stuck-pr-detect-XXXXXX.json)
  printf '%s' "$raw" > "$tmp_file"

  python3 - "$threshold" "$now_epoch" "$tmp_file" <<'PYEOF'
import json, sys, os

threshold_minutes = float(sys.argv[1])
now_epoch = int(sys.argv[2])
tmp_file = sys.argv[3]

try:
    with open(tmp_file) as f:
        data = json.load(f)
except Exception:
    print("[]")
    sys.exit(0)
finally:
    try:
        os.unlink(tmp_file)
    except Exception:
        pass

result = []

for pr in data:
    labels = [lb["name"] for lb in (pr.get("labels") or [])]
    if "code-review-passed" in labels:
        continue
    updated_at = pr.get("updatedAt", "")
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        pr_epoch = int(dt.timestamp())
        age_seconds = now_epoch - pr_epoch
        age_minutes = age_seconds / 60.0
    except Exception:
        age_minutes = 0.0

    if age_minutes >= threshold_minutes:
        result.append({
            "number": pr["number"],
            "updated_at": updated_at,
            "age_minutes": round(age_minutes, 1),
        })

print(json.dumps(result))
PYEOF
}
