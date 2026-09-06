#!/usr/bin/env bash
# scripts/ci/pr-link-policy.sh — enforce how a PR body is allowed to cite the
# Discussion it came from (D#2348 PR-h item 1).
#
# TWO RULES, ONE REASON
#
#   1. The body must carry a machine-readable closing reference in the
#      `D#NNNN` form.
#   2. The body must not contain a `github.com/` URL naming any owner other
#      than the code plane's own.
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
# RULE 2 IS AN ALLOWLIST, NOT A DENYLIST — AND NEEDS NO PRIVATE NAME
#
# This gate runs on the public code plane, which never contains a private
# owner name to hunt (the old denylist form read IDENTIFIER-RULES.txt, which
# open-source/export.sh deliberately excludes from the export — so the gate
# needed a name that could never exist in the tree it ran in, and failed
# closed on every PR the public repo has ever had). Rule 2 instead compares
# against the code plane's OWN owner: a URL naming any other owner is
# rejected. The public owner is, by definition, safe to write down and to
# resolve at run time — no secret, no export-excluded file.
#
# This is also strictly stronger than the old rule: it catches a link into
# *any* foreign repo, not just the one owner it happened to be told about,
# and it keeps working across a rename.
#
# OWNER RESOLUTION, FIRST HIT WINS
#
#   1. $PR_LINK_POLICY_CODE_OWNER — an explicit override, for the test suite
#      and for local runs that want to pin the answer.
#   2. $GITHUB_REPOSITORY — "owner/name" of the *base* repo on a pull_request
#      event. This is the source that makes the gate decidable on a fork PR:
#      it is a plain environment variable, not an Actions secret, so GitHub
#      populates it on workflow runs triggered by a fork's pull_request event
#      (secrets are withheld there by design). A secret-backed owner would
#      leave the gate undecidable on exactly the external-contributor PRs the
#      public code plane exists to accept.
#   3. code_repo from .autonomous-team/config.json — local and private-plane
#      runs only. Never the only source: that directory is excluded from the
#      export, so it does not exist in the tree this gate runs in on the
#      public plane.
#   4. Otherwise this FAILS — it does not skip. A gate that cannot name what
#      it is comparing against must not report a pass; that is the specific
#      defect (SKIP-on-missing-input) this cutover has already shipped three
#      times.
#
# HOST MATCHING IS EXACT, NOT A SUBDOMAIN WILDCARD
#
# Only the host "github.com" (a leading "www." is tolerated) is treated as a
# repo URL. "*.github.com" is deliberately NOT matched: docs.github.com/en/...
# would parse "en" as an owner and reject a documentation link — a realistic
# false positive on a PR about CI, and false positives are how a guardrail
# gets routed around.
#
# Known gap, accepted rather than silently dropped: the old substring match
# (`github.com/$OWNER`) also caught gist.github.com/<owner>/... links; exact
# host matching does not. Same tradeoff scripts/ci/publish-denylist.sh makes
# for its rename blind spot — a realistic false positive costs more than a
# false negative nobody here produces. Not gold-plated into a subdomain
# allowlist to close it.
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
# Resolve the code plane's own owner. See the header for why this order.
# ---------------------------------------------------------------------------
OWNER="${PR_LINK_POLICY_CODE_OWNER:-}"
OWNER_SOURCE="\$PR_LINK_POLICY_CODE_OWNER"

if [[ -z "$OWNER" && -n "${GITHUB_REPOSITORY:-}" ]]; then
  OWNER="${GITHUB_REPOSITORY%%/*}"
  OWNER_SOURCE="\$GITHUB_REPOSITORY"
fi

if [[ -z "$OWNER" && -f "$REPO_ROOT/.autonomous-team/config.json" ]]; then
  OWNER="$(sed -n 's/.*"code_repo"[[:space:]]*:[[:space:]]*"\([^\/"]*\)\/[^"]*".*/\1/p' \
    "$REPO_ROOT/.autonomous-team/config.json" | head -1)"
  [[ -n "$OWNER" ]] && OWNER_SOURCE="$REPO_ROOT/.autonomous-team/config.json"
fi

if [[ -z "$OWNER" ]]; then
  echo "FAIL: could not resolve the code plane's own owner." >&2
  echo "      Looked at \$PR_LINK_POLICY_CODE_OWNER, \$GITHUB_REPOSITORY, and" >&2
  echo "      code_repo in .autonomous-team/config.json." >&2
  echo "      Refusing to report a pass on a rule this gate cannot evaluate." >&2
  exit 1
fi

# Rule 1's pattern. Deliberately the same three verbs, with the same
# case-insensitive first letter, that scripts/lib/resolve-pr-discussion.sh
# already matches — two mechanisms reading the same PR body for the same
# reference must agree on what counts, or one of them is silently wrong on
# some real PR. Narrowed to the `D#` form: that resolver also accepts a bare
# `#N`, which is an Issue reference and is not what this rule is about.
CLOSES_RE='([Cc]loses|[Rr]esolves|[Ff]ixes) D#[0-9]+'

# Rule 2's pattern: a github.com (or www.github.com) URL, host matched
# exactly — not *.github.com — followed by the owner segment, terminated by
# "/", whitespace, ")", "]", ">", '"', or end of line. Markdown link syntax
# and plain URLs both take this shape.
GITHUB_URL_RE='https?://(www\.)?github\.com/[^]/[:space:])>"]+'

has_closes_ref() { printf '%s' "$1" | grep -Eq "$CLOSES_RE"; }

# Extracts every github.com/<segment> match and compares <segment>
# case-insensitively against the resolved code-plane owner. Returns success
# (0) the moment any match names a different owner — that is a foreign-owner
# URL and rule 2 fails the body.
has_foreign_owner_url() {
  local body="$1" match segment owner_lc restore_nocasematch=0
  owner_lc="${OWNER,,}"
  shopt -q nocasematch || restore_nocasematch=1
  shopt -s nocasematch
  while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    if [[ "$match" =~ ^https?://(www\.)?github\.com/(.+)$ ]]; then
      segment="${BASH_REMATCH[2]}"
      if [[ "${segment,,}" != "$owner_lc" ]]; then
        [[ $restore_nocasematch -eq 1 ]] && shopt -u nocasematch
        return 0
      fi
    fi
  done < <(printf '%s' "$body" | grep -oiE "$GITHUB_URL_RE")
  [[ $restore_nocasematch -eq 1 ]] && shopt -u nocasematch
  return 1
}

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

  # Rule 2 (allowlist) must catch a github.com URL naming any owner other than
  # the resolved code-plane owner, case-insensitively on both the host and
  # the owner segment; must accept a URL into the code plane's own repo; must
  # not fire on a bare D# reference or a body with no URL at all; and must
  # not false-positive on a GitHub *documentation* host, whose first path
  # segment is a locale, not an owner.
  local foreign_owner="${OWNER}-not-us"
  has_foreign_owner_url "see https://github.com/$foreign_owner/repo" \
    || { echo "SELF-TEST FAIL: foreign-owner rule missed a foreign github.com URL" >&2; bad=1; }
  has_foreign_owner_url "SEE HTTPS://GITHUB.COM/${foreign_owner^^}/REPO" \
    || { echo "SELF-TEST FAIL: foreign-owner rule is not case-insensitive" >&2; bad=1; }
  has_foreign_owner_url "Closes D#2348" \
    && { echo "SELF-TEST FAIL: foreign-owner rule fired on a bare D# reference" >&2; bad=1; }
  has_foreign_owner_url "see https://github.com/$OWNER/repo" \
    && { echo "SELF-TEST FAIL: foreign-owner rule fired on the code plane's own repo" >&2; bad=1; }
  has_foreign_owner_url "See https://docs.github.com/en/actions/security-guides/encrypted-secrets" \
    && { echo "SELF-TEST FAIL: foreign-owner rule false-positived on a GitHub documentation host" >&2; bad=1; }

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
echo "pr-link-policy: owner '$OWNER' resolved from $OWNER_SOURCE, body from $BODY_SOURCE, ${#PR_BODY} chars"

VIOLATIONS=0

if ! has_closes_ref "$PR_BODY"; then
  echo "FAIL: the PR body carries no closing reference in the D#NNNN form."
  echo "      Add a line reading: Closes D#<number>"
  echo "      (Resolves/Fixes are accepted too. A bare 'Closes #N' is an Issue"
  echo "      reference and does not satisfy this rule — a PR that closes an"
  echo "      Issue needs both lines.)"
  VIOLATIONS=$((VIOLATIONS + 1))
fi

if has_foreign_owner_url "$PR_BODY"; then
  echo "FAIL: the PR body contains a github.com URL whose owner is not '$OWNER'."
  echo "      A public PR body linking a repo we don't own can publish a dead"
  echo "      link or leak the existence, shape and numbering of a private"
  echo "      twin. Cite the Discussion as a bare 'Closes D#<number>' instead"
  echo "      — no URL. Offending line(s):"
  while IFS= read -r offending_line; do
    has_foreign_owner_url "$offending_line" && printf '        %s\n' "$offending_line"
  done <<<"$PR_BODY"
  VIOLATIONS=$((VIOLATIONS + 1))
fi

if [[ $VIOLATIONS -gt 0 ]]; then
  echo
  echo "FAIL: $VIOLATIONS link-policy rule(s) violated. Edit the PR body and"
  echo "      re-run this check — a PR body can be corrected with no residue,"
  echo "      which is why this rule is enforced here rather than on commits."
  exit 1
fi

echo "PASS: the PR body carries a bare D# closing reference and no foreign-owner github.com URL."
exit 0
