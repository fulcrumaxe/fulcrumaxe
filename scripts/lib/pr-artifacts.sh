#!/usr/bin/env bash
# scripts/lib/pr-artifacts.sh — PR test-artifact reader (consumer side).
#
# Provides inject_for_pr <pr_number> <sha> which prints a PRIOR_TEST_RUNS
# block suitable for injection into a spawn prompt.
#
# Usage (source then call):
#   source scripts/lib/pr-artifacts.sh
#   inject_for_pr 999 abc1234
#
# Smoke-test:
#   bash -c 'source scripts/lib/pr-artifacts.sh && inject_for_pr 999 abc1234'

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PR_ARTIFACTS_DIR="${REPO_ROOT}/.autonomous-team/pr-artifacts"

# inject_for_pr <pr_number> <sha>
# Reads .autonomous-team/pr-artifacts/<pr>/<sha>.jsonl and prints a
# PRIOR_TEST_RUNS block. Prints nothing if the file is absent or empty.
inject_for_pr() {
  local pr="${1:-}"
  local sha="${2:-}"

  if [[ -z "$pr" || -z "$sha" ]]; then
    return 0
  fi

  local artifact_file="${PR_ARTIFACTS_DIR}/${pr}/${sha}.jsonl"
  if [[ ! -f "$artifact_file" ]]; then
    return 0
  fi

  local now_epoch
  now_epoch=$(date +%s)
  local lines=()
  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Parse fields using python3 for reliability
    local summary
    summary=$(python3 - "$line" "$now_epoch" <<'_PYEOF'
import sys, json, datetime

line, now_epoch_s = sys.argv[1], int(sys.argv[2])
try:
    d = json.loads(line)
except Exception:
    sys.exit(0)

cmd = d.get("command", "?")
exit_code = d.get("exit_code", "?")
ts = d.get("ts", "")
agent = d.get("agent", "?")

# Compute age in minutes
age_min = "?"
if ts:
    try:
        # parse ISO8601 (with or without trailing Z) — always treat as UTC
        ts_clean = ts.rstrip("Z")
        dt = datetime.datetime.fromisoformat(ts_clean)
        # calendar.timegm converts a UTC struct_time to epoch without TZ offset
        import calendar
        epoch = calendar.timegm(dt.timetuple())
        age_min = int((now_epoch_s - epoch) / 60)
    except Exception:
        pass

print(f"  command={cmd!r} exit_code={exit_code} age={age_min}min agent={agent}")
_PYEOF
)
    [[ -n "$summary" ]] && lines+=("$summary")
  done < "$artifact_file"

  if [[ "${#lines[@]}" -eq 0 ]]; then
    return 0
  fi

  printf '## PRIOR_TEST_RUNS (PR #%s, sha %s)\n' "$pr" "$sha"
  printf 'These test runs were recorded by earlier agents on this PR+sha:\n'
  for l in "${lines[@]}"; do
    printf '%s\n' "$l"
  done
  printf 'If a prior run of an identical command (same sha, <30 min old, exit_code 0) covers your acceptance criterion, you MAY skip re-running it — cite the artifact in your envelope.\n'
}
