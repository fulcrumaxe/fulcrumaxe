#!/usr/bin/env bash
# drain-pending-prs.sh — drain .autonomous-team/pending-prs.json after rate limits clear.
#
# Executors that hit a GitHub secondary rate limit write their pending PR entry to
# pending-prs.json instead of looping.  This script attempts to open each queued PR
# via the REST API, removes successful entries, and leaves rate-limited ones in place.
#
# Usage (idempotent — safe to call after every merge):
#   bash scripts/drain-pending-prs.sh [--dry-run]
#
# On 403 (rate-limit still active):
#   - Tries the entry once (no sleep, no loop).
#   - Leaves the entry in the queue for the next invocation.
#   - Exits 0 so callers are never blocked.
#
# HARD RULE: This script MUST NOT invoke claude, claude -p, _start_loop_run, or /loop.
# HARD RULE: NO sleep loops.  One attempt per entry per call.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Allow REPO_ROOT override for testing; default to canonical path from script location
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
_REPO="$(_resolve_repo)"

PENDING_FILE="${REPO_ROOT}/.autonomous-team/pending-prs.json"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Guard: nothing to do if the queue file is absent or empty ─────────────────
if [[ ! -f "$PENDING_FILE" ]]; then
  echo "[drain-pending-prs] No pending-prs.json — nothing to drain"
  exit 0
fi

QUEUE=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    if not isinstance(data, list):
        print(json.dumps([]))
    else:
        print(json.dumps(data))
except Exception as e:
    print(json.dumps([]))
" "$PENDING_FILE" 2>/dev/null || echo "[]")

TOTAL=$(echo "$QUEUE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

if [[ "$TOTAL" -eq 0 ]]; then
  echo "[drain-pending-prs] Queue is empty — nothing to drain"
  exit 0
fi

echo "[drain-pending-prs] Found $TOTAL pending PR(s) — attempting to drain"

# ── Process each entry ────────────────────────────────────────────────────────
REMAINING="[]"
SUCCESS_COUNT=0
SKIP_COUNT=0

while IFS= read -r ENTRY; do
  [[ -z "$ENTRY" ]] && continue

  BRANCH=$(echo "$ENTRY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('branch',''))" 2>/dev/null || echo "")
  TITLE=$(echo "$ENTRY"  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('title',''))" 2>/dev/null || echo "")
  BODY=$(echo "$ENTRY"   | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('body',''))" 2>/dev/null || echo "")
  DISC=$(echo "$ENTRY"   | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('discussion',''))" 2>/dev/null || echo "")

  if [[ -z "$BRANCH" || -z "$TITLE" ]]; then
    echo "[drain-pending-prs] Skipping malformed entry (missing branch or title)"
    continue
  fi

  echo "[drain-pending-prs] Attempting PR: branch=$BRANCH title=$TITLE"

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[drain-pending-prs] DRY-RUN: would open PR for branch=$BRANCH"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    continue
  fi

  # Single REST attempt — NO retry loop, NO sleep
  HTTP_RESPONSE=$(python3 -c "
import json, subprocess, sys

branch = sys.argv[1]
title  = sys.argv[2]
body   = sys.argv[3]

payload = json.dumps({
    'title': title,
    'body': body,
    'head': branch,
    'base': 'main',
})

result = subprocess.run(
    ['gh', 'api', '-X', 'POST',
     'repos/$_REPO/pulls',
     '--input', '-',
     '-H', 'Accept: application/vnd.github+json'],
    input=payload,
    capture_output=True,
    text=True,
)

# Print: STATUS_CODE|PR_NUMBER
try:
    resp = json.loads(result.stdout)
    pr_num = resp.get('number', '')
    if pr_num:
        print(f'created|{pr_num}')
    else:
        # Check for errors
        errors = resp.get('errors', [])
        msg = resp.get('message', '')
        if 'already exists' in str(errors).lower() or 'already exists' in msg.lower():
            print('exists|0')
        elif result.returncode != 0:
            print('rate_limit|0')
        else:
            print(f'error|0')
except Exception:
    if result.returncode != 0:
        print('rate_limit|0')
    else:
        print('error|0')
" "$BRANCH" "$TITLE" "$BODY" 2>/dev/null || echo "error|0")

  STATUS="${HTTP_RESPONSE%%|*}"
  PR_NUM="${HTTP_RESPONSE##*|}"

  case "$STATUS" in
    created)
      echo "[drain-pending-prs] PR #$PR_NUM created for branch=$BRANCH"
      [[ -n "$DISC" ]] && \
        bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
          "[$(date +%H:%M)] drain-pending-prs: PR #$PR_NUM opened for D#${DISC} (branch=$BRANCH)" \
          2>/dev/null || true
      SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
      ;;
    exists)
      echo "[drain-pending-prs] PR already exists for branch=$BRANCH — removing from queue"
      SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
      ;;
    rate_limit)
      echo "[drain-pending-prs] Rate-limited on branch=$BRANCH — leaving in queue"
      REMAINING=$(echo "$REMAINING" | python3 -c "
import json, sys
lst = json.load(sys.stdin)
lst.append(json.loads(sys.argv[1]))
print(json.dumps(lst, indent=2))
" "$ENTRY" 2>/dev/null || echo "$REMAINING")
      SKIP_COUNT=$((SKIP_COUNT + 1))
      ;;
    *)
      echo "[drain-pending-prs] Unexpected error for branch=$BRANCH (status=$STATUS) — leaving in queue"
      REMAINING=$(echo "$REMAINING" | python3 -c "
import json, sys
lst = json.load(sys.stdin)
lst.append(json.loads(sys.argv[1]))
print(json.dumps(lst, indent=2))
" "$ENTRY" 2>/dev/null || echo "$REMAINING")
      SKIP_COUNT=$((SKIP_COUNT + 1))
      ;;
  esac

done < <(echo "$QUEUE" | python3 -c "
import json, sys
for item in json.load(sys.stdin):
    print(json.dumps(item))
" 2>/dev/null)

# ── Write back remaining entries (or remove the file if empty) ────────────────
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[drain-pending-prs] DRY-RUN complete: would process $SUCCESS_COUNT, skip $SKIP_COUNT"
  exit 0
fi

REMAINING_COUNT=$(echo "$REMAINING" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
if [[ "$REMAINING_COUNT" -eq 0 ]]; then
  rm -f "$PENDING_FILE"
  echo "[drain-pending-prs] Queue empty — removed pending-prs.json"
else
  echo "$REMAINING" > "$PENDING_FILE"
  echo "[drain-pending-prs] Wrote $REMAINING_COUNT remaining entry(ies) back to queue"
fi

echo "[drain-pending-prs] Done: $SUCCESS_COUNT created, $SKIP_COUNT still queued"
exit 0
