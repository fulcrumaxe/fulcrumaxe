#!/usr/bin/env bash
# scripts/ci/pr-link-policy.sh — enforce how a PR body is allowed to cite the
# Discussion it came from (D#2348 PR-h item 1).
#
# TWO RULES, ONE REASON
#
#   1. The body must carry a machine-readable Discussion reference in the
#      `D#NNNN` form — either a closing one (Closes/Resolves/Fixes) or an
#      advancing one (Refs/Part of/Towards).
#   2. The body must not contain a `github.com/` URL naming any owner other
#      than the code plane's own.
#
# Both come out of the same decision: PRs become public, Discussions stay
# private. A public PR body that links a private Discussion by URL publishes
# a 404 that also leaks shape — that a private twin exists, roughly how much
# work is in it, and how it is numbered. A bare `Closes D#2348` is honest
# about provenance and publishes neither a dead link nor a hostname.
#
# RULE 1 IS ABOUT PROVENANCE, NOT CLOSURE (D#2401)
#
# The reasoning above — provenance, no dead link, no leaked hostname — is
# satisfied exactly as well by `Refs D#2401` as by `Closes D#2401`. Nothing in
# it requires the reference to be a *closing* one, so rule 1 originally
# conflated two different requirements: "every PR names its Discussion" (the
# actual reason this gate exists) and "every PR closes a Discussion" (never
# the reason, and a PR that only advances a multi-PR Discussion could not say
# so honestly). D#2401 split them apart: rule 1 now accepts either verb class.
#
# This gives up nothing on premature closes. That protection was never this
# gate's job — it lives in `planned_prs` plus
# `scripts/lib/discussion-close-guard.sh::discussion_close_decision`, which
# holds on unknown and is unchanged by this. Whether a Discussion closes is
# decided there, after merge, from the Discussion body — never from which verb
# a PR body happened to use.
#
# The advancing verbs are accepted ONLY in the `D#` form, never bare `#N`.
# `Refs #123` is an ordinary cross-reference to a PR or Issue on the code
# plane, and reading it as Discussion 123 would silently resolve to the wrong
# thing. The closing verbs keep their existing `(D#|#)` behaviour — unchanged,
# relied on elsewhere (an Issue closed via `Closes #N`).
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
# HOST MATCHING IS EXACT AND SCHEME-AGNOSTIC — NOT A SUBDOMAIN WILDCARD AND
# NOT A BARE SUBSTRING
#
# Only the host "github.com" (a leading "www." is tolerated) is treated as a
# repo URL. "*.github.com" is deliberately NOT matched: docs.github.com/en/...
# would parse "en" as an owner and reject a documentation link — a realistic
# false positive on a PR about CI, and false positives are how a guardrail
# gets routed around.
#
# The match does not require a URL scheme, because the exact case this gate
# exists to catch doesn't carry one: "Context: github.com/some-private-org/
# enginerepo/discussions/2438" (D#2438's own motivating example). A pattern
# that required "https?://" would pass that clean. But dropping the scheme
# requirement and grepping for the bare substring "github.com/" reopens a
# DIFFERENT false positive: "notgithub.com/someowner" and "mygithub.com/
# someowner" both contain that substring. The fix that closes the scheme gap
# without reopening the substring gap is to extract the FULL contiguous
# hostname-like token around any "github.com" occurrence (greedy on both
# sides — "notgithub.com" and "docs.github.com" both extract in full, not
# just their "github.com" tail) and require that whole token to equal
# "github.com" or "www.github.com" exactly, case-insensitively, before an
# owner check ever runs on it. A token that differs by even one leading or
# trailing character is a different host and is never treated as a GitHub
# URL — scheme or no scheme. See has_foreign_owner_url() for the two-step
# implementation (POSIX/bash regex has no lookbehind to anchor this in one
# pattern).
#
# Known gap, accepted rather than silently dropped: this still does not
# match a github.com *subdomain* — gist.github.com/<owner>/... included —
# because its full hostname token differs from "github.com" the same way
# "docs.github.com" does. Same tradeoff scripts/ci/publish-denylist.sh makes
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
# Discussion reference.
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

# Rule 1's closing pattern. Deliberately the same three verbs, with the same
# case-insensitive first letter, that scripts/lib/resolve-pr-discussion.sh
# already matches — two mechanisms reading the same PR body for the same
# reference must agree on what counts, or one of them is silently wrong on
# some real PR. Narrowed to the `D#` form: that resolver also accepts a bare
# `#N`, which is an Issue reference and is not what this rule is about.
CLOSES_RE='([Cc]loses|[Rr]esolves|[Ff]ixes) D#[0-9]+'

# Rule 1's advancing pattern (D#2401) — a PR that names its Discussion without
# closing it. Kept as a SEPARATE pattern from CLOSES_RE rather than merged
# into one alternation: that makes the `D#`-only restriction structural. The
# advancing class simply has no `#`-only alternative to accidentally get
# right — there is nothing to narrow, because it was never given the option.
# `resolve-pr-discussion.sh:144` extends the same way, same restriction.
ADVANCES_RE='([Rr]efs|[Pp]art of|[Tt]owards) D#[0-9]+'

# Rule 2's pattern is scheme-agnostic on purpose: D#2438's own motivating
# example ("Context: github.com/some-private-org/enginerepo/discussions/2438")
# has no "https://" at all, and the old denylist was a bare substring match
# that caught it regardless of scheme. A pattern that required a scheme would
# pass that exact case clean — a real hole, not a theoretical one.
#
# The fix is NOT to drop the host anchor and grep for the substring
# "github.com/" — that reopens the false-positive this design exists to
# avoid: "notgithub.com/someowner" and "mygithub.com/someowner" both contain
# "github.com/someowner" as a substring, and under an allowlist a substring
# hit on an unrelated host is a false positive that blocks a legitimate PR.
#
# So the match is done in two steps instead of one regex: grep extracts the
# FULL contiguous hostname-like token surrounding any "github.com" occurrence
# — greedy on both sides, so "notgithub.com" extracts as "notgithub.com" in
# full and "docs.github.com" extracts as "docs.github.com" in full, not just
# the "github.com" tail — and only THEN is that whole token compared for
# exact (case-insensitive) equality against "github.com" or "www.github.com".
# A token that differs by so much as one leading or trailing character, on
# either side, is a different host and is never treated as a GitHub URL at
# all — no owner check runs on it, scheme or no scheme. This is what anchors
# the host without needing lookbehind, which POSIX/bash regex doesn't have.
GITHUB_HOST_TOKEN_RE='[A-Za-z0-9.-]*github\.com[A-Za-z0-9.-]*(/[^]/[:space:])>"]*)?'

has_closes_ref() { printf '%s' "$1" | grep -Eq "$CLOSES_RE"; }
has_advances_ref() { printf '%s' "$1" | grep -Eq "$ADVANCES_RE"; }

# Extracts every hostname-token(/owner-segment)? candidate touching
# "github.com" and, for each one whose FULL token is exactly "github.com" or
# "www.github.com", compares the owner segment case-insensitively against the
# resolved code-plane owner. Returns success (0) the moment any candidate
# names a real GitHub host with a different owner — that is a foreign-owner
# URL and rule 2 fails the body. A bare "github.com" mention with no "/"
# after it (no owner segment at all) is not flagged: rule 2 is about a link
# to a specific repo, and there is no repo named here to be foreign or not.
has_foreign_owner_url() {
  local body="$1" candidate host host_lc segment owner_lc
  owner_lc="${OWNER,,}"
  while IFS= read -r candidate; do
    [[ -z "$candidate" ]] && continue
    if [[ "$candidate" =~ ^([A-Za-z0-9.-]+)(/(.+))?$ ]]; then
      host="${BASH_REMATCH[1]}"
      segment="${BASH_REMATCH[3]}"
      host_lc="${host,,}"
      if [[ "$host_lc" == "github.com" || "$host_lc" == "www.github.com" ]] \
        && [[ -n "$segment" && "${segment,,}" != "$owner_lc" ]]; then
        return 0
      fi
    fi
  done < <(printf '%s' "$body" | grep -oiE "$GITHUB_HOST_TOKEN_RE")
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

  # Rule 1 (closing form) must accept each accepted verb, and reject a body
  # with no reference and a body whose only reference is a bare Issue `#N`.
  local good
  for good in "Closes D#2348" "resolves D#7 in the body" "Fixes D#1"; do
    has_closes_ref "$good" || { echo "SELF-TEST FAIL: closing-reference rule rejected '$good'" >&2; bad=1; }
  done
  local bad_body
  for bad_body in "" "No reference at all." "Closes #2348" "Closes D#" "closes d#2348"; do
    has_closes_ref "$bad_body" && { echo "SELF-TEST FAIL: closing-reference rule accepted '$bad_body'" >&2; bad=1; }
  done

  # Rule 1 (advancing form, D#2401) must accept each accepted verb in the D#
  # form, upper- and lower-case, and must NOT accept the bare `#N` form — that
  # is an ordinary PR/Issue cross-reference, not a Discussion reference, and
  # reading it as one would silently resolve to the wrong thing.
  for good in "Refs D#2401" "refs D#2401" "Part of D#2401" "part of D#2401" \
    "Towards D#2401" "towards D#2401"; do
    has_advances_ref "$good" || { echo "SELF-TEST FAIL: advancing-reference rule rejected '$good'" >&2; bad=1; }
  done
  for bad_body in "" "No reference at all." "Refs #2401" "refs #2401" \
    "Part of #2401" "Towards #2401" "Refs D#" "refs d#2401"; do
    has_advances_ref "$bad_body" && { echo "SELF-TEST FAIL: advancing-reference rule accepted '$bad_body'" >&2; bad=1; }
  done
  # An advancing reference alone (no closing reference) must satisfy rule 1 as
  # a whole, and a bare `Refs #N` (no D) must not.
  { has_closes_ref "Refs D#2401" || has_advances_ref "Refs D#2401"; } \
    || { echo "SELF-TEST FAIL: rule 1 rejected an advancing-only body 'Refs D#2401'" >&2; bad=1; }
  { has_closes_ref "Refs #2401" || has_advances_ref "Refs #2401"; } \
    && { echo "SELF-TEST FAIL: rule 1 accepted bare 'Refs #2401' as a Discussion reference" >&2; bad=1; }

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
  # A bare (scheme-less) foreign-owner mention must still be caught — this is
  # D#2438's own motivating example, and the specific bug a fix-round found:
  # a scheme-anchored pattern passes it clean.
  has_foreign_owner_url "Context: github.com/$foreign_owner/enginerepo/discussions/2438" \
    || { echo "SELF-TEST FAIL: foreign-owner rule missed a scheme-less github.com mention" >&2; bad=1; }
  # And a bare mention of our OWN repo, no scheme, must still pass.
  has_foreign_owner_url "Context: github.com/$OWNER/somerepo" \
    && { echo "SELF-TEST FAIL: foreign-owner rule fired on a scheme-less mention of the code plane's own repo" >&2; bad=1; }
  # The trap the naive fix (dropping the scheme and grepping the substring
  # "github.com/") falls into: a host that merely CONTAINS "github.com" as a
  # tail is not github.com and must not be flagged, with or without a scheme.
  has_foreign_owner_url "see notgithub.com/$foreign_owner/repo" \
    && { echo "SELF-TEST FAIL: foreign-owner rule false-positived on notgithub.com (substring trap)" >&2; bad=1; }
  has_foreign_owner_url "see https://mygithub.com/$foreign_owner/repo" \
    && { echo "SELF-TEST FAIL: foreign-owner rule false-positived on mygithub.com (substring trap)" >&2; bad=1; }
  # A bare host mention with no path/owner at all is not a link to any repo,
  # foreign or otherwise, and must not be flagged.
  has_foreign_owner_url "See github.com for more info, no link here." \
    && { echo "SELF-TEST FAIL: foreign-owner rule fired on a bare host mention with no owner segment" >&2; bad=1; }

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

if ! has_closes_ref "$PR_BODY" && ! has_advances_ref "$PR_BODY"; then
  echo "FAIL: the PR body carries no Discussion reference in the D#NNNN form."
  echo "      Add a line reading: Closes D#<number> — if this PR finishes the"
  echo "      Discussion (Resolves/Fixes are accepted too) — or:"
  echo "      Refs D#<number> — if it only advances one (Part of/Towards are"
  echo "      accepted too)."
  echo "      A bare '#N' (no D) never satisfies this rule for either verb"
  echo "      class — that is a PR/Issue reference, not a Discussion one. A PR"
  echo "      that closes an Issue needs its own 'Closes #N' line in addition."
  VIOLATIONS=$((VIOLATIONS + 1))
fi

if has_foreign_owner_url "$PR_BODY"; then
  echo "FAIL: the PR body contains a github.com URL whose owner is not '$OWNER'."
  echo "      A public PR body linking a repo we don't own can publish a dead"
  echo "      link or leak the existence, shape and numbering of a private"
  echo "      twin. Cite the Discussion as a bare 'Closes D#<number>' or"
  echo "      'Refs D#<number>' instead — no URL. Offending line(s):"
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

echo "PASS: the PR body carries a bare D# Discussion reference and no foreign-owner github.com URL."
exit 0
