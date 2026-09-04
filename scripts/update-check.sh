#!/usr/bin/env bash
# scripts/update-check.sh — detection half of /update (D#2335 Spec, PR 1).
#
# Ships into every adopter repo automatically: this file is a whole
# scripts/ BOOTSTRAP_PATHS entry, so no manifest edit is needed for it to
# reach a bootstrapped project (see open-source/MANIFEST.md's
# BOOTSTRAP_PATHS block and the Spec comment on D#2335 for why).
#
# Reports one of exactly three states, each with its own message and exit
# code, plus a fourth for usage errors:
#
#   0   up to date        — recorded baseline == upstream HEAD
#   10  update available  — baseline is behind upstream (N commits, printed)
#   20  cannot determine  — no real two-SHA comparison could be completed
#   2   usage error       — bad arguments (message on stderr)
#
# The hard rule this script exists to enforce: it must NEVER print "up to
# date" unless it actually compared two known commits. There is no
# default-to-0 branch anywhere below — every path that does not end in a
# successful comparison exits 20. If you're tempted to add `|| echo 0`
# anywhere in here, that is the bug this script exists to not have.
#
# Constraint: at most one authenticated read-only `gh api` GET per run.
# GitHub's compare endpoint (repos/{repo}/compare/{base}...{head}) gives us
# both pieces of information a two-call design (commits/main for the SHA,
# then compare for the count) would need in two round trips — ahead_by=0
# IS "up to date", ahead_by>0 IS "update available (N commits)", and a 404
# on the compare itself IS "baseline not in upstream" — so one call covers
# all three real-network verdicts. This is a deliberate simplification of
# the two-snippet sketch in the Spec's Implementation Notes (which showed
# commits/main and compare as separate calls); it satisfies the Spec's own
# harder constraint ("at most one gh api GET") instead of literally
# reproducing the sketch. See the PR description for the reasoning.
#
# UPDATE_CHECK_UPSTREAM_CMD overrides the compare call for hermetic tests
# (scripts/ci/update-check-guard.py) and is invoked via `bash -c`, with the
# baseline and source repo available as UPDATE_CHECK_BASELINE_SHA and
# UPDATE_CHECK_SOURCE_REPO. Its contract:
#   - exit 0, stdout is a bare non-negative integer -> that many commits
#     ahead (0 == up to date)
#   - exit 0, stdout is the literal string "NOT_FOUND" -> baseline not
#     found upstream (the 404 case)
#   - any non-zero exit -> upstream unreachable
# Any other stdout on exit 0 is treated as a malformed response, which
# fails closed to upstream_unreachable rather than guessing.
#
# Exit code choice (0/10/20/2, not 0/1/2): 1 is what a crashed script, an
# unset variable under `set -u`, or a no-match `grep` hands you by
# accident. Reserving it means a crash can never be silently read as a
# verdict. 2 for usage matches scripts/engine-sync/drift-check.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP_FILE="$REPO_ROOT/.autonomous-team/engine-install.json"
DEFAULT_ENGINE_REPO="${LOOP_BOOTSTRAP_ENGINE_REPO:-fulcrumaxe/fulcrumaxe}"

usage() {
  cat <<'EOF'
Usage: scripts/update-check.sh
       scripts/update-check.sh --record-baseline <40-hex-sha>
       scripts/update-check.sh --help

Checks whether this fulcrumaxe install is behind its upstream engine repo,
using the baseline commit recorded by bootstrap in
.autonomous-team/engine-install.json.

Exit codes:
  0   up to date        — recorded baseline matches upstream HEAD
  10  update available  — baseline is behind upstream (commit count printed)
  20  cannot determine  — no real comparison could be completed (see the
                           reason=... token in the message)
  2   usage error       — bad arguments; message printed on stderr

Options:
  --record-baseline <sha>   Write (or refresh) engine-install.json's
                             engine_commit field to <sha>, a 40-character
                             hex commit SHA. Exits 2 without writing if
                             <sha> is not exactly 40 hex characters.
  --help, -h                Print this message and exit 0.
EOF
}

die_usage() {
  echo "error: $1" >&2
  usage >&2
  exit 2
}

RECORD_BASELINE=""
DO_RECORD=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --record-baseline)
      [[ $# -ge 2 ]] || die_usage "--record-baseline requires a value"
      RECORD_BASELINE="$2"
      DO_RECORD=true
      shift 2
      ;;
    *)
      die_usage "unknown argument: $1"
      ;;
  esac
done

if [[ "$DO_RECORD" == "true" ]]; then
  if [[ ! "$RECORD_BASELINE" =~ ^[0-9a-fA-F]{40}$ ]]; then
    die_usage "--record-baseline expects a 40-character hex commit SHA, got: $RECORD_BASELINE"
  fi

  mkdir -p "$REPO_ROOT/.autonomous-team"
  ENGINE_VERSION_VAL="null"
  if [[ -f "$REPO_ROOT/engine/VERSION" ]]; then
    ENGINE_VERSION_VAL="$(tr -d '[:space:]' < "$REPO_ROOT/engine/VERSION")"
  fi
  python3 - "$STAMP_FILE" "$RECORD_BASELINE" "$DEFAULT_ENGINE_REPO" "$ENGINE_VERSION_VAL" <<'RECORD_PY'
import json
import sys
from datetime import datetime, timezone

dst, sha, default_repo, engine_version = sys.argv[1:5]
sha = sha.lower()

existing = {}
try:
    with open(dst) as f:
        existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
except (FileNotFoundError, json.JSONDecodeError):
    existing = {}

existing["engine_commit"] = sha
existing.setdefault("engine_version", None if engine_version == "null" else engine_version)
existing.setdefault("source", "manual-record")
existing.setdefault("source_repo", default_repo)
existing.setdefault("bootstrapped_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

with open(dst, "w") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")
RECORD_PY
  echo "recorded baseline $RECORD_BASELINE in $STAMP_FILE"
  exit 0
fi

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

if [[ ! -f "$STAMP_FILE" ]]; then
  echo "cannot determine: no baseline recorded (reason=no_baseline_recorded) — run 'bash scripts/update-check.sh --record-baseline <sha>', or re-bootstrap to record one" >&2
  exit 20
fi

BASELINE=""
SOURCE_REPO=""
STAMP_READ_ERR=""
STAMP_READ_OUT="$(python3 - "$STAMP_FILE" "$DEFAULT_ENGINE_REPO" <<'READ_PY'
import json
import sys

path, default_repo = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except Exception as exc:  # noqa: BLE001 - report and let caller treat as no-baseline
    print(f"ERR:{exc}")
    sys.exit(0)

commit = data.get("engine_commit") if isinstance(data, dict) else None
repo = data.get("source_repo") if isinstance(data, dict) else None
if not commit:
    print("ERR:no engine_commit recorded")
    sys.exit(0)
print(f"OK:{commit}:{repo or default_repo}")
READ_PY
)"

if [[ "$STAMP_READ_OUT" == ERR:* ]]; then
  echo "cannot determine: no baseline recorded (reason=no_baseline_recorded) — ${STAMP_READ_OUT#ERR:}; run 'bash scripts/update-check.sh --record-baseline <sha>' to fix" >&2
  exit 20
fi

BASELINE="$(cut -d: -f2 <<<"$STAMP_READ_OUT")"
SOURCE_REPO="$(cut -d: -f3- <<<"$STAMP_READ_OUT")"

# Resolve "commits ahead" (and implicitly, upstream reachability) via a
# single call — real gh api compare, or the injected hermetic-test command.
COMPARE_TMP_ERR="$(mktemp)"
trap 'rm -f "$COMPARE_TMP_ERR"' EXIT

set +e
if [[ -n "${UPDATE_CHECK_UPSTREAM_CMD:-}" ]]; then
  COMPARE_OUT="$(UPDATE_CHECK_BASELINE_SHA="$BASELINE" UPDATE_CHECK_SOURCE_REPO="$SOURCE_REPO" bash -c "$UPDATE_CHECK_UPSTREAM_CMD" 2>"$COMPARE_TMP_ERR")"
  COMPARE_RC=$?
else
  if ! command -v gh >/dev/null 2>&1; then
    COMPARE_OUT=""
    COMPARE_RC=127
    echo "gh CLI not found on PATH" > "$COMPARE_TMP_ERR"
  else
    COMPARE_OUT="$(gh api "repos/${SOURCE_REPO}/compare/${BASELINE}...main" --jq '.ahead_by' 2>"$COMPARE_TMP_ERR")"
    COMPARE_RC=$?
  fi
fi
set -e

COMPARE_ERR="$(cat "$COMPARE_TMP_ERR" 2>/dev/null || true)"

if [[ $COMPARE_RC -ne 0 ]]; then
  # Match gh's actual 404 error shape ("gh: Not Found (HTTP 404)") narrowly
  # — a broad "not found" match would also catch "gh CLI not found on
  # PATH" below and misreport a missing CLI as baseline_not_in_upstream.
  if [[ "$COMPARE_OUT" == "NOT_FOUND" ]] || grep -q 'HTTP 404' <<<"$COMPARE_ERR"; then
    echo "cannot determine: recorded baseline not found upstream (reason=baseline_not_in_upstream) — history may have been rewritten (force-push, squash, or wrong repo); re-run with --record-baseline <new-sha>" >&2
    exit 20
  fi
  echo "cannot determine: upstream unreachable (reason=upstream_unreachable) — check 'gh auth status' and network connectivity: ${COMPARE_ERR:-no gh output}" >&2
  exit 20
fi

if [[ "$COMPARE_OUT" == "NOT_FOUND" ]]; then
  echo "cannot determine: recorded baseline not found upstream (reason=baseline_not_in_upstream) — history may have been rewritten (force-push, squash, or wrong repo); re-run with --record-baseline <new-sha>" >&2
  exit 20
fi

if ! [[ "$COMPARE_OUT" =~ ^[0-9]+$ ]]; then
  echo "cannot determine: upstream unreachable (reason=upstream_unreachable) — unexpected response comparing against ${SOURCE_REPO}: '${COMPARE_OUT}'" >&2
  exit 20
fi

if [[ "$COMPARE_OUT" -eq 0 ]]; then
  echo "up to date (baseline ${BASELINE} matches ${SOURCE_REPO}@main)"
  exit 0
fi

echo "update available (${COMPARE_OUT} commits behind ${SOURCE_REPO}@main)"
exit 10
