#!/usr/bin/env bash
# drift-check.sh — Slice A of D#1528 (cross-project update-distribution channel).
#
# READ-ONLY. Compares two engine/manifest.json files (a local one and an
# "upstream" one) and reports drift: engine_version delta and the list of
# drifted file PATH NAMES only. It never prints file contents, never diffs
# file bodies, never writes any file, never spawns an agent, and never pushes
# to a remote or opens/merges a pull request.
#
# In Slice A both --local and --upstream point at local files on disk (e.g.
# two manifests you already have). Slice B (a separate, future Spec) will add
# an authenticated *read-only* `gh api` fetch of the upstream manifest --
# this script does not do that today.
#
# Usage:
#   drift-check.sh --local <manifest.json> --upstream <manifest.json>
#   drift-check.sh --help
#
# Exit codes:
#   0  in-sync            — engine_version and every file hash match
#   1  drift               — engine_version differs and/or one or more file
#                             hashes differ
#   2  usage/input error   — bad args, missing/unreadable/invalid manifest

set -euo pipefail

usage() {
  cat <<'EOF'
drift-check.sh — read-only comparison of two engine manifest.json files.

This tool is READ-ONLY: it never writes any file, never spawns an agent,
never pushes to a remote, and never opens or merges a pull request. It only
reads the two manifest files you pass it and prints a redacted report (path
names and an engine_version delta only -- never file contents).

Usage:
  drift-check.sh --local <manifest.json> --upstream <manifest.json>
  drift-check.sh --help

Exit codes:
  0  in-sync           -- engine_version and every file hash match
  1  drift              -- version and/or one or more file hashes differ
  2  usage/input error  -- bad args or unreadable/invalid manifest json
EOF
}

LOCAL_PATH=""
UPSTREAM_PATH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --local)
      LOCAL_PATH="${2:-}"
      shift 2 || { echo "error: --local requires a value" >&2; exit 2; }
      ;;
    --upstream)
      UPSTREAM_PATH="${2:-}"
      shift 2 || { echo "error: --upstream requires a value" >&2; exit 2; }
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$LOCAL_PATH" ] || [ -z "$UPSTREAM_PATH" ]; then
  echo "error: --local and --upstream are both required" >&2
  usage >&2
  exit 2
fi

if [ ! -f "$LOCAL_PATH" ]; then
  echo "error: local manifest not found: $LOCAL_PATH" >&2
  exit 2
fi
if [ ! -f "$UPSTREAM_PATH" ]; then
  echo "error: upstream manifest not found: $UPSTREAM_PATH" >&2
  exit 2
fi

# Delegate the actual comparison to a small inline python3 (stdlib json only)
# so the shell script stays a thin, auditable CLI wrapper. This does NOT
# execute any upstream-provided code -- both inputs are treated purely as
# data (json.load), never as instructions or shell.
python3 - "$LOCAL_PATH" "$UPSTREAM_PATH" <<'PYEOF'
import json
import sys

local_path, upstream_path = sys.argv[1], sys.argv[2]

try:
    with open(local_path) as f:
        local = json.load(f)
except (json.JSONDecodeError, OSError) as exc:
    print(f"error: could not parse local manifest {local_path}: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    with open(upstream_path) as f:
        upstream = json.load(f)
except (json.JSONDecodeError, OSError) as exc:
    print(f"error: could not parse upstream manifest {upstream_path}: {exc}", file=sys.stderr)
    sys.exit(2)

local_version = local.get("engine_version", "?")
upstream_version = upstream.get("engine_version", "?")

local_files = local.get("files", {})
upstream_files = upstream.get("files", {})

drifted = sorted(
    p for p in set(local_files) | set(upstream_files)
    if local_files.get(p) != upstream_files.get(p)
)

if local_version == upstream_version and not drifted:
    print("in-sync")
    print(f"engine_version: {local_version}")
    sys.exit(0)

print("drift detected")
print(f"engine_version: local={local_version} upstream={upstream_version}")
if drifted:
    print(f"drifted files ({len(drifted)}):")
    for p in drifted:
        in_local = p in local_files
        in_upstream = p in upstream_files
        if in_local and in_upstream:
            status = "changed"
        elif in_local:
            status = "local-only"
        else:
            status = "upstream-only"
        print(f"  {status}: {p}")
sys.exit(1)
PYEOF
