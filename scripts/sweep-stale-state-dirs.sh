#!/usr/bin/env bash
# scripts/sweep-stale-state-dirs.sh — enumerate (and, only when asked, quarantine)
# abandoned ~/.<project>-state directories.
#
# Usage:
#   bash scripts/sweep-stale-state-dirs.sh                      # dry run, writes nothing
#   bash scripts/sweep-stale-state-dirs.sh --older-than-days 60
#   bash scripts/sweep-stale-state-dirs.sh --apply              # move candidates to quarantine
#   bash scripts/sweep-stale-state-dirs.sh --root /tmp/fixture  # sweep somewhere else
#
# Options:
#   --root DIR              directory holding the .<name>-state dirs (default: $HOME)
#   --older-than-days N     nothing touched inside for N days is stale (default: 30)
#   --quarantine DIR        where --apply moves candidates
#                           (default: <root>/.state-dirs-quarantine-<UTC timestamp>)
#   --apply                 actually move the candidates. Without this the
#                           script only prints.
#
# Selection rule — a directory is a CANDIDATE only when ALL THREE hold:
#   1. it contains no dashboard-runtime.json (nothing has ever advertised a
#      running dashboard for it),
#   2. no live process is using it, and
#   3. nothing inside it has been modified within the threshold.
#
# It is NEVER selected by name pattern. That is not a stylistic preference:
# on 2026-09-03 a manual sweep of "stale state dirs" on the operator's host
# would have deleted the state dir of an active project with live processes
# had it matched on names. Size and mtime enumeration is what separated the
# 68 real fixtures from the 11 real projects (D#2317). Every directory is
# printed with its size and last-modified time, candidate or not, and with
# the reason it was kept, so the operator can check that separation by eye
# before anything moves.
#
# --apply MOVES. It never deletes. The quarantine directory is a reversible
# stopgap; `mv` back is the undo.
#
# Conservative failure direction: if /proc cannot be enumerated, the
# live-process check cannot be trusted, and the script refuses to nominate
# anything rather than guessing that nothing is running.

set -uo pipefail

ROOT="$HOME"
OLDER_THAN_DAYS=30
QUARANTINE=""
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:?--root needs a directory}"; shift 2 ;;
    --older-than-days) OLDER_THAN_DAYS="${2:?--older-than-days needs a number}"; shift 2 ;;
    --quarantine) QUARANTINE="${2:?--quarantine needs a directory}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! "$OLDER_THAN_DAYS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --older-than-days must be a non-negative integer (got: $OLDER_THAN_DAYS)" >&2
  exit 2
fi
if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: --root is not a directory: $ROOT" >&2
  exit 2
fi

CUTOFF=$(( $(date +%s) - OLDER_THAN_DAYS * 86400 ))

# ── live-process detection ───────────────────────────────────────────────────
# A directory is in use if any visible process has its cwd inside it, or names
# it in argv or its environment. Processes belonging to other users are not
# readable; that is a real blind spot, so it is stated here rather than hidden
# behind a confident answer.
PROC_READABLE=0
if [[ -d /proc ]] && compgen -G "/proc/[0-9]*" > /dev/null 2>&1; then
  PROC_READABLE=1
fi

dir_in_use() {
  local dir="$1" pid cwd real
  real="$(realpath "$dir" 2>/dev/null || printf '%s' "$dir")"
  # One pass per process rather than a glob of every /proc/*/cmdline at once:
  # the glob would be an argv list as long as the process table.
  for pid in /proc/[0-9]*; do
    cwd="$(readlink "$pid/cwd" 2>/dev/null)"
    if [[ -n "$cwd" ]]; then
      case "$cwd" in "$real"|"$real"/*) return 0 ;; esac
    fi
    if grep -qaF -- "$real" "$pid/cmdline" "$pid/environ" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

# ── enumerate ────────────────────────────────────────────────────────────────
CANDIDATES=()
printf '%s\n' "sweep-stale-state-dirs: root=$ROOT threshold=${OLDER_THAN_DAYS}d mode=$([[ $APPLY -eq 1 ]] && echo apply || echo dry-run)"
if [[ $PROC_READABLE -eq 0 ]]; then
  echo "WARN: /proc is not enumerable here — the live-process check cannot run, so nothing will be nominated." >&2
fi
echo ""

shopt -s nullglob dotglob
for dir in "$ROOT"/.*-state; do
  [[ -d "$dir" ]] || continue

  size="$(du -sh "$dir" 2>/dev/null | cut -f1)"
  mtime_epoch="$(find "$dir" -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)"
  [[ -z "$mtime_epoch" ]] && mtime_epoch=0
  mtime_h="$(date -d "@$mtime_epoch" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown)"

  reason=""
  if [[ $PROC_READABLE -eq 0 ]]; then
    reason="cannot verify process use"
  elif [[ -f "$dir/dashboard-runtime.json" ]]; then
    reason="has dashboard-runtime.json"
  elif dir_in_use "$dir"; then
    reason="live process using it"
  elif [[ "$mtime_epoch" -ge "$CUTOFF" ]]; then
    reason="modified within ${OLDER_THAN_DAYS}d"
  fi

  if [[ -n "$reason" ]]; then
    printf 'KEEP       %-8s  %-16s  %s  (%s)\n' "$size" "$mtime_h" "$dir" "$reason"
  else
    printf 'CANDIDATE  %-8s  %-16s  %s\n' "$size" "$mtime_h" "$dir"
    CANDIDATES+=("$dir")
  fi
done
shopt -u nullglob dotglob

echo ""
echo "${#CANDIDATES[@]} candidate(s)."

if [[ $APPLY -eq 0 ]]; then
  echo "Dry run — nothing moved. Re-run with --apply to move the candidates above to a quarantine directory."
  exit 0
fi

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo "Nothing to move."
  exit 0
fi

if [[ -z "$QUARANTINE" ]]; then
  QUARANTINE="$ROOT/.state-dirs-quarantine-$(date -u '+%Y-%m-%dT%H%M%SZ')"
fi
mkdir -p "$QUARANTINE" || { echo "ERROR: could not create quarantine dir $QUARANTINE" >&2; exit 1; }

for dir in "${CANDIDATES[@]}"; do
  mv -- "$dir" "$QUARANTINE/" && echo "MOVED      $dir -> $QUARANTINE/"
done

echo ""
echo "Moved ${#CANDIDATES[@]} directory(ies) to $QUARANTINE"
echo "Nothing was deleted. To undo: mv \"$QUARANTINE\"/* \"$ROOT\"/"
