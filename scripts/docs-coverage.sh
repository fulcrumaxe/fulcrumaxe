#!/usr/bin/env bash
# docs-coverage.sh — flag wiki pages whose mtime is older than their source module's mtime
#
# Output format (column-separated):
#   wiki-path | source-path | stale?
#
# A row is "stale=YES" when the wiki page's mtime is older than the source module.
# "stale=UNKNOWN" means no mapping could be found for the wiki page.
#
# Usage:
#   bash scripts/docs-coverage.sh
#   WIKI_DIR=wiki SOURCE_DIRS="backend dashboard tui" bash scripts/docs-coverage.sh
#   OUTPUT_FORMAT=tsv bash scripts/docs-coverage.sh
#
# Configuration via env vars (not CLI flags):
#   WIKI_DIR        — directory containing wiki pages (default: wiki)
#   SOURCE_DIRS     — space-separated source dirs to compare against (default: backend dashboard tui scripts)
#   OUTPUT_FORMAT   — table (default) or tsv
#
# Exit code: 0 always (even when stale pages exist — callers inspect the output).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/platform-compat.sh
source "$SCRIPT_DIR/lib/platform-compat.sh"

WIKI_DIR="${WIKI_DIR:-wiki}"
SOURCE_DIRS="${SOURCE_DIRS:-backend dashboard tui scripts}"
OUTPUT_FORMAT="${OUTPUT_FORMAT:-table}"  # table | tsv

# ---------------------------------------------------------------------------
# Mapping: wiki page basename -> source directory or file (heuristic)
# ---------------------------------------------------------------------------
declare -A WIKI_TO_SOURCE=(
  ["Project-Status.md"]="backend"
  ["Changelog.md"]="backend/api.py"
  ["Hook-Contract.md"]="scripts"
  ["Persona-Layer.md"]=".autonomous-team/personas"
  ["PR-Index.md"]="backend"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# mtime_epoch — 0 is this file's own explicit "couldn't determine, treat as
# UNKNOWN" sentinel, checked below wherever this is called. That's a
# deliberate, already-safe use of 0 (unlike the D#2263 bug elsewhere in this
# PR) — routing it through pc_stat_mtime just means it now gets a real mtime
# on a BSD host too, instead of falling straight to the sentinel there.
mtime_epoch() {
  local f="$1"
  if [[ -f "$f" ]]; then
    pc_stat_mtime "$f" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

newest_mtime_in_dir() {
  local dir="$1"
  if [[ ! -d "$dir" ]]; then
    echo 0
    return
  fi
  local newest=0 f mtime
  while IFS= read -r -d '' f; do
    mtime=$(pc_stat_mtime "$f" 2>/dev/null) || continue
    if [[ "$mtime" =~ ^[0-9]+$ ]] && [[ "$mtime" -gt "$newest" ]]; then
      newest="$mtime"
    fi
  done < <(find "$dir" -type f \( -name "*.py" -o -name "*.ts" -o -name "*.tsx" -o -name "*.sh" \) -print0 2>/dev/null)
  echo "$newest"
}

# ---------------------------------------------------------------------------
# Collect wiki pages
# ---------------------------------------------------------------------------

if [[ ! -d "$WIKI_DIR" ]]; then
  echo "# docs-coverage: wiki dir '$WIKI_DIR' not found — nothing to check" >&2
  exit 0
fi

WIKI_PAGES=()
while IFS= read -r -d '' f; do
  WIKI_PAGES+=("$f")
done < <(find "$WIKI_DIR" -name "*.md" -print0 2>/dev/null)

if [[ ${#WIKI_PAGES[@]} -eq 0 ]]; then
  echo "# docs-coverage: no wiki pages found in $WIKI_DIR" >&2
  exit 0
fi

# ---------------------------------------------------------------------------
# Print header
# ---------------------------------------------------------------------------

if [[ "$OUTPUT_FORMAT" == "table" ]]; then
  printf "%-45s | %-35s | %s\n" "wiki-path" "source-path" "stale?"
  printf "%s\n" "$(printf '%.0s-' {1..90})"
fi

# ---------------------------------------------------------------------------
# For each wiki page, find its source counterpart and compare mtimes
# ---------------------------------------------------------------------------

for wiki_page in "${WIKI_PAGES[@]}"; do
  page_basename=$(basename "$wiki_page")
  wiki_mtime=$(mtime_epoch "$wiki_page")

  # Look up explicit mapping first
  source_path="${WIKI_TO_SOURCE[$page_basename]:-}"

  if [[ -z "$source_path" ]]; then
    # Heuristic: try stripping trailing .md, lowercasing, matching a dir
    stem=$(echo "${page_basename%.md}" | tr '[:upper:]' '[:lower:]' | tr '-' '_')
    for sdir in $SOURCE_DIRS; do
      if [[ -d "$sdir/$stem" ]]; then
        source_path="$sdir/$stem"
        break
      fi
      if [[ -f "$sdir/$stem.py" ]]; then
        source_path="$sdir/$stem.py"
        break
      fi
    done
  fi

  if [[ -z "$source_path" ]]; then
    stale="UNKNOWN"
    source_label="(no mapping)"
  else
    if [[ -f "$source_path" ]]; then
      src_mtime=$(mtime_epoch "$source_path")
    elif [[ -d "$source_path" ]]; then
      src_mtime=$(newest_mtime_in_dir "$source_path")
    else
      src_mtime=0
    fi

    if [[ "$src_mtime" -eq 0 ]]; then
      stale="UNKNOWN"
    elif [[ "$wiki_mtime" -lt "$src_mtime" ]]; then
      stale="YES"
    else
      stale="no"
    fi
    source_label="$source_path"
  fi

  if [[ "$OUTPUT_FORMAT" == "tsv" ]]; then
    printf "%s\t%s\t%s\n" "$wiki_page" "$source_label" "$stale"
  else
    printf "%-45s | %-35s | %s\n" "$wiki_page" "$source_label" "$stale"
  fi
done

exit 0
