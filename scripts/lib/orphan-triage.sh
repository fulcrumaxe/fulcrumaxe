#!/usr/bin/env bash
# orphan-triage.sh — shared helpers for orphan-diff triage.
#
# Sourced by scripts/triage-orphan-diffs.sh and scripts/reap-worktrees.sh.
# Do NOT execute directly.
#
# Requires: bash, jq, git

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
# REPO_ROOT may be overridden by the sourcing script (e.g. tests).
# ORPHAN_DIFF_DIR and ORPHAN_DIFF_NUDGE_STATE are derived at call time via
# the functions below, not at source time, so REPO_ROOT overrides are respected.
_ot_orphan_diff_dir() {
  local root="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"
  echo "${root}/archive/orphan-diffs"
}
_ot_nudge_state_file() {
  local root="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"
  echo "${root}/.autonomous-team/orphan-diff-nudge-state.json"
}
# Keep backward-compat vars for any callers that read them directly;
# they will point to the correct location if REPO_ROOT is set before sourcing.
ORPHAN_DIFF_DIR="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}/archive/orphan-diffs"
ORPHAN_DIFF_NUDGE_STATE="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}/.autonomous-team/orphan-diff-nudge-state.json"

# ---------------------------------------------------------------------------
# _ot_meta_read <patch_path>
#   Prints the sidecar JSON for <patch_path> to stdout.
#   Returns the default untriaged object if no sidecar exists.
# ---------------------------------------------------------------------------
_ot_meta_read() {
  local patch="$1"
  local meta="${patch}.meta.json"
  if [[ -f "$meta" ]]; then
    cat "$meta"
  else
    echo '{"status":"untriaged","note":"","tagged_at":null,"tagged_by":null}'
  fi
}

# ---------------------------------------------------------------------------
# _ot_meta_write <patch_path> <status> <note> [<tagged_by>]
#   Writes/overwrites the sidecar JSON for <patch_path>.
# ---------------------------------------------------------------------------
_ot_meta_write() {
  local patch="$1"
  local status="$2"
  local note="${3:-}"
  local tagged_by="${4:-${USER:-unknown}}"
  local meta="${patch}.meta.json"
  local tagged_at
  tagged_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local patch_name
  patch_name=$(basename "$patch")

  # Validate status
  case "$status" in
    untriaged|salvaged|discarded|needs-review) ;;
    *)
      echo "ERROR: unknown status '${status}'. Must be: untriaged, salvaged, discarded, needs-review" >&2
      return 2
      ;;
  esac

  python3 -c "
import json, sys
d = {
  'patch': sys.argv[1],
  'status': sys.argv[2],
  'note': sys.argv[3],
  'tagged_at': sys.argv[4],
  'tagged_by': sys.argv[5],
}
print(json.dumps(d, indent=2))
" "$patch_name" "$status" "$note" "$tagged_at" "$tagged_by" > "$meta"
}

# ---------------------------------------------------------------------------
# _ot_parse_patch <patch_path>
#   Parses a patch file and emits tab-separated:
#   <agent_id>  <date>  <lines_added>  <lines_removed>  <top_files>
#
#   Uses git apply --numstat for accurate line counts.
#   Falls back to grep-based heuristic if numstat fails.
# ---------------------------------------------------------------------------
_ot_parse_patch() {
  local patch="$1"
  local filename
  filename=$(basename "$patch")

  # --- Extract agent_id and date from filename ---------------------------
  # Expected patterns:
  #   agent-<id>-<YYYY-MM-DD>.patch
  #   discussion-<num>-<slug>-<YYYY-MM-DD>.patch
  # Fallback: mtime date, id = "unknown"
  local agent_id date_str
  if [[ "$filename" =~ ^agent-([a-f0-9]+)-([0-9]{4}-[0-9]{2}-[0-9]{2})\.patch$ ]]; then
    agent_id="${BASH_REMATCH[1]}"
    date_str="${BASH_REMATCH[2]}"
  elif [[ "$filename" =~ ^([a-zA-Z0-9_-]+)-([0-9]{4}-[0-9]{2}-[0-9]{2})\.patch$ ]]; then
    agent_id="${BASH_REMATCH[1]}"
    date_str="${BASH_REMATCH[2]}"
  else
    agent_id="unknown"
    date_str=$(date -r "$patch" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
  fi

  # --- Line counts -------------------------------------------------------
  local added=0 removed=0
  local numstat
  numstat=$(git apply --numstat "$patch" 2>/dev/null || true)
  if [[ -n "$numstat" ]]; then
    added=$(echo "$numstat" | awk '{a+=$1} END{print a+0}')
    removed=$(echo "$numstat" | awk '{r+=$2} END{print r+0}')
  else
    # Fallback: count raw +/- lines (subtract diff headers)
    added=$(grep -c '^+' "$patch" 2>/dev/null || echo 0)
    local added_headers
    added_headers=$(grep -c '^+++' "$patch" 2>/dev/null || echo 0)
    removed=$(grep -c '^-' "$patch" 2>/dev/null || echo 0)
    local removed_headers
    removed_headers=$(grep -c '^---' "$patch" 2>/dev/null || echo 0)
    added=$((added - added_headers))
    removed=$((removed - removed_headers))
    [[ $added -lt 0 ]] && added=0
    [[ $removed -lt 0 ]] && removed=0
  fi

  # --- Top 3 files -------------------------------------------------------
  local top_files
  top_files=$(grep -E '^\+\+\+ b/' "$patch" 2>/dev/null \
    | sed 's|^+++ b/||' \
    | sort -u \
    | head -3 \
    | tr '\n' ',' \
    | sed 's/,$//')

  echo -e "${agent_id}\t${date_str}\t${added}\t${removed}\t${top_files}"
}

# ---------------------------------------------------------------------------
# _ot_count_untriaged
#   Prints the count of untriaged patches in the orphan-diffs directory.
# ---------------------------------------------------------------------------
_ot_count_untriaged() {
  local dir
  dir=$(_ot_orphan_diff_dir)
  local count=0
  for patch in "${dir}"/*.patch; do
    [[ -f "$patch" ]] || continue
    local status
    status=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('status', 'untriaged'))
except Exception:
    print('untriaged')
" "${patch}.meta.json" 2>/dev/null || echo "untriaged")
    if [[ "$status" == "untriaged" ]]; then
      ((count++)) || true
    fi
  done
  echo "$count"
}

# ---------------------------------------------------------------------------
# _ot_nudge_if_over_threshold
#   Checks untriaged count against ORPHAN_DIFF_NUDGE_THRESHOLD (default 50).
#   Posts a team-log warning only when BOTH conditions hold:
#     1. pile_size > threshold
#     2. auto-triage processed zero items in each of the last 3 runs
#        (i.e., auto-triage is stuck and not making progress)
#   When auto-triage IS making progress (any run processed > 0 in last 3),
#   the warning is suppressed — the reaper is already working the pile.
#   Non-fatal — wrapped in || true by caller.
# ---------------------------------------------------------------------------
_ot_nudge_if_over_threshold() {
  local threshold="${ORPHAN_DIFF_NUDGE_THRESHOLD:-50}"
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  local untriaged
  untriaged=$(_ot_count_untriaged 2>/dev/null || echo 0)

  local nudge_state_file
  nudge_state_file=$(_ot_nudge_state_file)

  local last_count=0
  local auto_triage_stuck=true  # assume stuck unless state says otherwise

  if [[ -f "$nudge_state_file" ]]; then
    last_count=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('last_count', 0))
except Exception:
    print(0)
" "$nudge_state_file" 2>/dev/null || echo 0)

    # Check if auto-triage processed > 0 in any of the last 3 runs.
    # If so, auto-triage is making progress — suppress the warning.
    local any_progress
    any_progress=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    runs = d.get('auto_triage_runs', [])
    # Any run in the last 3 that processed > 0 means progress is happening
    if any(r.get('processed', 0) > 0 for r in runs[-3:]):
        print('yes')
    else:
        print('no')
except Exception:
    print('no')
" "$nudge_state_file" 2>/dev/null || echo "no")

    if [[ "$any_progress" == "yes" ]]; then
      auto_triage_stuck=false
    fi
  fi

  # Only warn if pile > threshold AND auto-triage is stuck AND count changed
  if [[ "$untriaged" -gt "$threshold" ]] && $auto_triage_stuck && [[ "$untriaged" != "$last_count" ]]; then
    bash "${script_dir}/../rotate-team-log.sh" comment \
      "[$(date +%H:%M)] reaper: orphan-diff pile ${untriaged} untriaged (>${threshold}) — run scripts/triage-orphan-diffs.sh to triage" \
      2>/dev/null || true
    python3 -c "
import json, sys, os, datetime
state_file = sys.argv[1]
try:
    d = json.load(open(state_file))
except Exception:
    d = {}
d['last_nudge_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
d['last_count'] = int(sys.argv[2])
d['threshold'] = int(sys.argv[3])
os.makedirs(os.path.dirname(state_file), exist_ok=True)
with open(state_file, 'w') as f:
    json.dump(d, f, indent=2)
" "$nudge_state_file" "$untriaged" "$threshold" 2>/dev/null || true
  elif [[ "$untriaged" -le "$threshold" ]]; then
    # Pile is under control — update last_count so we re-trigger if it grows again
    python3 -c "
import json, sys, os
state_file = sys.argv[1]
try:
    d = json.load(open(state_file))
except Exception:
    d = {}
d['last_count'] = int(sys.argv[2])
os.makedirs(os.path.dirname(state_file), exist_ok=True)
with open(state_file, 'w') as f:
    json.dump(d, f, indent=2)
" "$nudge_state_file" "$untriaged" 2>/dev/null || true
  fi
}
