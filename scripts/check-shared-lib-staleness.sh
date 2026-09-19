#!/usr/bin/env bash
# scripts/check-shared-lib-staleness.sh — advisory: does <subject-dir> in this
# checkout match the code plane's main? (D#2534)
#
# Compares by content (git blob SHA), never by commit ancestry. The engine
# checkout and the code plane share no history since the cutover, so an
# ancestry-based check would report every shared file as diverged.
#
# Reads the code plane through `gh api` rather than fetching a remote — one
# recursive tree listing plus one blob read per differing file — so it never
# touches a git remote in a checkout other agents may be using concurrently.
#
# Advisory only: this script ALWAYS exits 0. It reports on stdout; it never
# blocks anything that calls it. A file, or the whole code plane, that
# cannot be read is reported as UNREADABLE / UNREACHABLE — never folded into
# "identical", which would hide the exact case an operator needs to see.
#
# Usage:
#   bash scripts/check-shared-lib-staleness.sh [subject-dir]
#   subject-dir defaults to scripts/lib. Kept as a parameter (not hardcoded)
#   so a later widening of scope is a call-site change, not a rewrite.
#
# Env overrides (mainly for tests):
#   CHECK_SHARED_LIB_STALENESS_ROOT   local base dir for subject-dir file
#                                     lookups. Defaults to this repo's root.
#                                     Lets a test point local-file checks at
#                                     a scratch checkout without touching
#                                     the real tree.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$LIB_ROOT/scripts/lib/repo-resolve.sh"

CHECK_ROOT="${CHECK_SHARED_LIB_STALENESS_ROOT:-$LIB_ROOT}"

SUBJECT_DIR="${1:-scripts/lib}"
SUBJECT_DIR="${SUBJECT_DIR%/}"

_def_names() {
  # _def_names <file> — shell function names (`name()`) and Python top-level
  # def names, one per line, sorted+unique. A set-difference over this is
  # enough to tell an operator whether staleness matters (item 3) — this is
  # deliberately not a semantic diff.
  local f="$1"
  {
    grep -oE '^[A-Za-z_][A-Za-z0-9_]*\(\)' "$f" 2>/dev/null | sed 's/()$//'
    grep -oE '^def [A-Za-z_][A-Za-z0-9_]*' "$f" 2>/dev/null | sed 's/^def //'
  } | sort -u
}

main() {
  local code_repo rc
  code_repo="$(_require_code_repo "check-shared-lib-staleness" 2>&1)"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "STALENESS: UNREACHABLE scope=${SUBJECT_DIR} reason=\"code plane unresolved: ${code_repo}\""
    exit 0
  fi

  local tree_json
  if ! tree_json="$(gh api "repos/${code_repo}/git/trees/main?recursive=true" 2>&1)"; then
    echo "STALENESS: UNREACHABLE scope=${SUBJECT_DIR} reason=\"could not reach code plane ${code_repo}\""
    exit 0
  fi

  local pairs
  pairs="$(printf '%s' "$tree_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
subject = sys.argv[1] + "/"
for item in data.get("tree", []):
    if item.get("type") == "blob" and item.get("path", "").startswith(subject):
        print(item["sha"] + "\t" + item["path"])
' "$SUBJECT_DIR")"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "STALENESS: UNREACHABLE scope=${SUBJECT_DIR} reason=\"malformed tree response from code plane ${code_repo}\""
    exit 0
  fi

  local identical=0 differs=0 absent=0 unreadable=0
  local sha rel_path local_path local_sha

  while IFS=$'\t' read -r sha rel_path; do
    [[ -z "$rel_path" ]] && continue
    local_path="$CHECK_ROOT/$rel_path"

    if [[ ! -f "$local_path" ]]; then
      absent=$((absent + 1))
      echo "ABSENT     ${rel_path}  (present on code plane, absent in this checkout)"
      continue
    fi

    local_sha="$(git hash-object "$local_path" 2>/dev/null)"
    if [[ -z "$local_sha" ]]; then
      unreadable=$((unreadable + 1))
      echo "UNREADABLE ${rel_path}  (could not hash local file)"
      continue
    fi

    if [[ "$local_sha" == "$sha" ]]; then
      identical=$((identical + 1))
      echo "IDENTICAL  ${rel_path}"
      continue
    fi

    differs=$((differs + 1))
    local blob_json cp_tmp only_on_cp
    if blob_json="$(gh api "repos/${code_repo}/git/blobs/${sha}" 2>/dev/null)"; then
      cp_tmp="$(mktemp)"
      printf '%s' "$blob_json" | python3 -c '
import json, sys, base64
try:
    data = json.load(sys.stdin)
    content = data.get("content", "")
    enc = data.get("encoding", "base64")
    if enc == "base64":
        sys.stdout.buffer.write(base64.b64decode(content))
    else:
        sys.stdout.write(content)
except Exception:
    pass
' > "$cp_tmp" 2>/dev/null
      only_on_cp="$(comm -23 <(_def_names "$cp_tmp") <(_def_names "$local_path") | tr '\n' ' ')"
      rm -f "$cp_tmp"
      only_on_cp="$(printf '%s' "$only_on_cp" | sed 's/[[:space:]]*$//')"
      if [[ -n "$only_on_cp" ]]; then
        echo "DIFFERS    ${rel_path}  — missing locally: ${only_on_cp}"
      else
        echo "DIFFERS    ${rel_path}  — content differs (no top-level def/function name difference detected)"
      fi
    else
      echo "DIFFERS    ${rel_path}  — content differs (could not read code-plane content to name the change)"
    fi
  done <<<"$pairs"

  echo ""
  echo "STALENESS SUMMARY host=$(hostname 2>/dev/null || echo unknown) scope=${SUBJECT_DIR} identical=${identical} differs=${differs} absent=${absent} unreadable=${unreadable}"
  exit 0
}

main
