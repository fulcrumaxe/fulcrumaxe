#!/usr/bin/env bash
# scripts/lib/identifier-rules-resolve.sh — resolve the identifier rules
# source to an ENUMERATED STATE, never a bare path. Sibling to
# scripts/lib/repo-resolve.sh; same sourcing idiom.
#
# Usage (almost every real caller — via command substitution, which forks
# a subshell, so the state AND the path must both travel on stdout; a
# side-effect global set inside that subshell would never reach here):
#   source "$(dirname "${BASH_SOURCE[0]}")/identifier-rules-resolve.sh"
#   IFS=$'\t' read -r STATE RULES_PATH <<< "$(_resolve_identifier_rules_state "$candidate1" ["$candidate2" ...])"
#
# One or more candidate file paths are checked in order; the first one that
# exists on disk is the resolved rules file. Most consumers pass exactly
# one candidate; scripts/ci/pr-link-policy.sh has two search locations and
# passes both, in priority order.
#
# Output is always exactly one line, "STATE\tPATH" — PATH is empty for
# "missing". Read it with the IFS=$'\t' read idiom above, not by comparing
# the whole captured string to a bare state word: "missing" alone would
# never match "missing\t".
#
# D#2399: backports the three-state design already shipped (unlabelled) on
# the code plane's scripts/ci/repo-target-gate.sh:212-221 (D#2492, D#2545).
# This file is new here; the *logic* is not.
#
# The STATE field is exactly one of three tokens, and nothing else — never
# a path, never empty, never a fourth value:
#
#   present        A candidate exists and carries an
#                  "=== IDENTITIES_START ===" block — real identity data.
#   declared-none  A candidate exists, carries no IDENTITIES block, but
#                  does carry a bare "NO_IDENTITIES=declared" line — this
#                  tree has deliberately said it carries no identities.
#   missing        No candidate exists on disk, OR a candidate exists but
#                  says NOTHING about itself (no IDENTITIES block and no
#                  NO_IDENTITIES=declared line). The second case is
#                  deliberately folded into "missing", not treated as a
#                  softer case: "I could not find a header line" and "I
#                  have deliberately declared this tree carries no
#                  identities" are different statements, and only the
#                  second is safe to trust with a clean exit. A tree that
#                  says nothing about itself gets enforcement, exactly
#                  like a tree with no file at all — never a silent skip.
#
# For a caller that invokes the function DIRECTLY (no subshell — output
# redirected with a plain `>`, not captured with `$(...)`),
# $IDENTIFIER_RULES_RESOLVED_PATH is also set as a convenience, same
# contents as the PATH field. Never rely on it after a command-substitution
# capture — that forks a subshell, and the assignment never leaves it.
#
# This is a state resolver, not a rules parser: it answers exactly one
# question — is there a usable rules source, and does it say anything about
# carrying no identities — and nothing else. Pattern parsing, exemptions
# and the allowlist stay entirely inside scripts/check-forbidden-identifiers.sh
# and open-source/lib/identifier_rewrite.py, where they already live. This
# file adds no second copy of either, so a consumer that also needs the
# FORBIDDEN_PATTERNS/PREPUSH_EXEMPT data still reads it from the resolved
# path itself, through the one parser that already owns that job.
#
# "declared-none" is scoped to the IDENTITIES block only. A rules file can
# declare NO_IDENTITIES=declared (nothing to hunt for repo-target-gate.sh's
# owner-identity question) while still carrying its own FORBIDDEN_PATTERNS
# — the two questions are independent, and this resolver only answers the
# first. A consumer whose job is FORBIDDEN_PATTERNS (not IDENTITIES) should
# treat "present" and "declared-none" the same — proceed and read the file
# — and reserve "missing" as its only hard-stop.

_resolve_identifier_rules_state() {
  local candidate rules_file=""

  for candidate in "$@"; do
    if [[ -f "$candidate" ]]; then
      rules_file="$candidate"
      break
    fi
  done

  unset IDENTIFIER_RULES_RESOLVED_PATH

  if [[ -z "$rules_file" ]]; then
    printf 'missing\t\n'
    return 0
  fi

  # Exact equality on the trimmed line, matching parse_block's own marker
  # test in check-forbidden-identifiers.sh and identifier-gate.sh — never a
  # substring/prefix match. A prefix match here would resolve "present" for
  # a line that starts with the marker and continues with prose (the rules
  # file's own header text explaining the marker, for instance), while
  # parse_block finds no block there at all — the resolver would be the
  # looser of the two, silently, which is exactly the class of bug D#1844
  # fixed once already for this same marker-matching shape.
  if grep -qE '^[[:space:]]*=== IDENTITIES_START ===[[:space:]]*$' "$rules_file"; then
    IDENTIFIER_RULES_RESOLVED_PATH="$rules_file"
    printf 'present\t%s\n' "$rules_file"
    return 0
  fi

  local declared
  declared="$(sed -n 's/^[[:space:]]*NO_IDENTITIES=\(.*\)$/\1/p' "$rules_file" | head -1)"
  # Trim BOTH ends. Trailing-only trimming let a value with a leading space
  # ("NO_IDENTITIES= declared") fail the equality check below even though
  # it is a well-formed declaration.
  declared="${declared#"${declared%%[![:space:]]*}"}"
  declared="${declared%"${declared##*[![:space:]]}"}"
  if [[ "$declared" == "declared" ]]; then
    IDENTIFIER_RULES_RESOLVED_PATH="$rules_file"
    printf 'declared-none\t%s\n' "$rules_file"
    return 0
  fi

  printf 'missing\t\n'
  return 0
}
