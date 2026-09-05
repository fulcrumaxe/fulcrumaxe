#!/usr/bin/env bash
# scripts/ci/pr-link-policy.sh — enforce how a PR body is allowed to cite the
# Discussion it came from (D#2348 PR-h item 1).
#
# TWO RULES, ONE REASON
#
#   1. The body must carry a machine-readable closing reference in the
#      `D#NNNN` form.
#   2. The body must not contain a `github.com/<private-owner>` substring.
#
# Both come out of the same decision: PRs become public, Discussions stay
# private. A public PR body that links a private Discussion by URL publishes
# a 404 that also leaks shape — that a private twin exists, roughly how much
# work is in it, and how it is numbered. A bare `Closes D#2348` is honest
# about provenance and publishes neither a dead link nor a hostname.
#
# WHY PRE-MERGE IS ENOUGH HERE, AND ONLY HERE
#
# A PR body is not a published artifact: it can be edited afterwards with no
# residue. A commit cannot, which is why the identifier scan that guards
# commit content is a separate, pre-push mechanism (D#2348 PR-g) rather than
# another job in this workflow.
#
# THE OWNER NAME IS NOT WRITTEN DOWN IN THIS FILE
#
# It is read at run time from IDENTIFIER-RULES.txt's OLD_OWNER, the same way
# and from the same key as scripts/ci/repo-target-gate.sh. Three reasons, in
# order of how much they matter:
#
#   1. This file ships. A gate that hunts a private owner name by spelling it
#      out lands that name in the published tree — the gate becoming the leak
#      it was written to prevent. D#2348 PR-i hit this exact shape.
#   2. The export's rewrite pass rewrites OLD_OWNER to the public owner in
#      every text file it touches. A hard-coded literal here would come out
#      of an export inverted: hunting the PUBLIC owner, so it would block
#      every legitimate link and pass every private one, silently.
#   3. One source for the name means the gate and the rewrite table cannot
#      disagree about who "we" are.
#
# Two candidate locations are searched, because D#2348 PR-i moves that file
# from open-source/ to scripts/ci/ and this reader has to survive the move.
# If neither resolves, PRIVATE_REPO_OWNER can supply the name directly. If
# nothing supplies it, this FAILS — it does not skip. A gate that cannot name
# what it is hunting must not report a pass; that is the specific defect
# (SKIP-on-missing-input) this cutover has already shipped three times.
#
# INPUT — $PR_BODY_FILE IN CI, $PR_BODY ONLY FOR LOCAL USE
#
# In CI the body arrives as a FILE whose path is named by $PR_BODY_FILE, and
# the workflow fills that file from $GITHUB_EVENT_PATH. It is deliberately
# not passed through the step's `env:` block, which is how this first shipped
# and was wrong in the worst way available: the runner prints a step's env
# block into the log before the step runs, so a body containing the private
# slug got published verbatim into a public Actions log — permanently, and
# only on the bodies this gate exists to catch. This script's redaction of
# its own output was correct and arrived 36ms too late to matter. The fix is
# to keep the body out of anything the runner echoes, not to redact harder.
#
# It is also never interpolated into a `run:` line: on a fork PR the body is
# attacker-controlled text and splicing it into a shell command is an
# injection hole.
#
# $PR_BODY is still honoured when $PR_BODY_FILE is unset, for local runs and
# for the test suite. Neither set is a wiring error (exit 2). An EMPTY body is
# a real PR with an empty body and fails rule 1 like any other body with no
# closing reference.
#
# Usage:
#   PR_BODY_FILE=/path/to/body.txt bash scripts/ci/pr-link-policy.sh
#   PR_BODY="$(gh pr view 123 --json body -q .body)" bash scripts/ci/pr-link-policy.sh
#
# Exit 0 = both rules satisfied.
# Exit 1 = a rule is violated, the owner name could not be resolved, or the
#          self-test failed.
# Exit 2 = neither input is set, the named file is unreadable, or too many
#          arguments.

set -uo pipefail

if [[ $# -gt 0 ]]; then
  echo "usage: PR_BODY=<text> $(basename "$0")" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ---------------------------------------------------------------------------
# Resolve the private owner name.
# ---------------------------------------------------------------------------
OWNER="${PRIVATE_REPO_OWNER:-}"
RULES_SOURCE="\$PRIVATE_REPO_OWNER"
if [[ -z "$OWNER" ]]; then
  for candidate in \
    "$REPO_ROOT/scripts/ci/IDENTIFIER-RULES.txt" \
    "$REPO_ROOT/open-source/IDENTIFIER-RULES.txt"; do
    if [[ -f "$candidate" ]]; then
      OWNER="$(sed -n 's/^[[:space:]]*OLD_OWNER=\(.*\)$/\1/p' "$candidate" | head -1)"
      OWNER="${OWNER%"${OWNER##*[![:space:]]}"}"
      RULES_SOURCE="$candidate"
      [[ -n "$OWNER" ]] && break
    fi
  done
fi

if [[ -z "$OWNER" ]]; then
  echo "FAIL: could not resolve the private owner name." >&2
  echo "      Looked for OLD_OWNER in scripts/ci/IDENTIFIER-RULES.txt and" >&2
  echo "      open-source/IDENTIFIER-RULES.txt, and at \$PRIVATE_REPO_OWNER." >&2
  echo "      Refusing to report a pass on a rule this gate cannot evaluate." >&2
  exit 1
fi

FORBIDDEN_HOST_PREFIX="github.com/$OWNER"

# Rule 1's pattern. Deliberately the same three verbs, with the same
# case-insensitive first letter, that scripts/lib/resolve-pr-discussion.sh
# already matches — two mechanisms reading the same PR body for the same
# reference must agree on what counts, or one of them is silently wrong on
# some real PR. Narrowed to the `D#` form: that resolver also accepts a bare
# `#N`, which is an Issue reference and is not what this rule is about.
CLOSES_RE='([Cc]loses|[Rr]esolves|[Ff]ixes) D#[0-9]+'

has_closes_ref() { printf '%s' "$1" | grep -Eq "$CLOSES_RE"; }
has_private_url() { printf '%s' "$1" | grep -Fqi "$FORBIDDEN_HOST_PREFIX"; }

# ---------------------------------------------------------------------------
# Self-test, before the real body, on every run.
#
# The failure mode this rules out is the one this cutover keeps producing: a
# check that passes because it stopped looking. Both rules are asserted in
# both directions against synthetic bodies built from the resolved owner
# name, so the fixtures cannot contain that name as a literal either.
# ---------------------------------------------------------------------------
self_test() {
  local bad=0

  # Rule 1 must accept each accepted verb, and reject a body with no
  # reference and a body whose only reference is a bare Issue `#N`.
  local good
  for good in "Closes D#2348" "resolves D#7 in the body" "Fixes D#1"; do
    has_closes_ref "$good" || { echo "SELF-TEST FAIL: closing-reference rule rejected '$good'" >&2; bad=1; }
  done
  local bad_body
  for bad_body in "" "No reference at all." "Closes #2348" "Closes D#" "closes d#2348"; do
    has_closes_ref "$bad_body" && { echo "SELF-TEST FAIL: closing-reference rule accepted '$bad_body'" >&2; bad=1; }
  done

  # Rule 2 must catch the private host prefix, in either case, and must not
  # fire on a body that merely names the Discussion without a URL.
  local leak="see https://$FORBIDDEN_HOST_PREFIX/repo/discussions/2348"
  has_private_url "$leak" || { echo "SELF-TEST FAIL: private-URL rule missed a private Discussion URL" >&2; bad=1; }
  has_private_url "SEE HTTPS://${FORBIDDEN_HOST_PREFIX^^}/REPO" || { echo "SELF-TEST FAIL: private-URL rule is case-sensitive" >&2; bad=1; }
  has_private_url "Closes D#2348" && { echo "SELF-TEST FAIL: private-URL rule fired on a bare D# reference" >&2; bad=1; }
  has_private_url "see https://github.com/some-other-org/thing" && { echo "SELF-TEST FAIL: private-URL rule fired on an unrelated GitHub URL" >&2; bad=1; }

  if [[ $bad -ne 0 ]]; then
    echo "FAIL: pr-link-policy self-test failed — the matchers no longer discriminate, so their verdict on the real body means nothing" >&2
    return 1
  fi
  echo "self-test: both rules assert in both directions, all as expected"
  return 0
}

self_test || exit 1

# ---------------------------------------------------------------------------
# The real body.
# ---------------------------------------------------------------------------
BODY_SOURCE=""
if [[ -n "${PR_BODY_FILE:-}" ]]; then
  if [[ ! -r "$PR_BODY_FILE" ]]; then
    echo "FAIL: PR_BODY_FILE is set to '$PR_BODY_FILE' but that file is not readable." >&2
    exit 2
  fi
  PR_BODY="$(cat "$PR_BODY_FILE")"
  BODY_SOURCE="\$PR_BODY_FILE"
elif [[ -n "${PR_BODY+set}" ]]; then
  BODY_SOURCE="\$PR_BODY"
else
  echo "FAIL: neither PR_BODY_FILE nor PR_BODY is set." >&2
  echo "      In CI, write the body to a file from \$GITHUB_EVENT_PATH and name it" >&2
  echo "      in PR_BODY_FILE. Do NOT put the body in the step's \`env:\` block —" >&2
  echo "      the runner prints that block into the log before the step runs." >&2
  exit 2
fi

# Deliberately reports the LENGTH, never the content. This script must not be
# the thing that puts a violating body into a log.
echo "pr-link-policy: owner resolved from $RULES_SOURCE, body from $BODY_SOURCE, ${#PR_BODY} chars"

VIOLATIONS=0

if ! has_closes_ref "$PR_BODY"; then
  echo "FAIL: the PR body carries no closing reference in the D#NNNN form."
  echo "      Add a line reading: Closes D#<number>"
  echo "      (Resolves/Fixes are accepted too. A bare 'Closes #N' is an Issue"
  echo "      reference and does not satisfy this rule — a PR that closes an"
  echo "      Issue needs both lines.)"
  VIOLATIONS=$((VIOLATIONS + 1))
fi

if has_private_url "$PR_BODY"; then
  echo "FAIL: the PR body contains a URL pointing at the private engine repo."
  echo "      Once PRs are public this publishes a 404 that also leaks the"
  echo "      private repository's existence, size and numbering. Cite the"
  echo "      Discussion as a bare 'Closes D#<number>' instead — no URL."
  echo "      Offending line(s), with the owner name redacted:"
  printf '%s' "$PR_BODY" | grep -Fin "$FORBIDDEN_HOST_PREFIX" \
    | sed "s|$OWNER|<private-owner>|g" | sed 's/^/        /'
  VIOLATIONS=$((VIOLATIONS + 1))
fi

if [[ $VIOLATIONS -gt 0 ]]; then
  echo
  echo "FAIL: $VIOLATIONS link-policy rule(s) violated. Edit the PR body and"
  echo "      re-run this check — a PR body can be corrected with no residue,"
  echo "      which is why this rule is enforced here rather than on commits."
  exit 1
fi

echo "PASS: the PR body carries a bare D# closing reference and no private-repo URL."
exit 0
