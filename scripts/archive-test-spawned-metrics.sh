#!/usr/bin/env bash
# scripts/archive-test-spawned-metrics.sh — one-shot migration of test-spawned loop metrics rows.
#
# Purpose:
#   Moves rows written by Puppeteer/E2E test runs (2026-05-10 02:00–06:10 UTC window)
#   from .autonomous-team/loop-metrics.jsonl to
#   archive/loop-metrics-test-spawned-2026-05-11/loop-metrics-test-spawned-2026-05-11.jsonl
#   per the Archive Protocol (never delete — always archive with README).
#
# Idempotent: safe to re-run. If the archive file already exists and the live file
# has no rows in the contaminated window, exits 0 without changes.
#
# Usage:
#   bash scripts/archive-test-spawned-metrics.sh [--dry-run]
#
# Exit codes:
#   0  — success (rows moved, already done, or dry-run preview)
#   1  — fatal error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

METRICS_FILE="${METRICS_FILE:-$REPO_ROOT/.autonomous-team/loop-metrics.jsonl}"
ARCHIVE_DIR="$REPO_ROOT/archive/loop-metrics-test-spawned-2026-05-11"
ARCHIVE_FILE="$ARCHIVE_DIR/loop-metrics-test-spawned-2026-05-11.jsonl"
README_FILE="$ARCHIVE_DIR/README.md"

DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "archive-test-spawned-metrics: unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Window boundaries (inclusive on both ends, ISO 8601 string comparison)
# Rows produced by Puppeteer test runs on 2026-05-10
WINDOW_START="2026-05-10T02:00:00"
WINDOW_END="2026-05-10T06:10:00"

if [[ ! -f "$METRICS_FILE" ]]; then
  echo "archive-test-spawned-metrics: $METRICS_FILE not found — nothing to do."
  exit 0
fi

RESULT=$(METRICS_FILE="$METRICS_FILE" ARCHIVE_FILE="$ARCHIVE_FILE" \
  WINDOW_START="$WINDOW_START" WINDOW_END="$WINDOW_END" DRY_RUN="$DRY_RUN" \
  python3 - <<'PYEOF'
import json, os, sys

metrics_file  = os.environ["METRICS_FILE"]
archive_file  = os.environ["ARCHIVE_FILE"]
window_start  = os.environ["WINDOW_START"]
window_end    = os.environ["WINDOW_END"]
dry_run       = os.environ.get("DRY_RUN", "false") == "true"


def row_ts(row: dict) -> str:
    """Return the timestamp string, supporting both 'timestamp' and legacy 'ts'."""
    return (row.get("timestamp") or row.get("ts") or "").rstrip("Z").replace("+00:00", "")


with open(metrics_file, "r", encoding="utf-8", errors="replace") as fh:
    raw_lines = [l.rstrip("\n") for l in fh if l.strip()]

keep_rows: list[str] = []
archive_rows: list[str] = []

for line in raw_lines:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        keep_rows.append(line)
        continue
    ts = row_ts(row)
    if window_start <= ts <= window_end:
        archive_rows.append(line)
    else:
        keep_rows.append(line)

result = {
    "total": len(raw_lines),
    "keep": len(keep_rows),
    "archive": len(archive_rows),
    "dry_run": dry_run,
    "newly_archived": 0,
}

if not dry_run and archive_rows:
    os.makedirs(os.path.dirname(archive_file), exist_ok=True)

    # Idempotency: skip rows already in archive
    existing = set()
    if os.path.exists(archive_file):
        with open(archive_file, "r", encoding="utf-8") as af:
            existing = {l.strip() for l in af if l.strip()}
    new_rows = [r for r in archive_rows if r not in existing]
    if new_rows:
        with open(archive_file, "a", encoding="utf-8") as af:
            for r in new_rows:
                af.write(r + "\n")
    result["newly_archived"] = len(new_rows)

    # Rewrite live file keeping only non-contaminated rows
    with open(metrics_file, "w", encoding="utf-8") as fh:
        for r in keep_rows:
            fh.write(r + "\n")

print(json.dumps(result))
PYEOF
)

ARCHIVE_COUNT=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['archive'])")
KEEP_COUNT=$(echo "$RESULT"   | python3 -c "import json,sys; print(json.load(sys.stdin)['keep'])")
NEWLY=$(echo "$RESULT"        | python3 -c "import json,sys; print(json.load(sys.stdin).get('newly_archived', 0))")

if [[ "$DRY_RUN" == "true" ]]; then
  echo "archive-test-spawned-metrics [dry-run]: would archive $ARCHIVE_COUNT rows, keep $KEEP_COUNT rows."
  exit 0
fi

if [[ "$ARCHIVE_COUNT" -eq 0 ]]; then
  echo "archive-test-spawned-metrics: no rows in contaminated window — already migrated or file is clean."
  exit 0
fi

# Write README (Archive Protocol requirement)
mkdir -p "$ARCHIVE_DIR"
if [[ ! -f "$README_FILE" ]]; then
  cat > "$README_FILE" <<'README'
# Archive: loop-metrics-test-spawned-2026-05-11

## When removed
2026-05-11

## Why removed
Puppeteer/E2E test-spawned `/loop` runs (Discussion #487 bug #4) polluted the production
`.autonomous-team/loop-metrics.jsonl` file. All rows in the 2026-05-10 02:00–06:10 UTC
window were written by test-triggered loop iterations, not real cron iterations. 26 of
them contained Python tracebacks. They caused the `/loop-timeline` Activity chart to show
only test data instead of real iterations.

This archive was created by `scripts/archive-test-spawned-metrics.sh` (one-shot migration,
idempotent) as part of the Discussion #487 bundled fix.

## Original path
`.autonomous-team/loop-metrics.jsonl`

## How to restore
```bash
cat archive/loop-metrics-test-spawned-2026-05-11/loop-metrics-test-spawned-2026-05-11.jsonl \
  >> .autonomous-team/loop-metrics.jsonl
```
Sort by timestamp if order matters:
```bash
python3 - <<'EOF'
import json
rows = [json.loads(l) for l in open(".autonomous-team/loop-metrics.jsonl") if l.strip()]
rows.sort(key=lambda r: r.get("timestamp") or r.get("ts", ""))
with open(".autonomous-team/loop-metrics.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
EOF
```

## What would justify restoring
Only a forensic replay of the 2026-05-10 Puppeteer test runaway session — e.g. to audit
which test scenarios fired real loop triggers before the `AF_MCP_TEST_ORIGIN` guard was
in place. Not needed for normal operations.
README
fi

# Stage archive files (git add — respects Archive Protocol tracking)
git -C "$REPO_ROOT" add "$ARCHIVE_DIR" 2>/dev/null || true

echo "archive-test-spawned-metrics: archived $ARCHIVE_COUNT rows ($NEWLY newly written) → $ARCHIVE_FILE; kept $KEEP_COUNT rows in live file."
