#!/usr/bin/env bash
# scripts/lib/gate1-receipt.sh — schema, path construction, and atomic write
# for a Gate 1 receipt (D#2566 PR-1).
#
# Sourced by scripts/gate1-invoke.sh. This file writes receipts; it never
# gates anything and it never reads its own output back for a decision — the
# checker that authorizes a merge off a receipt is PR-2, not this file.
#
# A receipt has exactly two top-level objects, `caller` and `head_reported`
# (plus a `schema` version int). Everything under `caller` is written by
# THIS file, at whatever uid is running gate1-invoke.sh (the caller) — never
# by the tree under test. Everything under `head_reported` is copied
# verbatim from the manifest scripts/run-pr-tests.sh already produces
# (`routing`, `tests_run`, `partial`, `measured_tree`) — that half can be
# influenced by head-authored code, which is exactly why the checker in
# PR-2 must never read it for authorization. This file does not enforce
# that boundary; it only builds the shape that makes the boundary
# structural rather than documentary. See D#2566's Spec for the full
# argument.
#
# Usage:
#   source scripts/lib/gate1-receipt.sh
#   gate1_receipt_validate_sha "$SHA"                        # 0/1, no output
#   gate1_receipt_dir  "$STATE_DIR" "$REPO"                  # prints dir
#   gate1_receipt_path "$STATE_DIR" "$REPO" "$PR" "$SHA"     # prints path
#   gate1_receipt_write "$STATE_DIR" "$REPO" "$PR" "$SHA" \
#     "$TREE_ROOT" "$RUNNER_COPY" "$CONTAINMENT_MODE" \
#     "$PROBES_JSON" "$VERDICT" "$ENV_JSON" "$MANIFEST_PATH"
#     # prints the written receipt's absolute path on stdout on success;
#     # prints one line to stderr and returns non-zero on failure.

set -uo pipefail

# gate1_receipt_validate_sha SHA — 0 if SHA is exactly 40 lowercase hex
# characters, 1 otherwise. CWE-22: this is the one check standing between a
# caller-controlled string and a path segment, so it is checked here AND at
# the call site in gate1-invoke.sh before anything else touches disk.
gate1_receipt_validate_sha() {
  [[ "${1:-}" =~ ^[0-9a-f]{40}$ ]]
}

# gate1_receipt_dir STATE_DIR REPO — the receipt directory for REPO
# ("owner/name") under STATE_DIR. "/" in REPO becomes "__" so the receipts
# for every repo land in one flat directory per repo, never nested through
# an accidental extra path level.
gate1_receipt_dir() {
  local state_dir="$1" repo="$2"
  printf '%s/gate1-receipts/%s' "$state_dir" "${repo//\//__}"
}

# gate1_receipt_path STATE_DIR REPO PR SHA — the full receipt file path.
# Does NOT validate SHA — callers must call gate1_receipt_validate_sha first;
# this function only formats a path from values already trusted by the
# caller.
gate1_receipt_path() {
  local state_dir="$1" repo="$2" pr="$3" sha="$4"
  printf '%s/%s-%s.json' "$(gate1_receipt_dir "$state_dir" "$repo")" "$pr" "$sha"
}

# gate1_receipt_write STATE_DIR REPO PR SHA TREE_ROOT RUNNER_COPY \
#                      CONTAINMENT_MODE PROBES_JSON VERDICT ENV_JSON \
#                      MANIFEST_PATH
#
# STATE_DIR         $AUTONOMOUS_TEAM_STATE_DIR (or its default)
# REPO              "owner/name" of the code-plane repo
# PR                PR number
# SHA               the PR's head sha — validated against
#                    ^[0-9a-f]{40}$ before it becomes any part of a path
# TREE_ROOT         absolute path to the tree the suites ran against
# RUNNER_COPY       absolute path to the run-pr-tests.sh copy that executed
# CONTAINMENT_MODE  "NONE (same-uid)" or "UID(<name>)"
# PROBES_JSON       JSON object, the four gate1-verify-containment.sh probe
#                    verdicts, e.g. {"gh-credential":"NOT-DENIED",...}
# VERDICT           CONTAINED | UNCONTAINED | INDETERMINATE
# ENV_JSON          JSON object: AUTONOMOUS_TEAM_REPO,
#                    AUTONOMOUS_TEAM_STATE_DIR, seed_files{...}
# MANIFEST_PATH     path to the JSON file scripts/run-pr-tests.sh's
#                    --manifest-out wrote — becomes head_reported verbatim
#
# Prints the written receipt's absolute path on stdout on success. On
# failure (bad sha, unreadable manifest, write error) prints one line to
# stderr and returns non-zero — writes nothing.
gate1_receipt_write() {
  if [ "$#" -ne 11 ]; then
    echo "gate1-receipt: gate1_receipt_write expects 11 arguments, got $#" >&2
    return 1
  fi
  local state_dir="$1" repo="$2" pr="$3" sha="$4" tree_root="$5" \
        runner_copy="$6" containment_mode="$7" probes_json="$8" \
        verdict="$9" env_json="${10}" manifest_path="${11}"

  if ! gate1_receipt_validate_sha "$sha"; then
    echo "gate1-receipt: refusing to write — pr_head_sha does not match ^[0-9a-f]{40}\$: $sha" >&2
    return 1
  fi

  if [ ! -f "$manifest_path" ]; then
    echo "gate1-receipt: manifest not found at $manifest_path — refusing to write a receipt with no head_reported content" >&2
    return 1
  fi

  local dir path tmp written_at
  dir="$(gate1_receipt_dir "$state_dir" "$repo")"
  path="$(gate1_receipt_path "$state_dir" "$repo" "$pr" "$sha")"
  written_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if ! mkdir -p "$dir" 2>/dev/null; then
    echo "gate1-receipt: could not create receipt directory $dir" >&2
    return 1
  fi
  chmod 0700 "$dir" 2>/dev/null || true

  tmp="$(mktemp "${dir}/.receipt.XXXXXX" 2>/dev/null)" || {
    echo "gate1-receipt: mktemp failed under $dir" >&2
    return 1
  }

  if ! python3 - "$tmp" "$pr" "$repo" "$sha" "$tree_root" "$runner_copy" \
           "$containment_mode" "$probes_json" "$verdict" "$env_json" \
           "$written_at" "$path" "$manifest_path" <<'PYEOF'
import json
import sys

(tmp, pr, repo, sha, tree_root, runner_copy, containment_mode,
 probes_json, verdict, env_json, written_at, path, manifest_path) = sys.argv[1:14]

with open(manifest_path) as f:
    manifest = json.load(f)

head_reported = {
    "routing": manifest.get("routing", []),
    "tests_run": manifest.get("tests_run", []),
    "partial": bool(manifest.get("partial", False)),
    "measured_tree": manifest.get("measured_tree", {}),
}

receipt = {
    "schema": 1,
    "caller": {
        "pr": int(pr),
        "repo": repo,
        "pr_head_sha": sha,
        "tree_root": tree_root,
        "gate1_runner_copy": runner_copy,
        "gate1_containment": containment_mode,
        "containment_probes": json.loads(probes_json),
        "containment_verdict": verdict,
        "env": json.loads(env_json),
        "written_at": written_at,
        "receipt_path": path,
    },
    "head_reported": head_reported,
}

with open(tmp, "w") as f:
    json.dump(receipt, f, indent=2, sort_keys=True)
    f.write("\n")
PYEOF
  then
    echo "gate1-receipt: failed to build receipt JSON (manifest=$manifest_path)" >&2
    rm -f "$tmp"
    return 1
  fi

  chmod 0600 "$tmp" 2>/dev/null || true
  if ! mv -f "$tmp" "$path"; then
    echo "gate1-receipt: mv failed writing $path" >&2
    rm -f "$tmp"
    return 1
  fi

  printf '%s\n' "$path"
}
