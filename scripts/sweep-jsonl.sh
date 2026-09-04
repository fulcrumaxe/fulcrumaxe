#!/usr/bin/env bash
# sweep-jsonl.sh — rotate JSONL files and clean up hook-events/ per config.
#
# Config search order (first found wins):
#   1. .autonomous-team/jsonl-rotation.json  (user override in external state)
#   2. templates/jsonl-rotation.json         (repo default, always present)
#
# Usage:
#   bash scripts/sweep-jsonl.sh [--config <path>] [--dry-run]
#
# In dry-run mode: prints what would happen, makes no changes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=""
CONFIG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  DRY_RUN="1"; shift ;;
    --config)   CONFIG_OVERRIDE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Locate config ──────────────────────────────────────────────────────────
CONFIG_FILE=""
if [[ -n "$CONFIG_OVERRIDE" ]]; then
  CONFIG_FILE="$CONFIG_OVERRIDE"
elif [[ -f "$REPO_ROOT/.autonomous-team/jsonl-rotation.json" ]]; then
  CONFIG_FILE="$REPO_ROOT/.autonomous-team/jsonl-rotation.json"
elif [[ -f "$REPO_ROOT/templates/jsonl-rotation.json" ]]; then
  CONFIG_FILE="$REPO_ROOT/templates/jsonl-rotation.json"
fi

if [[ -z "$CONFIG_FILE" ]]; then
  echo "[sweep-jsonl] ERROR: no config found (tried .autonomous-team/jsonl-rotation.json and templates/jsonl-rotation.json)" >&2
  exit 1
fi

echo "[sweep-jsonl] using config: $CONFIG_FILE"

# ── Read config values via Python ──────────────────────────────────────────
CONFIG_JSON=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    print(json.dumps(json.load(f)))
" "$CONFIG_FILE" 2>/dev/null) || {
  echo "[sweep-jsonl] ERROR: could not parse config $CONFIG_FILE" >&2
  exit 1
}

HOOK_EVENTS_MAX_AGE=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
print(d.get('hook_events_max_age_days', 7))
" "$CONFIG_JSON")

HOOK_EVENTS_DONE_MAX_AGE=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
print(d.get('hook_events_done_max_age_days', 2))
" "$CONFIG_JSON")

# ── Rotate JSONL files ─────────────────────────────────────────────────────
echo "[sweep-jsonl] rotating JSONL files..."

python3 - "$CONFIG_JSON" "$REPO_ROOT" "$DRY_RUN" <<'PYEOF'
import json, os, sys

config_json = sys.argv[1]
repo_root   = sys.argv[2]
dry_run     = sys.argv[3] == "1"

sys.path.insert(0, repo_root)
from backend.jsonl_rotator import rotate_if_needed

config = json.loads(config_json)
files  = config.get("files", [])

for entry in files:
    rel_path = entry.get("path", "")
    abs_path = os.path.join(repo_root, rel_path) if not os.path.isabs(rel_path) else rel_path

    max_size_mb  = entry.get("max_size_mb")
    max_age_days = entry.get("max_age_days")
    max_lines    = entry.get("max_lines")
    keep_archives = entry.get("keep_archives", 5)

    if dry_run:
        exists = os.path.exists(abs_path)
        size = os.path.getsize(abs_path) / (1024*1024) if exists else 0
        print(f"[sweep-jsonl] DRY-RUN {rel_path}: size={size:.1f}MB exists={exists}")
        continue

    if not os.path.exists(abs_path):
        print(f"[sweep-jsonl] skip (not found): {rel_path}")
        continue

    result = rotate_if_needed(
        path=abs_path,
        max_size_mb=max_size_mb,
        max_age_days=max_age_days,
        max_lines=max_lines,
        keep_archives=keep_archives,
    )

    if result.get("error"):
        print(f"[sweep-jsonl] ERROR rotating {rel_path}: {result['error']}", file=sys.stderr)
    elif result["rotated"]:
        arc = result.get("archive", "")
        pruned = result.get("pruned", 0)
        print(f"[sweep-jsonl] rotated {rel_path} → {os.path.basename(arc)} (pruned {pruned} old archives)")
    else:
        print(f"[sweep-jsonl] ok (below thresholds): {rel_path}")
PYEOF

ROTATE_EXIT=$?
if [[ $ROTATE_EXIT -ne 0 ]]; then
  echo "[sweep-jsonl] WARN: JSONL rotation step exited $ROTATE_EXIT" >&2
fi

# ── hook-events/ cleanup ───────────────────────────────────────────────────
HOOK_DIR="$REPO_ROOT/.autonomous-team/hook-events"

if [[ -d "$HOOK_DIR" ]]; then
  DONE_DIR="$HOOK_DIR/done"
  MAX_DELETIONS=1000

  # Clean hook-events/done/ — files older than hook_events_done_max_age_days
  if [[ -d "$DONE_DIR" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      COUNT=$(find "$DONE_DIR" -maxdepth 1 -type f -mtime +"$HOOK_EVENTS_DONE_MAX_AGE" 2>/dev/null | wc -l | tr -d ' ')
      echo "[sweep-jsonl] DRY-RUN hook-events/done/: $COUNT files older than ${HOOK_EVENTS_DONE_MAX_AGE} days would be deleted"
    else
      DELETED=$(find "$DONE_DIR" -maxdepth 1 -type f -mtime +"$HOOK_EVENTS_DONE_MAX_AGE" 2>/dev/null | head -n "$MAX_DELETIONS" | wc -l | tr -d ' ')
      find "$DONE_DIR" -maxdepth 1 -type f -mtime +"$HOOK_EVENTS_DONE_MAX_AGE" 2>/dev/null | head -n "$MAX_DELETIONS" | xargs rm -f 2>/dev/null || true
      echo "[sweep-jsonl] hook-events/done/: deleted $DELETED files older than ${HOOK_EVENTS_DONE_MAX_AGE} days"
    fi
  fi

  # Clean hook-events/*.json (top-level only, not done/) — older than hook_events_max_age_days
  if [[ "$DRY_RUN" == "1" ]]; then
    COUNT=$(find "$HOOK_DIR" -maxdepth 1 -type f -name "*.json" -mtime +"$HOOK_EVENTS_MAX_AGE" 2>/dev/null | wc -l | tr -d ' ')
    COUNT_LOCK=$(find "$HOOK_DIR" -maxdepth 1 -type f -name "*.lock" -mtime +"$HOOK_EVENTS_MAX_AGE" 2>/dev/null | wc -l | tr -d ' ')
    echo "[sweep-jsonl] DRY-RUN hook-events/ (top-level): $COUNT .json + $COUNT_LOCK .lock files older than ${HOOK_EVENTS_MAX_AGE} days would be deleted"
  else
    DELETED_JSON=$(find "$HOOK_DIR" -maxdepth 1 -type f -name "*.json" -mtime +"$HOOK_EVENTS_MAX_AGE" 2>/dev/null | head -n "$MAX_DELETIONS" | wc -l | tr -d ' ')
    DELETED_LOCK=$(find "$HOOK_DIR" -maxdepth 1 -type f -name "*.lock" -mtime +"$HOOK_EVENTS_MAX_AGE" 2>/dev/null | head -n "$MAX_DELETIONS" | wc -l | tr -d ' ')
    find "$HOOK_DIR" -maxdepth 1 -type f \( -name "*.json" -o -name "*.lock" \) -mtime +"$HOOK_EVENTS_MAX_AGE" 2>/dev/null | head -n "$MAX_DELETIONS" | xargs rm -f 2>/dev/null || true
    echo "[sweep-jsonl] hook-events/ (top-level): deleted $DELETED_JSON .json + $DELETED_LOCK .lock files older than ${HOOK_EVENTS_MAX_AGE} days"
  fi
else
  echo "[sweep-jsonl] hook-events/ directory not found — skipping hook cleanup"
fi

echo "[sweep-jsonl] done."
