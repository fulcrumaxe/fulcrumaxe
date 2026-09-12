#!/usr/bin/env bash
# scripts/pr-instruction-path-notice.sh — D#2434: say so out loud, once, when
# a PR touches a path an agent may load as instructions rather than plain
# code. Detect-and-tell only: applies the `instruction-paths-touched` label
# and posts exactly one PR comment naming the matched paths. Changes nothing
# else about how the PR is handled — no spawn suppressed, no merge gate
# added (AC-6, AC-12).
#
# Usage:
#   bash scripts/pr-instruction-path-notice.sh <PR_NUMBER> [<cross_repository:true|false>]
#
# The optional second argument lets a caller that already resolved
# cross_repository for this PR earlier in the same loop iteration (e.g.
# pr-pickup-gate.sh, via pr_intake_gate.py's check-pr) pass it straight
# through, so instruction_paths.py's own check-pr does not have to make a
# second `gh api .../pulls/{pr}` call to re-derive the same fact. Omit it
# for a standalone run (e.g. Gate 2 verification) — instruction_paths.py
# resolves it itself in that case, at the cost of one extra call.
#
# Idempotent: a second run against a PR that already carries the notice
# applies no second label and posts no second comment. The idempotency
# check filters by comment AUTHOR (our own live `gh` identity), not just by
# marker text — a marker with no author filter lets the PR's own author
# pre-post it and permanently silence the detector on their own PR (D#2434
# review round 2, finding #2).
#
# Every path rendered into the comment goes through
# `instruction_paths.py render-matched-paths`, which fences and sanitizes
# it — a path string is attacker-controlled diff content, and the comment
# is authored by our own trusted bot account, so an unsanitized render would
# launder attacker-written bytes into the trusted half of pr_comment_trust.py's
# own partition, under our own signature (D#2434 review round 2, finding #1).
#
# Exit 0 on success, including "nothing to do" (no instruction-bearing path
# touched). Exit 1 when the code plane cannot be resolved, the underlying
# check-pr call fails, its output is not the expected JSON shape, or our own
# identity cannot be resolved — never falls back to whatever repo the git
# remote happens to point at (see repo-resolve.sh's _require_code_repo), and
# never treats unreadable/malformed output as "0 matches" (D#2434 review
# round 2: a detector that goes quiet on noise reports the same 0 as a clean
# PR). Exit 2 on a usage error. No `gh` call is made before the code plane
# is resolved (AC-10).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"

LABEL_NAME="instruction-paths-touched"
COMMENT_MARKER="<!-- instruction-paths-touched-notice:v1 -->"

_log() { echo "[pr-instruction-path-notice] $*" >&2; }

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $(basename "$0") <PR_NUMBER> [<cross_repository:true|false>]" >&2
  exit 2
fi

PR_NUM="$1"
if ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
  echo "error: '$PR_NUM' is not a PR number" >&2
  exit 2
fi

CROSS_REPO_ARGS=()
if [[ $# -eq 2 ]]; then
  case "$2" in
    true|false) CROSS_REPO_ARGS=(--cross-repository "$2") ;;
    *)
      echo "error: cross_repository must be 'true' or 'false', got '$2'" >&2
      exit 2
      ;;
  esac
fi

# The code plane, resolved and guarded — never a fallback to whatever the
# git remote happens to point at, and no `gh` call before this succeeds.
CODE_REPO="$(_require_code_repo "pr-instruction-path-notice")" || exit 1

# --- run check-pr, stderr and stdout kept separate -------------------------
# D#2434 review round 2, hardening note: a prior version merged stderr into
# stdout (`2>&1`) when capturing check-pr's JSON. Any stderr noise on an
# otherwise-successful run corrupted the "JSON" this script went on to
# parse, and every downstream parser below had a bare `except: print(0)` /
# `print('false')` fallback — so noise on stderr silently read as "0 paths
# touched", the exact shape of a fail-open detector. Both halves are fixed
# here: stderr is captured separately and only surfaced on failure, and the
# parse step below fails closed (exit 1) rather than defaulting.
_CHECK_ERR_FILE="$(mktemp)"
RESULT_JSON="$(python3 "$SCRIPT_DIR/lib/instruction_paths.py" check-pr "$PR_NUM" --repo "$CODE_REPO" "${CROSS_REPO_ARGS[@]}" 2>"$_CHECK_ERR_FILE")"
CHECK_RC=$?
CHECK_ERR="$(cat "$_CHECK_ERR_FILE" 2>/dev/null)"
rm -f "$_CHECK_ERR_FILE"

if [[ $CHECK_RC -ne 0 ]]; then
  echo "error: instruction_paths.py check-pr failed for PR #$PR_NUM (rc=$CHECK_RC): $CHECK_ERR" >&2
  exit 1
fi
if [[ -n "$CHECK_ERR" ]]; then
  _log "WARN: check-pr exited 0 but printed to stderr (not trusted as a match, but noted): $CHECK_ERR"
fi

# Parse once, fail closed on anything that isn't exactly the expected shape
# — never default to "0 matches" / "not cross-repository" on a parse error.
PARSED="$(printf '%s' "$RESULT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    paths = d.get('paths_touched')
    if not isinstance(paths, list):
        raise ValueError(\"'paths_touched' missing or not a list\")
    cross = bool(d.get('cross_repository'))
    print(len(paths))
    print('true' if cross else 'false')
except Exception as exc:
    print(str(exc)[:200], file=sys.stderr)
    sys.exit(1)
")"
PARSE_RC=$?
if [[ $PARSE_RC -ne 0 ]]; then
  echo "error: check-pr output for PR #$PR_NUM was not the expected JSON shape ($PARSED): $RESULT_JSON" >&2
  exit 1
fi
PATHS_TOUCHED_COUNT="$(sed -n '1p' <<<"$PARSED")"
CROSS_REPOSITORY="$(sed -n '2p' <<<"$PARSED")"

# --- AC-7: the expiry tripwire — a signal only, never a gate --------------
if [[ "$CROSS_REPOSITORY" == "true" ]]; then
  _log "EXPIRY-TRIPWIRE: first cross-repository PR on the code plane (PR #$PR_NUM, see D#2434)"
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
    "EXPIRY-TRIPWIRE: first cross-repository PR on the code plane — PR #$PR_NUM (see D#2434)" \
    >/dev/null 2>&1 \
    || _log "WARN: rotate-team-log.sh comment failed for the tripwire line (non-fatal)"
fi

if [[ "$PATHS_TOUCHED_COUNT" -eq 0 ]] 2>/dev/null; then
  _log "PR #$PR_NUM touches no instruction-bearing path — no label, no comment"
  exit 0
fi

# --- our own identity, for the author-filtered idempotency check ----------
# D#2434 review round 2, finding #2: the marker text alone is not enough —
# any PR author can pre-post the exact marker string on their own PR, and
# the old check ("does any comment contain this text?") would then treat
# the notice as already-posted forever, permanently silencing the detector
# on that PR with no label, no comment, and (before this fix) stderr routed
# to /dev/null. Filtering by the live authenticated `gh` identity closes
# this: only a comment WE posted can satisfy the idempotency check.
BOT_LOGIN="$(gh api user --jq '.login' 2>/dev/null)"
if [[ -z "$BOT_LOGIN" ]]; then
  echo "error: could not resolve the authenticated gh identity — refusing to check idempotency or post blind" >&2
  exit 1
fi

# --- AC-6 idempotency: has WE already posted the notice? -------------------
EXISTING_MARKER_COUNT="$(gh pr view "$PR_NUM" --repo "$CODE_REPO" --json comments 2>/dev/null \
  | jq --arg m "$COMMENT_MARKER" --arg author "$BOT_LOGIN" \
       '[.comments[] | select((.body // "" | contains($m)) and ((.author.login // "") == $author))] | length' 2>/dev/null)"
EXISTING_MARKER_COUNT="${EXISTING_MARKER_COUNT:-0}"

if [[ "$EXISTING_MARKER_COUNT" -gt 0 ]] 2>/dev/null; then
  _log "PR #$PR_NUM already carries our own instruction-paths-touched notice — no second comment (idempotent no-op)"
  exit 0
fi

# --- render the matched paths safely, then post ----------------------------
MATCHED_PATHS_BLOCK="$(printf '%s' "$RESULT_JSON" \
  | python3 -c "import json, sys; print(json.dumps(json.load(sys.stdin).get('paths_touched') or []))" \
  | python3 "$SCRIPT_DIR/lib/instruction_paths.py" render-matched-paths)"
RENDER_RC=$?
if [[ $RENDER_RC -ne 0 || -z "$MATCHED_PATHS_BLOCK" ]]; then
  echo "error: could not render matched paths for PR #$PR_NUM (rc=$RENDER_RC)" >&2
  exit 1
fi

COMMENT_BODY="$(cat <<EOF
This PR touches one or more paths that an agent may load as instructions rather than plain code:

${MATCHED_PATHS_BLOCK}

Review of these paths is human-gated. On a PR carrying this label, applying \`intake-approved\` means a human has actually read the contents of the paths listed above — not just that the contributor is otherwise welcome to submit changes.

${COMMENT_MARKER}
EOF
)"

gh label create "$LABEL_NAME" \
  --color "5319E7" \
  --description "Diff touches a path an agent may read as instructions (CLAUDE.md, .claude/, hooks/, etc.) — advisory only, changes nothing about spawning or merging" \
  --repo "$CODE_REPO" \
  --force \
  >/dev/null 2>&1 \
  || _log "WARN: gh label create for '$LABEL_NAME' failed (non-fatal — it may already exist correctly)"

gh api -X POST "repos/${CODE_REPO}/issues/${PR_NUM}/labels" -f "labels[]=${LABEL_NAME}" >/dev/null 2>&1
LABEL_RC=$?
[[ $LABEL_RC -ne 0 ]] && _log "WARN: failed to apply label '$LABEL_NAME' to PR #$PR_NUM (rc=$LABEL_RC)"

gh pr comment "$PR_NUM" --repo "$CODE_REPO" --body "$COMMENT_BODY" >/dev/null 2>&1
COMMENT_RC=$?
[[ $COMMENT_RC -ne 0 ]] && _log "WARN: failed to post instruction-paths-touched comment on PR #$PR_NUM (rc=$COMMENT_RC)"

if [[ $LABEL_RC -eq 0 && $COMMENT_RC -eq 0 ]]; then
  _log "PR #$PR_NUM: applied label '$LABEL_NAME' and posted the notice comment ($PATHS_TOUCHED_COUNT path(s))"
fi

exit 0
