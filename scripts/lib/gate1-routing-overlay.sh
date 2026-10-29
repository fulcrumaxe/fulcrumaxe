#!/usr/bin/env bash
# scripts/lib/gate1-routing-overlay.sh — PR-carried first-coverage overlay +
# stem-mapping predicate for Gate 1 routing (D#370 PR1).
#
# Sourced by scripts/run-pr-tests.sh (registration only). Every read this lib
# makes resolves under the <repo_root> argument — the MEASURED (PR-head) tree,
# never the operator checkout. That is what breaks the bootstrap deadlock:
# main's router code + the PR's tree content lets a PR carrying the first-ever
# test for a file satisfy Gate 1 within itself.
#
# FIRST-COVERAGE PROCESS (R6 — the steps for covering an unrouted file):
#   1. Add the suite file to the PR (scripts/test-<stem>.sh for a scripts/**
#      file, so the R2 naming arm claims it with no further wiring).
#   2. Add a .gate1-routing.json overlay entry ONLY if no naming arm claims
#      the file (see SCHEMA below).
#   3. The reviewer verifies the negative control — the suite MUST fail
#      without the fix — before approving. A suite that passes on the
#      un-fixed tree proves nothing and must not ride an overlay entry.
#   4. Follow-up coverage work removes the overlay entry once a naming arm
#      (or a committed suite the arm finds) covers the file on its own.
#
# SCHEMA (.gate1-routing.json at the measured tree's root):
#   {"version":1,"routes":{"<changed path>":{"suite":"bash <suite.sh>"}}}
# Validation is strict and malformed-never-permissive: unknown keys or wrong
# shapes ignore that entry (whole file for a bad version/routes) with a
# stderr line; the suite command must match ^bash \S+\.sh$ (no arbitrary
# commands, no absolute paths, no .. escapes); the suite file must exist in
# the measured tree. A PR can only cause MORE tests to run, never forge
# routing — the manifest is written by the operator's runner copy to a
# caller-private path, and gate1-receipt-check.sh needs no change.
#
# Exposes (all re-entrant; safe under `set -u`):
#   gate1_overlay_load <repo_root>        populate the overlay table (stderr on
#                                          malformed bits; no-op if absent)
#   gate1_overlay_lookup <changed path>   print the suite command, or nothing
#   gate1_stem_suites <changed path> <repo_root>
#                                          print R2 suite relpaths, one per
#                                          line (exact stem first), or nothing
#   gate1_registry_has_suite <label> <repo_root>
#                                          0 if any of the registry's suite
#                                          files exists in the measured tree

# Overlay table: changed path -> "bash <suite.sh>". Populated ONLY by
# gate1_overlay_load (bare call — never command-substituted, the table would
# be lost with the subshell). Callers look up via gate1_overlay_lookup.
declare -A _GATE1_OVERLAY_ROUTES=()
_GATE1_OVERLAY_LOADED=""

gate1_overlay_load() {
  local repo_root="$1"
  local overlay="$repo_root/.gate1-routing.json"
  _GATE1_OVERLAY_ROUTES=()
  _GATE1_OVERLAY_LOADED=1
  [ -f "$overlay" ] || return 0
  # Validator prints one "<path>\t<suite cmd>" line per HONORED entry on
  # stdout and every rejection reason on stderr. Exit 0 with partial output
  # means "some entries honored, the rest ignored"; exit 1 means the whole
  # file was unusable (stderr already carries why) — either way nothing
  # unvalidated reaches the table.
  local parsed
  if ! parsed="$(python3 - "$overlay" "$repo_root" <<'PYEOF'
import json
import os.path
import re
import sys
from pathlib import Path

overlay_path, repo_root = sys.argv[1:3]
repo = Path(repo_root)

try:
    with open(overlay_path) as f:
        data = json.load(f)
except Exception as e:
    print(f"gate1-overlay: ignoring {overlay_path}: not valid JSON ({e})", file=sys.stderr)
    sys.exit(1)

if not isinstance(data, dict):
    print(f"gate1-overlay: ignoring {overlay_path}: top level is not an object", file=sys.stderr)
    sys.exit(1)

for key in data:
    if key not in ("version", "routes"):
        print(f"gate1-overlay: ignoring unknown top-level key {key!r}", file=sys.stderr)

if data.get("version") != 1:
    print(f"gate1-overlay: ignoring {overlay_path}: version is {data.get('version')!r}, only version 1 is honored", file=sys.stderr)
    sys.exit(1)

routes = data.get("routes")
if not isinstance(routes, dict):
    print(f"gate1-overlay: ignoring {overlay_path}: 'routes' is not an object", file=sys.stderr)
    sys.exit(1)

for route_path, entry in routes.items():
    tag = f"route {route_path!r}"
    if not isinstance(route_path, str) or not route_path:
        print("gate1-overlay: ignoring a route with a non-string or empty path", file=sys.stderr)
        continue
    if not isinstance(entry, dict):
        print(f"gate1-overlay: ignoring {tag}: entry is not an object", file=sys.stderr)
        continue
    if sorted(entry.keys()) != ["suite"]:
        print(f"gate1-overlay: ignoring {tag}: entry keys must be exactly ['suite']", file=sys.stderr)
        continue
    suite = entry["suite"]
    if not isinstance(suite, str) or not re.fullmatch(r"bash \S+\.sh", suite):
        print(f"gate1-overlay: ignoring {tag}: suite must match ^bash \\S+\\.sh$ (got {suite!r})", file=sys.stderr)
        continue
    suite_rel = suite[len("bash "):]
    if os.path.isabs(suite_rel) or ".." in Path(suite_rel).parts:
        print(f"gate1-overlay: ignoring {tag}: suite path must be in-tree relative (got {suite_rel!r})", file=sys.stderr)
        continue
    if not (repo / suite_rel).is_file():
        print(f"gate1-overlay: ignoring {tag}: suite file {suite_rel!r} is absent from the measured tree", file=sys.stderr)
        continue
    print(f"{route_path}\t{suite}")
PYEOF
)"; then
    return 0
  fi
  local route_path route_cmd
  while IFS=$'\t' read -r route_path route_cmd; do
    [ -n "${route_path:-}" ] && [ -n "${route_cmd:-}" ] || continue
    _GATE1_OVERLAY_ROUTES["$route_path"]="$route_cmd"
  done <<< "$parsed"
  return 0
}

gate1_overlay_lookup() {
  local file="$1"
  [ "${_GATE1_OVERLAY_LOADED:-}" = "1" ] || return 1
  local cmd="${_GATE1_OVERLAY_ROUTES[$file]:-}"
  [ -n "$cmd" ] || return 1
  printf '%s' "$cmd"
}

# R2 stem mapping (D#370): scripts/<s>.sh, scripts/lib/<s>.sh, and the .py
# twins are claimed by scripts/test-<stem>.sh or scripts/test-<stem>-*.sh
# (stem = basename minus extension) when at least one exists in the measured
# tree. Prints matches (exact stem first, then variants sorted) or nothing.
# Out of scope (deeper nesting, scripts/ci/**, the test files themselves —
# R1 owns those) prints nothing.
gate1_stem_suites() {
  local file="$1" repo_root="$2"
  local dir base stem m
  dir="$(dirname "$file")"
  case "$dir" in scripts|scripts/lib) ;; *) return 0 ;; esac
  base="$(basename "$file")"
  case "$base" in test-*.sh) return 0 ;; esac
  case "$base" in *.sh|*.py) ;; *) return 0 ;; esac
  stem="${base%.*}"
  [ -n "$stem" ] || return 0
  if [ -f "$repo_root/scripts/test-$stem.sh" ]; then
    printf 'scripts/test-%s.sh\n' "$stem"
  fi
  for m in "$repo_root/scripts/test-$stem-"*.sh; do
    [ -f "$m" ] || continue
    printf 'scripts/%s\n' "$(basename "$m")"
  done | sort -u
  return 0
}

# R0 liveness (D#370): 0 when at least one of the registry's suite files
# exists in the measured tree. Must list exactly the files the run blocks in
# run-pr-tests.sh guard on — a label whose files are ALL absent routes null.
gate1_registry_has_suite() {
  local registry="$1" repo_root="$2"
  local s
  case "$registry" in
    orphan-triage)
      [ -f "$repo_root/tests/test_triage_orphan_diffs.sh" ] && return 0
      [ -f "$repo_root/tests/test_worktree_self_exclusion.sh" ] && return 0
      return 1 ;;
    post-merge-hook)
      # Single-guard registry: the run block in run-pr-tests.sh executes
      # only when test_post_merge_hook_pull.sh exists, so that one file is
      # the liveness signal — a sibling suite without it routes null.
      [ -f "$repo_root/tests/test_post_merge_hook_pull.sh" ] ;;
    pr-plane)
      for s in test_pr_plane test_pr_tree_provisioning test_merge_and_hook \
               test_post_merge_hook_browser_queue; do
        [ -f "$repo_root/tests/$s.sh" ] && return 0
      done
      return 1 ;;
    pr-amend-e2e)
      [ -f "$repo_root/tests/test_spawn_agent_pr_amend_e2e.sh" ] ;;
    *) return 1 ;;
  esac
}
