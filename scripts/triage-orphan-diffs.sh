#!/usr/bin/env bash
# triage-orphan-diffs.sh — inspect and manage the orphan-diff pile.
#
# Usage:
#   scripts/triage-orphan-diffs.sh [list] [--status <filter>] [--json]
#   scripts/triage-orphan-diffs.sh set <patch-name> --status <salvaged|discarded|untriaged> [--note "..."]
#   scripts/triage-orphan-diffs.sh discard-older-than <duration> [--dry-run]
#   scripts/triage-orphan-diffs.sh auto-triage --batch N [--dry-run]
#   scripts/triage-orphan-diffs.sh stats [--json]
#   scripts/triage-orphan-diffs.sh -h | --help
#
# <duration> format: integer + d (days), h (hours), w (weeks). E.g.: 30d, 7d, 2w, 48h
#
# The script NEVER calls `git rm` or `rm` on patches. All archiving uses `git mv`.
#
# Auto-triage safe-discard heuristics (--auto mode):
#   A patch is safe to discard automatically when ALL of the following hold:
#   1. It only touches files in the safe-discard allowlist (see _ot_is_safe_discard)
#   2. It contains no test file changes (no paths matching tests/, test_*, *_test.*)
#   Patches that do NOT match are moved to archive/orphan-diffs-needs-review/ for
#   human inspection. A single summary line is emitted per run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Allow REPO_ROOT override (used by tests)
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Source shared helpers
source "$SCRIPT_DIR/lib/orphan-triage.sh"
# pc_stat_mtime: GNU/BSD `stat` flag styles, probed once per process and
# cached. Used by discard-older-than so reading a patch's age costs no
# interpreter spawn per patch (D#2133).
source "$SCRIPT_DIR/lib/platform-compat.sh"

ORPHAN_DIFF_DIR="${REPO_ROOT}/archive/orphan-diffs"
WORKTREES_JSON="${REPO_ROOT}/.autonomous-team/worktrees.json"

# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------
_print_help() {
  cat <<'HELP'
triage-orphan-diffs.sh — manage the orphan-diff pile

SUBCOMMANDS
  (default)                    Same as: list
  list [--status <s>] [--json] List all patches; optionally filter by status
  set <patch> --status <s> [--note "..."]
                               Tag a patch: untriaged|salvaged|discarded
  discard-older-than <dur> [--dry-run]
                               Archive untriaged patches older than <dur>
                               (uses git mv, never git rm or rm)
  auto-triage --batch N [--dry-run]
                               Non-interactive batch triage: safe patches are
                               discarded; needs-review patches moved to
                               archive/orphan-diffs-needs-review/
  stats [--json]               Print counts: total, untriaged, salvaged, discarded
  -h | --help                  Show this help

EXAMPLES
  scripts/triage-orphan-diffs.sh
  scripts/triage-orphan-diffs.sh list --status untriaged
  scripts/triage-orphan-diffs.sh list --json
  scripts/triage-orphan-diffs.sh set agent-abc123-2026-05-09.patch --status salvaged --note "merged in #410"
  scripts/triage-orphan-diffs.sh discard-older-than 30d
  scripts/triage-orphan-diffs.sh discard-older-than 30d --dry-run
  scripts/triage-orphan-diffs.sh auto-triage --batch 25
  scripts/triage-orphan-diffs.sh auto-triage --batch 25 --dry-run
  scripts/triage-orphan-diffs.sh stats --json
HELP
}

# ---------------------------------------------------------------------------
# _resolve_patch <name-or-id>
#   Given a full filename or bare agent-id, return the full path to the patch.
#   Exits 2 if ambiguous or not found.
# ---------------------------------------------------------------------------
_resolve_patch() {
  local input="$1"
  local matches=()

  # Check if it's already a full filename
  if [[ -f "${ORPHAN_DIFF_DIR}/${input}" ]]; then
    echo "${ORPHAN_DIFF_DIR}/${input}"
    return 0
  fi

  # Also allow absolute path
  if [[ -f "$input" ]]; then
    echo "$input"
    return 0
  fi

  # Search by bare ID substring in filename
  while IFS= read -r -d '' f; do
    local fname
    fname=$(basename "$f")
    if [[ "$fname" == *"${input}"* ]]; then
      matches+=("$f")
    fi
  done < <(find "$ORPHAN_DIFF_DIR" -maxdepth 1 -name '*.patch' -print0 2>/dev/null)

  if [[ ${#matches[@]} -eq 0 ]]; then
    echo "ERROR: no patch found matching '${input}' in ${ORPHAN_DIFF_DIR}" >&2
    exit 2
  fi

  if [[ ${#matches[@]} -gt 1 ]]; then
    echo "ERROR: ambiguous match for '${input}' — matches:" >&2
    for m in "${matches[@]}"; do
      echo "  $(basename "$m")" >&2
    done
    exit 2
  fi

  echo "${matches[0]}"
}

# ---------------------------------------------------------------------------
# _parse_duration <duration>
#   Returns the number of seconds for the given duration string.
#   Exits 1 on invalid input.
# ---------------------------------------------------------------------------
_parse_duration() {
  local dur="$1"
  if [[ "$dur" =~ ^([0-9]+)([dhw])$ ]]; then
    local val="${BASH_REMATCH[1]}"
    local unit="${BASH_REMATCH[2]}"
    case "$unit" in
      d) echo $((val * 86400)) ;;
      h) echo $((val * 3600)) ;;
      w) echo $((val * 7 * 86400)) ;;
    esac
  else
    echo "ERROR: invalid duration '${dur}'. Use format: 30d, 24h, 2w" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# _lookup_agent_role <agent_id>
#   Tries to find the agent role from worktrees.json, falls back to <unknown>.
# ---------------------------------------------------------------------------
_lookup_agent_role() {
  local agent_id="$1"
  if [[ -f "$WORKTREES_JSON" ]]; then
    python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
if isinstance(data, list):
    for entry in data:
        if isinstance(entry, dict) and sys.argv[2] in str(entry.get('id', '')):
            print(entry.get('role', '<unknown>'))
            sys.exit(0)
print('<unknown>')
" "$WORKTREES_JSON" "$agent_id" 2>/dev/null || echo "<unknown>"
  else
    echo "<unknown>"
  fi
}

# ---------------------------------------------------------------------------
# cmd_list [--status <filter>] [--json]
# ---------------------------------------------------------------------------
cmd_list() {
  local filter_status=""
  local json_mode=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status) filter_status="$2"; shift 2 ;;
      --json)   json_mode=true; shift ;;
      *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
  done

  if [[ ! -d "$ORPHAN_DIFF_DIR" ]]; then
    if $json_mode; then
      echo '{"generated_at":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","count":0,"patches":[]}'
    else
      echo "(no archive/orphan-diffs directory found)"
    fi
    return 0
  fi

  # Collect patch data
  local rows=()
  local json_rows=()

  shopt -s nullglob
  for patch in "${ORPHAN_DIFF_DIR}"/*.patch; do
    [[ -f "$patch" ]] || continue

    local fname
    fname=$(basename "$patch")

    # Read meta
    local meta_json
    meta_json=$(_ot_meta_read "$patch")
    local status note
    status=$(echo "$meta_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','untriaged'))" 2>/dev/null || echo "untriaged")
    note=$(echo "$meta_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('note',''))" 2>/dev/null || echo "")

    # Apply filter
    if [[ -n "$filter_status" ]] && [[ "$status" != "$filter_status" ]]; then
      continue
    fi

    # Parse patch
    local parsed
    parsed=$(_ot_parse_patch "$patch" 2>/dev/null || echo "unknown	unknown	0	0	")
    local agent_id date_str added removed top_files
    IFS=$'\t' read -r agent_id date_str added removed top_files <<< "$parsed"

    # Look up role
    local role
    role=$(_lookup_agent_role "$agent_id")

    # Truncate top_files to 60 chars
    local top_files_display="$top_files"
    if [[ ${#top_files} -gt 60 ]]; then
      top_files_display="${top_files:0:59}…"
    fi
    [[ -z "$top_files_display" ]] && top_files_display="-"

    local note_display="${note:-"-"}"
    [[ -z "$note_display" ]] && note_display="-"

    rows+=("${fname}|${date_str}|${role}|${added}|${removed}|${top_files_display}|${status}|${note_display}")

    # Build JSON row
    local top_files_json
    top_files_json=$(echo "$top_files" | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
if raw:
    files = [f.strip() for f in raw.split(',') if f.strip()]
else:
    files = []
print(json.dumps(files))
" 2>/dev/null || echo "[]")

    json_rows+=("$(python3 -c "
import json, sys
print(json.dumps({
    'patch': sys.argv[1],
    'path': sys.argv[2],
    'agent_id': sys.argv[3],
    'agent_role': sys.argv[4],
    'date': sys.argv[5],
    'lines_added': int(sys.argv[6]),
    'lines_removed': int(sys.argv[7]),
    'top_files': json.loads(sys.argv[8]),
    'status': sys.argv[9],
    'note': sys.argv[10],
}))
" "$fname" "$patch" "$agent_id" "$role" "$date_str" "$added" "$removed" "$top_files_json" "$status" "$note" 2>/dev/null)")
  done
  shopt -u nullglob

  if $json_mode; then
    local count=${#json_rows[@]}
    local generated_at
    generated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    python3 -c "
import json, sys
rows = [json.loads(line) for line in sys.stdin if line.strip()]
print(json.dumps({
    'generated_at': sys.argv[1],
    'count': len(rows),
    'patches': rows,
}, indent=2))
" "$generated_at" <<< "$(printf '%s\n' "${json_rows[@]+"${json_rows[@]}"}")"
    return 0
  fi

  # Table output
  if [[ ${#rows[@]} -eq 0 ]]; then
    echo "(no patches found)"
    return 0
  fi

  printf "%-52s %-12s %-16s %6s %7s  %-36s %-12s %s\n" \
    "PATCH" "DATE" "AGENT-ROLE" "ADDED" "REMOVED" "TOP-FILES" "STATUS" "NOTE"
  printf "%s\n" "$(printf '%.0s-' {1..160})"
  for row in "${rows[@]}"; do
    IFS='|' read -r r_fname r_date r_role r_added r_removed r_top r_status r_note <<< "$row"
    printf "%-52s %-12s %-16s %6s %7s  %-36s %-12s %s\n" \
      "$r_fname" "$r_date" "$r_role" "$r_added" "$r_removed" "$r_top" "$r_status" "$r_note"
  done
}

# ---------------------------------------------------------------------------
# cmd_set <patch-name> --status <s> [--note "..."]
# ---------------------------------------------------------------------------
cmd_set() {
  if [[ $# -eq 0 ]]; then
    echo "ERROR: 'set' requires a patch name. See --help." >&2
    exit 1
  fi

  local patch_input="$1"; shift
  local new_status=""
  local new_note=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status) new_status="$2"; shift 2 ;;
      --note)   new_note="$2"; shift 2 ;;
      *) echo "ERROR: unknown option '$1'" >&2; exit 1 ;;
    esac
  done

  if [[ -z "$new_status" ]]; then
    echo "ERROR: --status is required for 'set'" >&2
    exit 1
  fi

  # Validate status value early (nice error before resolving patch)
  case "$new_status" in
    untriaged|salvaged|discarded) ;;
    *)
      echo "ERROR: invalid status '${new_status}'. Must be: untriaged, salvaged, discarded" >&2
      exit 2
      ;;
  esac

  local patch_path
  patch_path=$(_resolve_patch "$patch_input")

  _ot_meta_write "$patch_path" "$new_status" "$new_note"
  echo "Updated $(basename "$patch_path").meta.json: status=${new_status}${new_note:+, note=${new_note}}"
}

# ---------------------------------------------------------------------------
# cmd_discard_older_than <duration> [--dry-run]
# ---------------------------------------------------------------------------
cmd_discard_older_than() {
  if [[ $# -eq 0 ]]; then
    echo "ERROR: 'discard-older-than' requires a duration. E.g.: 30d" >&2
    exit 1
  fi

  local duration="$1"; shift
  local dry_run=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=true; shift ;;
      *) echo "ERROR: unknown option '$1'" >&2; exit 1 ;;
    esac
  done

  # Verify git mv is available
  if ! git -C "$REPO_ROOT" --version >/dev/null 2>&1; then
    echo "ERROR: git is not available — cannot proceed safely" >&2
    exit 1
  fi

  local seconds
  seconds=$(_parse_duration "$duration")
  local cutoff
  cutoff=$(date -d "-${seconds} seconds" +%s 2>/dev/null \
    || python3 -c "import time; print(int(time.time()) - int('$seconds'))")

  local today
  today=$(date +%Y-%m-%d)
  local target_dir="${REPO_ROOT}/archive/orphan-diffs-discarded-${today}"

  if [[ ! -d "$ORPHAN_DIFF_DIR" ]]; then
    echo "(no archive/orphan-diffs directory — nothing to discard)"
    return 0
  fi

  # Probe the host's `stat` flavour once, up front. pc_stat_mtime caches a
  # successful probe but not a failed one, so a host where neither GNU
  # `stat -c` nor BSD `stat -f` works would otherwise re-probe once per patch.
  # If we cannot read mtimes at all, every patch's age is unknown, so refuse
  # rather than discard on a guess.
  if ! pc_stat_mtime "$ORPHAN_DIFF_DIR" >/dev/null 2>&1; then
    echo "ERROR: cannot read file mtimes on this host — neither GNU 'stat -c' nor BSD 'stat -f' works. Refusing to discard anything, because every patch's age would be unknown." >&2
    return 1
  fi

  # Find candidate patches (D#2133).
  #
  # Two passes, and neither spawns an interpreter per patch. Pass 1 partitions
  # the pile with shell builtins and `stat`; pass 2 hands every sidecar that
  # actually exists to a single interpreter.
  local candidates=()
  local with_meta=()
  local unreadable=()
  local unrecognised_paths=()
  local unrecognised_statuses=()
  shopt -s nullglob
  for patch in "${ORPHAN_DIFF_DIR}"/*.patch; do
    [[ -f "$patch" ]] || continue

    # Check mtime. pc_stat_mtime supplies no fallback of its own on purpose,
    # so the caller picks the safe default: a patch whose age we cannot
    # establish is treated as too recent and kept. Keeping a patch we could
    # have discarded costs disk; discarding one we could not age costs
    # somebody their work.
    local mtime
    if ! mtime=$(pc_stat_mtime "$patch"); then
      continue
    fi

    if [[ "$mtime" -ge "$cutoff" ]]; then
      continue  # too recent
    fi

    # An absent sidecar is the ordinary case and means untriaged — 100% of the
    # patches on the operator checkout take this branch, and it costs no
    # process at all. `-e` rather than `-f` so that a sidecar path holding
    # something other than a regular file — a directory, say — is sent to the
    # reader and comes back unreadable, instead of being mistaken for absent.
    #
    # `-e` follows symlinks, so one case does NOT get that treatment: a
    # sidecar that is a symlink to a missing target reads as absent here, and
    # the patch stays discard-eligible. That is the same answer the previous
    # code gave, so it is not a change, but it is not what "exists but could
    # not be read" would suggest either, and it is written down rather than
    # left for the next reader to discover.
    if [[ ! -e "${patch}.meta.json" ]]; then
      candidates+=("$patch")
      continue
    fi

    with_meta+=("$patch")
  done
  shopt -u nullglob

  # Pass 2 — one interpreter for every sidecar that exists, not one each.
  if [[ ${#with_meta[@]} -gt 0 ]]; then
    local meta_out=""
    if ! meta_out=$(printf '%s.meta.json\0' "${with_meta[@]}" | _ot_read_meta_statuses); then
      : # partial output is still usable; unreported patches are caught below
    fi

    # Match results to patches BY POSITION. The reader emits one line per
    # input path, in input order, and deliberately does not send the path
    # back. Re-parsing a path out of the record is what let a patch whose
    # filename contained a newline split one record in two, so that the front
    # half named a different, real patch — which was then discarded on
    # somebody else's status, with its sidecar rewritten on the way out.
    # Position carries no filename, so there is nothing here to split.
    local idx=0
    local kind status
    while IFS=$'\t' read -r kind status; do
      [[ -n "$kind" ]] || continue
      # More lines than inputs means the framing no longer lines up, and a
      # misaligned result is exactly what this loop exists to prevent. Stop
      # consuming; the tail is caught as unreported below.
      [[ "$idx" -lt "${#with_meta[@]}" ]] || break
      patch="${with_meta[$idx]}"
      idx=$((idx + 1))
      if [[ "$kind" != "R" ]]; then
        unreadable+=("$patch")
      elif [[ "$status" == "untriaged" ]]; then
        candidates+=("$patch")
      elif [[ "$status" == "salvaged" || "$status" == "discarded" || "$status" == "needs-review" ]]; then
        : # already triaged by somebody, leave it alone
      else
        # A status nobody recognises is not a licence to discard, and it is
        # not something to pass over in silence either — kept-but-unmentioned
        # is the same shape of defect as discarded-without-saying-why.
        unrecognised_paths+=("$patch")
        unrecognised_statuses+=("$status")
      fi
    done <<< "$meta_out"

    # A reader that died part-way stops emitting, so the trailing inputs go
    # unreported. They are unreadable by the same argument as an explicit U
    # line: we did not learn the status, so we must not treat the patch as
    # untriaged.
    while [[ "$idx" -lt "${#with_meta[@]}" ]]; do
      unreadable+=("${with_meta[$idx]}")
      idx=$((idx + 1))
    done
  fi

  # Say so out loud. A sidecar we could not read is a patch we deliberately
  # kept, not a patch that quietly did not come up — the whole defect being
  # fixed here is an unreadable sidecar reading as a clean "untriaged".
  if [[ ${#unreadable[@]} -gt 0 ]]; then
    echo "WARNING: ${#unreadable[@]} patch(es) have a sidecar that could not be read or parsed. Keeping them — an unreadable sidecar is not 'untriaged' and is never discard-eligible:" >&2
    local u
    for u in "${unreadable[@]}"; do
      echo "  ${u##*/} — ${u##*/}.meta.json is unreadable or not valid JSON" >&2
    done
  fi

  if [[ ${#unrecognised_paths[@]} -gt 0 ]]; then
    echo "WARNING: ${#unrecognised_paths[@]} patch(es) have a sidecar with an unrecognised status. Keeping them — only 'untriaged' is discard-eligible:" >&2
    local i
    for i in "${!unrecognised_paths[@]}"; do
      echo "  ${unrecognised_paths[$i]##*/} — status '${unrecognised_statuses[$i]}' is not one of untriaged/salvaged/discarded/needs-review" >&2
    done
  fi

  if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "No untriaged patches older than ${duration} found — nothing to discard."
    return 0
  fi

  if $dry_run; then
    echo "DRY RUN — would discard ${#candidates[@]} patch(es) into ${target_dir}:"
    for p in "${candidates[@]}"; do
      echo "  $(basename "$p")"
    done
    return 0
  fi

  # Create target directory
  mkdir -p "$target_dir"

  # Generate README.md for target dir
  local readme="${target_dir}/README.md"
  if [[ ! -f "$readme" ]]; then
    cat > "$readme" <<README
# Discarded Orphan Diffs — ${today}

**When removed:** ${today} (auto-aged-out)
**Why removed:** Patches were untriaged and older than ${duration}. Moved by \`scripts/triage-orphan-diffs.sh discard-older-than ${duration}\`.
**Original path:** \`archive/orphan-diffs/<patch-name>\`
**How to restore:** \`git mv archive/orphan-diffs-discarded-${today}/<patch-name> archive/orphan-diffs/<patch-name>\`

Files in this directory were automatically archived from \`archive/orphan-diffs/\`
because they were untriaged for more than ${duration}. To recover a patch, restore
it with the command above and re-run \`scripts/triage-orphan-diffs.sh\` to triage.
README
    git -C "$REPO_ROOT" add "$readme" 2>/dev/null || true
  fi

  local count=0
  for patch in "${candidates[@]}"; do
    local fname
    fname=$(basename "$patch")
    local meta="${patch}.meta.json"

    # Write/update sidecar before move, then stage it so git mv works
    _ot_meta_write "$patch" "discarded" "auto-aged-out at >${duration} untriaged"
    git -C "$REPO_ROOT" add "${patch}.meta.json" 2>/dev/null || true

    # git mv patch
    git -C "$REPO_ROOT" mv "$patch" "${target_dir}/${fname}"

    # git mv sidecar (always present now — written above)
    if [[ -f "${patch}.meta.json" ]]; then
      git -C "$REPO_ROOT" add "${patch}.meta.json" 2>/dev/null || true
      git -C "$REPO_ROOT" mv "${patch}.meta.json" "${target_dir}/${fname}.meta.json"
    fi

    ((count++)) || true
  done

  echo "Discarded ${count} patch(es) older than ${duration} into ${target_dir}/"
}

# ---------------------------------------------------------------------------
# _ot_is_safe_discard <patch_path>
#   Returns 0 (true) when the patch only touches files in the safe-discard
#   allowlist and has no test-file changes.
#
# Safe-discard allowlist (paths that are auto-generated, ephemeral, or log-like):
#   .autonomous-team/           (team state, hook events, logs)
#   archive/                    (already-archived material)
#   .claude/                    (Claude Code harness state)
#
# Unsafe-if-present patterns:
#   tests/                      (test code — always needs human review)
#   test_*.py / *_test.py       (Python test files)
#   test_*.sh / *_test.sh       (shell test files)
#   scripts/                    (tooling changes — risky to auto-discard)
#   backend/                    (application code)
#   tui/ / dashboard/           (frontend code)
#   *.md in wiki/               (documentation changes worth reviewing)
# ---------------------------------------------------------------------------
_ot_is_safe_discard() {
  local patch="$1"

  # Extract all file paths touched by this patch (b-side = new path)
  local touched_files
  touched_files=$(grep -E '^\+\+\+ b/' "$patch" 2>/dev/null | sed 's|^+++ b/||' | sort -u)

  if [[ -z "$touched_files" ]]; then
    # Empty patch or unparseable — treat as safe (nothing to lose)
    return 0
  fi

  # Check each touched file against the safe/unsafe rules
  while IFS= read -r fpath; do
    [[ -z "$fpath" ]] && continue

    # Unsafe patterns — any match means NOT safe to auto-discard
    if [[ "$fpath" == tests/* ]] || \
       [[ "$fpath" == test_*.py ]] || \
       [[ "$fpath" == *_test.py ]] || \
       [[ "$fpath" == test_*.sh ]] || \
       [[ "$fpath" == *_test.sh ]] || \
       [[ "$fpath" == scripts/* ]] || \
       [[ "$fpath" == backend/* ]] || \
       [[ "$fpath" == tui/* ]] || \
       [[ "$fpath" == dashboard/* ]] || \
       [[ "$fpath" == wiki/*.md ]]; then
      return 1
    fi

    # Safe allowlist — these paths are always ok to auto-discard
    if [[ "$fpath" == .autonomous-team/* ]] || \
       [[ "$fpath" == archive/* ]] || \
       [[ "$fpath" == .claude/* ]]; then
      continue
    fi

    # Anything else (unknown paths) — not safe to auto-discard without human review
    return 1
  done <<< "$touched_files"

  return 0
}

# ---------------------------------------------------------------------------
# cmd_auto_triage --batch N [--dry-run]
#   Non-interactive triage of untriaged patches.
#   - Processes at most N patches per run (batch cap).
#   - Safe patches (matching _ot_is_safe_discard) → discarded (git mv to archive/orphan-diffs-discarded-YYYY-MM-DD/)
#   - Needs-review patches → moved to archive/orphan-diffs-needs-review/ for human inspection
#   - Prints a one-line summary.
#   - Writes auto_triage_processed count to nudge-state so the warning-suppression
#     logic can detect when auto-triage is making progress.
# ---------------------------------------------------------------------------
cmd_auto_triage() {
  local batch_size=25
  local dry_run=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --batch)   batch_size="$2"; shift 2 ;;
      --dry-run) dry_run=true; shift ;;
      --auto)    shift ;;  # accepted for back-compat, already implied by this subcommand
      *) echo "ERROR: unknown option '$1'" >&2; exit 1 ;;
    esac
  done

  if [[ ! "$batch_size" =~ ^[0-9]+$ ]] || [[ "$batch_size" -lt 1 ]]; then
    echo "ERROR: --batch must be a positive integer, got '${batch_size}'" >&2
    exit 1
  fi

  if [[ ! -d "$ORPHAN_DIFF_DIR" ]]; then
    echo "auto-triage: no orphan-diffs directory — nothing to process"
    _ot_record_auto_triage_run 0 0 0
    return 0
  fi

  local today
  today=$(date +%Y-%m-%d)
  local discard_dir="${REPO_ROOT}/archive/orphan-diffs-discarded-${today}"
  local review_dir="${REPO_ROOT}/archive/orphan-diffs-needs-review"

  # Collect untriaged patches (up to batch_size).
  #
  # A missing sidecar is the ordinary case and means untriaged. A sidecar
  # that exists but cannot be opened or parsed does NOT mean untriaged — it
  # means we don't know, and this is a destructive path (D#2461, same
  # collapse D#2133 fixed on discard-older-than). Reuse the same reader
  # rather than re-deriving the distinction with a fresh `except Exception:
  # print('untriaged')`.
  local candidates=()
  local unreadable_meta=()
  shopt -s nullglob
  for patch in "${ORPHAN_DIFF_DIR}"/*.patch; do
    [[ -f "$patch" ]] || continue
    [[ ${#candidates[@]} -ge "$batch_size" ]] && break

    if [[ ! -e "${patch}.meta.json" ]]; then
      candidates+=("$patch")
      continue
    fi

    local kind status
    IFS=$'\t' read -r kind status < <(printf '%s.meta.json\0' "$patch" | _ot_read_meta_statuses)
    if [[ "$kind" != "R" ]]; then
      # Sidecar exists but could not be opened or parsed. Never
      # discard-eligible — keep it in place and say so, don't skip silently.
      unreadable_meta+=("$patch")
    elif [[ "$status" == "untriaged" ]]; then
      candidates+=("$patch")
    fi
  done
  shopt -u nullglob

  if [[ ${#unreadable_meta[@]} -gt 0 ]]; then
    echo "WARNING: ${#unreadable_meta[@]} patch(es) have a sidecar that could not be read or parsed. Keeping them — an unreadable sidecar is not 'untriaged' and is never discard-eligible:" >&2
    local u
    for u in "${unreadable_meta[@]}"; do
      echo "  ${u##*/} — ${u##*/}.meta.json is unreadable or not valid JSON" >&2
    done
  fi

  local total_candidates=${#candidates[@]}
  if [[ "$total_candidates" -eq 0 ]]; then
    echo "auto-triage: 0 untriaged patches in batch — nothing to process"
    _ot_record_auto_triage_run 0 0 0
    return 0
  fi

  local safe_count=0
  local review_count=0

  for patch in "${candidates[@]}"; do
    local fname
    fname=$(basename "$patch")

    if _ot_is_safe_discard "$patch"; then
      # Safe to auto-discard
      if $dry_run; then
        echo "  [dry-run] would discard: ${fname}"
      else
        mkdir -p "$discard_dir"
        _ensure_discard_dir_readme "$discard_dir" "$today"
        _ot_meta_write "$patch" "discarded" "auto-triage: safe-discard heuristic"
        git -C "$REPO_ROOT" add "${patch}.meta.json" 2>/dev/null || true
        git -C "$REPO_ROOT" mv "$patch" "${discard_dir}/${fname}" 2>/dev/null || true
        if [[ -f "${patch}.meta.json" ]]; then
          git -C "$REPO_ROOT" mv "${patch}.meta.json" "${discard_dir}/${fname}.meta.json" 2>/dev/null || true
        fi
      fi
      ((safe_count++)) || true
    else
      # Needs human review — move to review dir
      if $dry_run; then
        echo "  [dry-run] would move to needs-review: ${fname}"
      else
        mkdir -p "$review_dir"
        _ensure_review_dir_readme "$review_dir"
        _ot_meta_write "$patch" "needs-review" "auto-triage: moved to needs-review"
        git -C "$REPO_ROOT" add "${patch}.meta.json" 2>/dev/null || true
        git -C "$REPO_ROOT" mv "$patch" "${review_dir}/${fname}" 2>/dev/null || true
        if [[ -f "${patch}.meta.json" ]]; then
          git -C "$REPO_ROOT" mv "${patch}.meta.json" "${review_dir}/${fname}.meta.json" 2>/dev/null || true
        fi
      fi
      ((review_count++)) || true
    fi
  done

  local processed=$(( safe_count + review_count ))
  local summary="auto-triage: processed ${processed}/${total_candidates} (batch=${batch_size}): ${safe_count} discarded, ${review_count} moved to needs-review"
  if $dry_run; then
    summary="[dry-run] ${summary}"
  fi
  echo "$summary"

  if ! $dry_run; then
    _ot_record_auto_triage_run "$processed" "$safe_count" "$review_count"
    # Post needs-review summary to team-log if any
    if [[ "$review_count" -gt 0 ]]; then
      local script_dir_parent
      script_dir_parent="$(cd "$SCRIPT_DIR" && pwd)"
      bash "${script_dir_parent}/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] auto-triage: ${review_count} patch(es) need human review in archive/orphan-diffs-needs-review/" \
        2>/dev/null || true
    fi
  fi
}

# ---------------------------------------------------------------------------
# _ensure_discard_dir_readme <dir> <date>
#   Creates a README.md in the discard directory if it doesn't already exist.
# ---------------------------------------------------------------------------
_ensure_discard_dir_readme() {
  local dir="$1"
  local date="$2"
  local readme="${dir}/README.md"
  [[ -f "$readme" ]] && return 0
  cat > "$readme" <<README
# Auto-Discarded Orphan Diffs — ${date}

**When removed:** ${date} (auto-triage)
**Why removed:** Patches matched the safe-discard heuristic (only touched ephemeral/auto-generated paths, no test or application code).
**Original path:** \`archive/orphan-diffs/<patch-name>\`
**How to restore:** \`git mv archive/orphan-diffs-discarded-${date}/<patch-name> archive/orphan-diffs/<patch-name>\`

Safe-discard allowlist: .autonomous-team/, archive/, .claude/
Unsafe patterns (trigger needs-review instead): tests/, scripts/, backend/, tui/, dashboard/, wiki/*.md
README
  git -C "$REPO_ROOT" add "$readme" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# _ensure_review_dir_readme <dir>
#   Creates a README.md in the needs-review directory if it doesn't already exist.
# ---------------------------------------------------------------------------
_ensure_review_dir_readme() {
  local dir="$1"
  local readme="${dir}/README.md"
  [[ -f "$readme" ]] && return 0
  cat > "$readme" <<README
# Orphan Diffs Needing Review

Patches in this directory were moved here by auto-triage because they touch
application code, tests, or scripts that require human judgment before discarding.

To inspect a patch:
  git diff --stat < <patch-file>
  git apply --check <patch-file>

To mark as reviewed and discard:
  bash scripts/triage-orphan-diffs.sh set <patch-name> --status discarded --note "reason"
  git mv archive/orphan-diffs-needs-review/<patch-name> archive/orphan-diffs-discarded-<date>/

To salvage (apply the work):
  git apply <patch-file>
  # review, commit, and open a PR
  bash scripts/triage-orphan-diffs.sh set <patch-name> --status salvaged --note "applied in PR #N"
README
  git -C "$REPO_ROOT" add "$readme" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# _ot_record_auto_triage_run <processed> <safe_count> <review_count>
#   Updates nudge-state with auto-triage metrics so the warning-suppression
#   logic knows whether auto-triage made progress.
# ---------------------------------------------------------------------------
_ot_record_auto_triage_run() {
  local processed="$1"
  local safe_count="$2"
  local review_count="$3"
  local nudge_state_file
  nudge_state_file=$(_ot_nudge_state_file)

  python3 -c "
import json, sys, os, datetime

state_file = sys.argv[1]
processed = int(sys.argv[2])
safe_count = int(sys.argv[3])
review_count = int(sys.argv[4])
now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

try:
    d = json.load(open(state_file))
except Exception:
    d = {}

# Keep a rolling window of last 3 auto-triage run results
runs = d.get('auto_triage_runs', [])
runs.append({'at': now, 'processed': processed})
d['auto_triage_runs'] = runs[-3:]  # keep last 3 only
d['last_auto_triage_at'] = now
d['last_auto_triage_processed'] = processed

os.makedirs(os.path.dirname(state_file), exist_ok=True)
with open(state_file, 'w') as f:
    json.dump(d, f, indent=2)
" "$nudge_state_file" "$processed" "$safe_count" "$review_count" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# cmd_stats [--json]
# ---------------------------------------------------------------------------
cmd_stats() {
  local json_mode=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json_mode=true; shift ;;
      *) echo "ERROR: unknown option '$1'" >&2; exit 1 ;;
    esac
  done

  local total=0 untriaged=0 salvaged=0 discarded=0 needs_review=0
  local oldest_untriaged_days=0
  local now
  now=$(date +%s)

  shopt -s nullglob
  for patch in "${ORPHAN_DIFF_DIR}"/*.patch; do
    [[ -f "$patch" ]] || continue
    ((total++)) || true

    local status
    status=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('status', 'untriaged'))
except Exception:
    print('untriaged')
" "${patch}.meta.json" 2>/dev/null || echo "untriaged")

    case "$status" in
      untriaged)
        ((untriaged++)) || true
        local mtime
        mtime=$(python3 -c "import os; print(int(os.path.getmtime('$patch')))" 2>/dev/null || echo "$now")
        local age_days=$(( (now - mtime) / 86400 ))
        if [[ "$age_days" -gt "$oldest_untriaged_days" ]]; then
          oldest_untriaged_days="$age_days"
        fi
        ;;
      salvaged)      ((salvaged++)) || true ;;
      discarded)     ((discarded++)) || true ;;
      needs-review)  ((needs_review++)) || true ;;
    esac
  done
  shopt -u nullglob

  local threshold="${ORPHAN_DIFF_NUDGE_THRESHOLD:-50}"
  local over_threshold="false"
  [[ "$untriaged" -gt "$threshold" ]] && over_threshold="true"

  if $json_mode; then
    python3 -c "
import json, sys
print(json.dumps({
    'generated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'total': int(sys.argv[1]),
    'untriaged': int(sys.argv[2]),
    'salvaged': int(sys.argv[3]),
    'discarded': int(sys.argv[4]),
    'needs_review': int(sys.argv[5]),
    'oldest_untriaged_age_days': int(sys.argv[6]),
    'threshold': int(sys.argv[7]),
    'over_threshold': sys.argv[8] == 'true',
}, indent=2))
" "$total" "$untriaged" "$salvaged" "$discarded" "$needs_review" "$oldest_untriaged_days" "$threshold" "$over_threshold"
  else
    echo "Orphan-diff stats:"
    printf "  %-20s %d\n" "total" "$total"
    printf "  %-20s %d\n" "untriaged" "$untriaged"
    printf "  %-20s %d\n" "salvaged" "$salvaged"
    printf "  %-20s %d\n" "discarded" "$discarded"
    printf "  %-20s %d\n" "needs_review" "$needs_review"
    printf "  %-20s %d\n" "oldest_untriaged_days" "$oldest_untriaged_days"
    printf "  %-20s %d\n" "threshold" "$threshold"
    printf "  %-20s %s\n" "over_threshold" "$over_threshold"
  fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
  if [[ $# -eq 0 ]]; then
    cmd_list
    return
  fi

  case "$1" in
    list)             shift; cmd_list "$@" ;;
    set)              shift; cmd_set "$@" ;;
    discard-older-than) shift; cmd_discard_older_than "$@" ;;
    auto-triage)      shift; cmd_auto_triage "$@" ;;
    stats)            shift; cmd_stats "$@" ;;
    --json)           cmd_list --json ;;
    -h|--help)        _print_help ;;
    *)
      # If first arg looks like a flag, treat as list argument
      if [[ "$1" == --* ]]; then
        cmd_list "$@"
      else
        echo "ERROR: unknown subcommand '$1'. See --help." >&2
        exit 1
      fi
      ;;
  esac
}

main "$@"
