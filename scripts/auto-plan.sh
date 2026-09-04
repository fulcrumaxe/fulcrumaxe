#!/usr/bin/env bash
# scripts/auto-plan.sh — deterministic daily plan skeleton generator.
#
# Assembles .autonomous-team/PLAN-<date>.md from GitHub + filesystem data:
#   - PRs merged on <date>
#   - Discussions filed on <date>
#   - Latest run-analyst findings
#   - SPEC_READY backlog count + titles
#   - Open PRs
#
# No LLM call. Pure bash + gh + python3 + filesystem.
#
# Usage:
#   bash scripts/auto-plan.sh [--date YYYY-MM-DD] [--force]

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# shellcheck source=lib/platform-compat.sh
source "$SCRIPT_DIR/lib/platform-compat.sh"
REPO="$(_resolve_repo)"
REPO_OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"

DATE=$(date '+%Y-%m-%d')
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --date) shift; DATE="$1" ;;
    --date=*) DATE="${arg#--date=}" ;;
    --force) FORCE=true ;;
  esac
done

OUTPUT_FILE="$REPO_ROOT/.autonomous-team/PLAN-${DATE}.md"

# Idempotency check
if [[ -f "$OUTPUT_FILE" ]] && [[ "$FORCE" != "true" ]]; then
  echo "auto-plan.sh: $OUTPUT_FILE already exists — skipping (use --force to overwrite)" >&2
  exit 0
fi

GENERATED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# ── Data source functions (each writes markdown to stdout, never aborts) ─────

today_merged_prs() {
  local date="$1"
  local yesterday="$2"
  local out
  out=$(gh pr list \
    --repo "$REPO" \
    --state merged \
    --search "merged:>=${yesterday} merged:<${date}" \
    --json number,title,mergedAt \
    --limit 50 \
    2>&1) || { echo "- (data source unavailable: gh pr list merged — $out)"; return; }

  local count
  count=$(echo "$out" | python3 -c "import json,sys; prs=json.load(sys.stdin); print(len(prs))" 2>/dev/null || echo 0)
  if [[ "$count" -eq 0 ]]; then
    echo "- (no PRs merged on ${yesterday})"
    return
  fi

  echo "| PR | Title | Verdict |"
  echo "|---|---|---|"
  echo "$out" | python3 -c "
import json, sys
prs = json.load(sys.stdin)
for p in sorted(prs, key=lambda x: x.get('number', 0)):
    title = p['title'][:70].replace('|', '\\\\|')
    print(f\"| #{p['number']} | {title} | |\")" 2>/dev/null \
    || echo "- (parse error reading merged PRs)"
}

today_new_discussions() {
  local date="$1"
  local threshold="${date}T00:00:00Z"
  local out
  out=$(gh api graphql -f query="query {
    repository(owner:\"$REPO_OWNER\", name:\"$REPO_NAME\") {
      discussions(first:50, orderBy:{field:CREATED_AT, direction:DESC}) {
        nodes { number title createdAt }
      }
    }
  }" 2>&1) || { echo "- (data source unavailable: discussions GraphQL — $out)"; return; }

  local items
  items=$(echo "$out" | python3 -c "
import json, sys
data = json.load(sys.stdin)
nodes = data.get('data', {}).get('repository', {}).get('discussions', {}).get('nodes', [])
threshold = sys.argv[1]
for n in nodes:
    if n.get('createdAt','') >= threshold:
        print(f\"- D#{n['number']}: {n['title'][:80]}\")
" "$threshold" 2>/dev/null)

  if [[ -z "$items" ]]; then
    echo "- (no new Discussions filed on ${date})"
  else
    echo "$items"
  fi
}

latest_run_analyst_findings() {
  local report_dir="$REPO_ROOT/.autonomous-team/run-reports"
  local newest
  newest=$(ls -t "$report_dir"/*.md 2>/dev/null | head -1)
  if [[ -z "$newest" ]]; then
    echo "- (no run-analyst reports found)"
    return
  fi
  # Extract up to 5 bullets from the first ## heading that contains findings/categories
  local bullets
  bullets=$(awk '
    /^## / { in_section = ($0 ~ /[Ff]inding|[Cc]ategor/) }
    in_section && /^- / { print; count++ }
    count >= 5 { exit }
  ' "$newest" 2>/dev/null)
  if [[ -z "$bullets" ]]; then
    # Fallback: grab first 5 bullet lines from the file
    bullets=$(grep '^- ' "$newest" 2>/dev/null | head -5)
  fi
  if [[ -z "$bullets" ]]; then
    echo "- (no findings extracted from $newest)"
  else
    echo "Source: \`$(basename "$newest")\`"
    echo ""
    echo "$bullets"
  fi
}

spec_ready_backlog() {
  local out
  out=$(gh api graphql -f query="query {
    repository(owner:\"$REPO_OWNER\", name:\"$REPO_NAME\") {
      discussions(first:50, states:OPEN) {
        nodes { number title body }
      }
    }
  }" 2>&1) || { echo "- (data source unavailable: spec_ready GraphQL — $out)"; return; }

  local items
  # Uses the same shared parse as the loop selector, so this report cannot claim
  # a Discussion is ready that the selector would refuse (D#1755). Blocked ones
  # are listed as blocked rather than counted as ready or dropped.
  items=$(echo "$out" | python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from backend.blocked_by import partition_spec_ready
data = json.load(sys.stdin)
nodes = data.get("data", {}).get("repository", {}).get("discussions", {}).get("nodes", [])
ready, blocked = partition_spec_ready(nodes)
if not ready:
    print("- (no spawnable SPEC_READY discussions)")
else:
    print(f"**{len(ready)} SPEC_READY:**")
    for n in ready[:20]:
        num, title = n["number"], n["title"][:80]
        print(f"- D#{num}: {title}")
if blocked:
    print("")
    print(f"**{len(blocked)} SPEC_READY but BLOCKED (not spawnable):**")
    for num, reasons in blocked[:20]:
        print(f"- D#{num}: blocked by {reasons}")
' "$REPO_ROOT" 2>/dev/null || echo "- (parse error reading SPEC_READY backlog)")

  echo "$items"
}

open_prs() {
  local out
  out=$(gh pr list \
    --repo "$REPO" \
    --state open \
    --json number,title,labels \
    --limit 30 \
    2>&1) || { echo "- (data source unavailable: open PRs — $out)"; return; }

  local count
  count=$(echo "$out" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
  if [[ "$count" -eq 0 ]]; then
    echo "- (no open PRs)"
    return
  fi
  echo "$out" | python3 -c "
import json, sys
prs = json.load(sys.stdin)
for p in sorted(prs, key=lambda x: x.get('number', 0)):
    labels = [l['name'] for l in p.get('labels', [])]
    flags = ' '.join(f'[{l[:18]}]' for l in labels if 'review' in l or 'needs' in l)
    title = p['title'][:70].replace('|', '\\\\|')
    print(f\"- #{p['number']}: {title} {flags}\".rstrip())
" 2>/dev/null || echo "- (parse error reading open PRs)"
}

# ── Collect data ──────────────────────────────────────────────────────────────

YESTERDAY=$(pc_date_offset "$DATE" -1)

echo "auto-plan.sh: collecting data for ${DATE} (merged window: ${YESTERDAY}–${DATE})..." >&2

MERGED_PRS=$(today_merged_prs "$DATE" "$YESTERDAY")
NEW_DISCUSSIONS=$(today_new_discussions "$DATE")
RUN_FINDINGS=$(latest_run_analyst_findings)
SPEC_READY=$(spec_ready_backlog)
OPEN_PRS=$(open_prs)

# ── Write output ──────────────────────────────────────────────────────────────

cat > "$OUTPUT_FILE" <<EOF
# Plan for ${DATE}

Auto-generated ${GENERATED_AT} by scripts/auto-plan.sh from start-the-day sweeps. Review and edit before spawning work.

---

## Yesterday's Results (${YESTERDAY})

${MERGED_PRS}

---

## Carryover

### Open PRs

${OPEN_PRS}

### SPEC_READY Backlog

${SPEC_READY}

---

## New Discussions Filed Today

${NEW_DISCUSSIONS}

---

## Recent Run-Analyst Findings

${RUN_FINDINGS}

---

## P0

- TODO

---

## P1

- TODO

---

## P2

- TODO

---

## P3

- TODO

---

## P4

- TODO

---

## Today's mistakes-to-avoid

- TODO

---

## End-of-day target

- TODO
EOF

echo "auto-plan.sh: wrote $OUTPUT_FILE" >&2
