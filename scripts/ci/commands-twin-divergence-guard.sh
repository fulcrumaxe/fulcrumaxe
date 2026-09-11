#!/usr/bin/env bash
# scripts/ci/commands-twin-divergence-guard.sh — every .claude/commands/*.md
# file has a byte-identical twin at top-level commands/ (D#2486).
#
# Why this exists
# ----------------
# .claude/commands/ and top-level commands/ carry the SAME three files today,
# duplicated by whatever seeds the public export (see
# open-source/MANIFEST.md's GENERATED_PATHS block, engine-side: commands/ is
# export.sh's own generated mirror of .claude/commands/, not an independently
# maintained copy). Nothing enforced that the two ever agreed, so a PR that
# edited only .claude/commands/start-the-day.md left a stale, since-corrected
# instruction alive in commands/start-the-day.md with nothing marking it
# stale. This guard is the check that would have caught that PR.
#
# What it checks
# ---------------
# For every .claude/commands/*.md file, paired with commands/<same-basename>
# by basename (not a hardcoded list — a fourth command file is covered the
# day it lands):
#
#   - Both exist and are byte-identical           -> PASS
#   - Both exist and differ                        -> FAIL, names the pair
#   - .claude/commands/<name>.md has no top-level twin -> FAIL, names it
#
# The asymmetric case is a deliberate choice, not an oversight (D#2486 item
# 4 asks for both and a stated reason):
#
#   .claude/commands/<name>.md with NO commands/<name>.md twin -> FAIL LOUD.
#   Every .claude/commands/ file has had a top-level twin since this pairing
#   existed at all, and the entire point of pairing by basename instead of a
#   hardcoded three-name list is to catch a NEW .claude/commands/ file
#   landing without its export mirror in the same PR — the same failure
#   shape as content drift, just at file-creation time instead of edit time.
#
#   commands/<name>.md with NO .claude/commands/<name>.md counterpart ->
#   NOTED, never fails. Whether top-level commands/ is purely a generated
#   mirror, an independent adopter-facing surface, or both is an open
#   structural question this guard is deliberately not positioned to answer
#   (see the Discussion this guard was written for) — failing here would
#   make the guard cast its own vote on that undecided question. Silence
#   would be the same defect shape the divergence itself is, so it is always
#   printed as a NOTE line, just never turned into a failing exit code.
#
# Usage
# -----
#   bash scripts/ci/commands-twin-divergence-guard.sh
#
# Exit 0: every .claude/commands/*.md file has an identical top-level twin.
# Exit 1: a pair diverged, a twin is missing, or .claude/commands/ itself is
#         absent.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

CLAUDE_DIR=".claude/commands"
TOP_DIR="commands"

if [ ! -d "$CLAUDE_DIR" ]; then
  echo "commands-twin-divergence-guard: FAIL — $CLAUDE_DIR is not a directory" >&2
  exit 1
fi

FAILED=0
MATCHED=0

while IFS= read -r claude_path; do
  name="$(basename "$claude_path")"
  top_path="$TOP_DIR/$name"

  if [ ! -f "$top_path" ]; then
    echo "FAIL $name — $claude_path has no top-level twin at $top_path"
    FAILED=$((FAILED + 1))
    continue
  fi

  if cmp -s "$claude_path" "$top_path"; then
    echo "PASS $name — identical"
    MATCHED=$((MATCHED + 1))
  else
    echo "FAIL $name — $claude_path and $top_path differ:"
    diff -u "$top_path" "$claude_path" | sed 's/^/    /'
    FAILED=$((FAILED + 1))
  fi
done < <(find "$CLAUDE_DIR" -maxdepth 1 -type f -name '*.md' | sort)

if [ -d "$TOP_DIR" ]; then
  while IFS= read -r top_path; do
    name="$(basename "$top_path")"
    if [ ! -f "$CLAUDE_DIR/$name" ]; then
      echo "NOTE $name — $top_path has no .claude/commands/ counterpart (not flagged: structural question open, see D#2486)"
    fi
  done < <(find "$TOP_DIR" -maxdepth 1 -type f -name '*.md' | sort)
fi

echo
if [ "$FAILED" -gt 0 ]; then
  echo "commands-twin-divergence-guard: FAIL — $FAILED pair(s) diverged or missing a twin, $MATCHED matched" >&2
  exit 1
fi

echo "commands-twin-divergence-guard: OK — $MATCHED pair(s) identical"
exit 0
