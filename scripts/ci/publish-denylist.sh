#!/usr/bin/env bash
# scripts/ci/publish-denylist.sh — fail a PR that ADDS a path which must
# never reach the published tree (D#2348 PR-h item 2).
#
# WHY THIS EXISTS
#
# open-source/MANIFEST.md's "Explicitly excluded" list is enforced today by
# four checks that all run against a PRODUCED EXPORT TREE
# (shipped-to-listed.sh, shipped-dir-inventory.sh, shipped-extensions.sh,
# tracked-provenance.sh). Once development happens in the public repo there
# is no export tree to audit — the repository is the artifact — so the same
# list has to be re-expressed as a gate over the diff. That is this file. It
# collapses those four into one because they were four answers to a single
# question: did anything land where it must not land.
#
# SUBJECT: ADDED PATHS ONLY, NOT THE TREE
#
# This is a diff gate, deliberately. .env.example and 1801 files under
# archive/ are already tracked and are supposed to stay tracked — the
# private repo keeps archive/ and .autonomous-team/ by the same owner
# decision that put this gate here. A tree-wide scan would be red on every
# PR forever, which is a gate that gets switched off. The question this
# answers is "did THIS PR put something new there", which is the question
# with an actionable answer.
#
# RENAME HANDLING — a documented, deliberate blind spot
#
# git's rename detection is left ON (the default). A pure `git mv` of an
# existing file into a denied prefix is therefore reported as R, not A, and
# does not fail this gate. That is a real hole and it is chosen: CLAUDE.md's
# Archive Protocol REQUIRES `git mv` into archive/<name>-<date>/ and forbids
# `git rm`, so a gate that reddened on every protocol-compliant archive move
# would be red on exactly the PRs that are doing the right thing. Over-
# blocking is how a guardrail gets routed around. What still fails: new
# content written under a denied prefix, and a move so heavily edited that
# git scores it below the rename-similarity threshold (a 50%-changed file
# comes through as A+D).
#
# NO SELF-EXCLUSION LIST, ON PURPOSE
#
# The sibling gates in this directory carry a SCAN_EXCLUDE_PREFIXES block
# because they scan file CONTENT and would otherwise flag their own pattern
# data. This one reads PATHS. Its own path (scripts/ci/) and its test's
# (tests/) are not denied by any rule below, so it cannot trip on itself and
# needs no carve-out — a carve-out here would be a hole with no reason.
#
# SELF-TEST ON EVERY RUN
#
# Three gates in this cutover shipped in a state where they could not fail,
# and all three were found by a person going to look rather than by anything
# going red. So this file runs its own classifier against a fixed set of
# paths that MUST be denied and paths that MUST be allowed before it looks
# at the real diff, and aborts if any verdict is wrong. A clean repository
# and a broken classifier no longer look the same from the outside.
#
# Usage:
#   bash scripts/ci/publish-denylist.sh [base-ref]
#
# base-ref defaults to $PUBLISH_DENYLIST_BASE, then to origin/main. It is
# resolved against the repository in the current working directory, which is
# what makes this testable against a fixture repo (cd into it) rather than
# only against the live tree.
#
# Exit 0 = no added path is under a denied rule.
# Exit 1 = at least one added path is denied, the base ref does not resolve,
#          the diff could not be computed, or the self-test failed. There is
#          deliberately no SKIP branch: a gate that cannot measure must not
#          report a pass.
# Exit 2 = usage error.

set -uo pipefail

if [[ $# -gt 1 ]]; then
  echo "usage: $(basename "$0") [base-ref]" >&2
  exit 2
fi

BASE="${1:-${PUBLISH_DENYLIST_BASE:-origin/main}}"

# ---------------------------------------------------------------------------
# The denylist. Source: D#2348 PR-h item 2, reconciled against
# open-source/MANIFEST.md's "Explicitly excluded" section and against the
# owner's final push-set decision recorded on D#2348. Two reconciliations
# are worth writing down here rather than leaving in a PR description:
#
#   1. The Spec writes "gemma-sandbox/". No such path exists at the repo
#      root — the directory is scripts/gemma-sandbox/ (6 tracked files), and
#      that is how MANIFEST.md and scripts/ci/repo-target-gate.sh both spell
#      it. Implementing the Spec's spelling literally would have produced a
#      rule that can never match, which is the precise defect this Spec item
#      exists to prevent. Both spellings are listed: the real one so the
#      rule fires, the root one so a future top-level directory of that name
#      is not a silent gap.
#
#   2. The owner's final push set also excludes wiki/, open-source/,
#      docker/, systemd/, verification-report/, templates/ and
#      dashboard_tui/. Those are NOT here. They are "exists in this repo,
#      does not ship", not "must never exist" — wiki/ is written to by
#      docs-writer and open-source/ is edited by this Discussion's own
#      remaining PRs, so denying additions to them would block ordinary
#      private-repo work today for a publishing rule that only binds at push
#      time. Enforcing the push set is PR-l's job and it enforces it on the
#      push, which is where it belongs.
# ---------------------------------------------------------------------------
DENIED_PREFIXES=(
  ".autonomous-team/"
  "archive/"
  "scripts/training/"
  "scripts/serving/"
  "scripts/gemma-sandbox/"
  "gemma-sandbox/"
)

# Matched against the whole path, not just the basename, so a file inside a
# directory such as .envs/ is caught too. rsync's own guard in
# open-source/lib/rsync-excludes.sh is spelled the same way and matches path
# components for the same reason.
DENIED_GLOB="*.env*"

# One carve-out from that glob, decided rather than inherited. `*.env*` exists
# to stop credentials; `.env.example` is the documented opposite of a
# credential file — the tracked one at the repo root carries exactly two
# credential lines and both are commented out. It is also on PR-l's seed path,
# so leaving the bare glob in place would have decided, as a side effect of a
# pattern, that this project ships no example environment file at all. That is
# a decision worth making on purpose or not at all.
#
# The carve-out is an EXACT BASENAME match, not a suffix match. `backend/.env.example`
# is allowed because its basename is `.env.example`; `prod.env.example` and
# `.env.example.bak` are still denied, because a suffix rule would let any
# file be smuggled through by renaming it. Both directions are in the
# self-test below so the boundary is asserted rather than described.
ENV_TEMPLATE_BASENAME=".env.example"

# Prints the rule that denies $1 and returns 0; returns 1 if nothing denies it.
denied_reason() {
  local path="$1" prefix
  for prefix in "${DENIED_PREFIXES[@]}"; do
    if [[ "$path" == "$prefix"* ]]; then
      printf 'under %s\n' "$prefix"
      return 0
    fi
  done
  [[ "$(basename -- "$path")" == "$ENV_TEMPLATE_BASENAME" ]] && return 1
  # shellcheck disable=SC2254  # intentional glob match, not a literal
  case "$path" in
    $DENIED_GLOB)
      printf 'matches %s\n' "$DENIED_GLOB"
      return 0
      ;;
  esac
  return 1
}

# ---------------------------------------------------------------------------
# Self-test. Runs before the real diff on every invocation. Each MUST_DENY
# entry proves a rule still fires; each MUST_ALLOW entry proves it still
# discriminates. The MUST_ALLOW set is chosen to be adversarial rather than
# decorative: "scripts/trainingwheels.sh" and "docs/archived-notes.md" both
# start with a denied prefix's text and are separated from it only by the
# trailing slash, and "backend/environment.py" contains "env" but not ".env".
# A prefix rule that lost its slash, or a glob that decayed to a bare "env"
# substring, would over-block and this catches that direction too.
# ---------------------------------------------------------------------------
MUST_DENY=(
  ".autonomous-team/project.json"
  "archive/some-tool-2026-09-04/README.md"
  "scripts/training/train.py"
  "scripts/serving/deploy.sh"
  "scripts/gemma-sandbox/loop.sh"
  "gemma-sandbox/loop.sh"
  ".env"
  "backend/.env.local"
  "config/.envs/keys.txt"
  "prod.env.example"
  ".env.example.bak"
  "archive/x/.env.example"
)
MUST_ALLOW=(
  "scripts/ci/publish-denylist.sh"
  "tests/test_publish_denylist.sh"
  "scripts/trainingwheels.sh"
  "scripts/serve.sh"
  "docs/archived-notes.md"
  "backend/environment.py"
  "autonomous-team-notes.md"
  ".env.example"
  "backend/.env.example"
)

self_test() {
  local bad=0 p
  for p in "${MUST_DENY[@]}"; do
    if ! denied_reason "$p" >/dev/null; then
      echo "SELF-TEST FAIL: '$p' should be denied and was not" >&2
      bad=1
    fi
  done
  for p in "${MUST_ALLOW[@]}"; do
    local reason
    if reason="$(denied_reason "$p")"; then
      echo "SELF-TEST FAIL: '$p' should be allowed and was denied ($reason)" >&2
      bad=1
    fi
  done
  if [[ $bad -ne 0 ]]; then
    echo "FAIL: publish-denylist self-test failed — the classifier no longer discriminates, so its verdict on the real diff means nothing" >&2
    return 1
  fi
  echo "self-test: ${#MUST_DENY[@]} denied / ${#MUST_ALLOW[@]} allowed, all as expected"
  return 0
}

self_test || exit 1

# ---------------------------------------------------------------------------
# The real diff.
# ---------------------------------------------------------------------------
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "FAIL: $(pwd) is not a git checkout — this gate reads a diff and cannot report a pass without one" >&2
  exit 1
fi

BASE_SHA="$(git rev-parse --verify --quiet "${BASE}^{commit}" 2>/dev/null)"
if [[ -z "$BASE_SHA" ]]; then
  echo "FAIL: base ref '$BASE' does not resolve to a commit in this checkout." >&2
  echo "      A shallow clone is the usual cause: fetch the base branch (or use" >&2
  echo "      actions/checkout with fetch-depth: 0) before running this gate." >&2
  echo "      Refusing to pass without measuring anything." >&2
  exit 1
fi

# -z, and not the plain --name-only this first shipped with. Without it git
# applies core.quotePath: any path it considers unprintable — non-ASCII, a
# quote, a backslash — comes back wrapped in double quotes with its bytes
# octal-escaped. `".autonomous-team/\303\251tat.json"` does not start with
# `.autonomous-team/`, so every prefix rule below silently missed it and the
# gate passed with a clean self-test. Measured on a fixture: two files added
# under .autonomous-team/ and archive/ scored rc=0.
#
# -z fixes it at the source — it emits paths verbatim, NUL-delimited, with no
# quoting at all — and it also handles a newline inside a filename, which
# `-c core.quotePath=false` alone does not.
DIFF_OUT="$(mktemp)"
DIFF_ERR="$(mktemp)"
trap 'rm -f "$DIFF_OUT" "$DIFF_ERR"' EXIT
git diff -z --diff-filter=A --name-only "${BASE_SHA}...HEAD" >"$DIFF_OUT" 2>"$DIFF_ERR"
DIFF_RC=$?
if [[ $DIFF_RC -ne 0 ]]; then
  echo "FAIL: could not compute the added-file set against $BASE ($BASE_SHA):" >&2
  cat "$DIFF_ERR" >&2
  exit 1
fi

ADDED=()
while IFS= read -r -d '' path; do
  [[ -n "$path" ]] && ADDED+=("$path")
done <"$DIFF_OUT"

# Parse invariants, checked against the real diff rather than against fixture
# data. The self-test above proves the classifier discriminates, but it feeds
# the classifier paths this script constructs itself — so it could not, and
# did not, see the quoting defect, which lives in the step BEFORE the
# classifier ever runs. These two checks sit on the real path.
#
# The first invariant is the one that matters. Dropping -z does not produce
# quoted paths here — it produces NO paths: git emits newline-delimited output,
# `read -d ''` finds no NUL, and the loop above exits without a single
# iteration. The gate then reports "added paths=0" and passes everything,
# which is the most dangerous shape a gate can have. So: output present but
# nothing parsed out of it is a hard failure, not an empty result.
if [[ -s "$DIFF_OUT" && ${#ADDED[@]} -eq 0 ]]; then
  echo "FAIL: git reported added files but none were parsed out of its output." >&2
  echo "      That means the diff is not being read NUL-delimited — check that" >&2
  echo "      'git diff -z' is intact. Refusing to report a pass on a file list" >&2
  echo "      this script could not read." >&2
  exit 1
fi

# The second catches a path that reached the classifier still wrapped in git's
# quoting, whatever the route. A file whose name genuinely begins with a double
# quote would trip it; that is fail-closed and vanishingly rare, and a loud
# false failure is the right side to err on for a rule about what gets
# published.
for path in "${ADDED[@]+"${ADDED[@]}"}"; do
  if [[ "$path" == '"'* ]]; then
    echo "FAIL: '$path' arrived from git quoted." >&2
    echo "      No prefix rule can match a quoted path. Restore 'git diff -z' —" >&2
    echo "      see the comment above this check." >&2
    exit 1
  fi
done

echo "publish-denylist: base=$BASE ($BASE_SHA), added paths=${#ADDED[@]}"

VIOLATIONS=0
for path in "${ADDED[@]+"${ADDED[@]}"}"; do
  if reason="$(denied_reason "$path")"; then
    echo "FAIL: $path — $reason, which is excluded from the published tree"
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
done

if [[ $VIOLATIONS -gt 0 ]]; then
  echo
  echo "FAIL: $VIOLATIONS added path(s) land under a publish denylist rule."
  echo "      These paths are excluded from what this project publishes (see"
  echo "      open-source/MANIFEST.md, 'Explicitly excluded'). Move the file"
  echo "      somewhere that ships, or if it genuinely belongs in an excluded"
  echo "      directory, that is a decision for a Discussion and not for this PR."
  exit 1
fi

echo "PASS: no added path is under a publish denylist rule."
exit 0
