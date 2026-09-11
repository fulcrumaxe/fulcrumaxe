#!/usr/bin/env bash
# tests/test_unpinned_gh_pr_guard.sh — mutation test for
# scripts/ci/unpinned-gh-pr-guard.sh (D#2523).
#
# A guard that only ever reports today's tree passing is "documented but
# unenforced" — the exact failure shape the Spec calls out. This proves the
# guard can actually fail: it builds a private fixture tree under
# .claude/agents/*.md, runs the REAL guard against it with --root (not a
# reimplementation, not a dry-run — --root only changes which directory is
# scanned, matching run-guards.sh's own --dir contract, so this is evidence
# about the real check per D#2149), and checks both directions:
#
#   1. An unpinned `gh pr` statement introduced into a card -> guard FAILS,
#      naming the fixture file and line.
#   2. The same statement pinned with ${CODE_REPO:?...} resolved in the same
#      statement -> guard PASSES.
#
# It also checks the two non-firing cases the Spec requires (item 5):
#   3. A `gh api repos/` statement correctly pinned to the Discussion-plane
#      literal -> guard does not fire.
#   4. Prose that merely mentions `gh pr` (a markdown bullet, an inline aside)
#      -> guard does not fire.
#
# Exit 0: all four checks behaved as expected.
# Exit 1: any one did not.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="$REPO_ROOT/scripts/ci/unpinned-gh-pr-guard.sh"
NAME="$(basename "${BASH_SOURCE[0]}")"

if [[ ! -f "$GUARD" ]]; then
  echo "$NAME: FAIL — $GUARD does not exist" >&2
  exit 1
fi

FIX="$(mktemp -d)" || { echo "$NAME: FAIL — mktemp -d failed" >&2; exit 1; }
cleanup() { rm -rf "$FIX"; }
trap cleanup EXIT

mkdir -p "$FIX/.claude/agents"

FAILED=0

# ---------------------------------------------------------------------------
# Check 1 — an unpinned gh pr statement fails, naming file and line.
# ---------------------------------------------------------------------------

CARD="$FIX/.claude/agents/fixture-role.md"
cat > "$CARD" <<'EOF'
---
name: fixture-role
---

# Fixture Role

## Workflow

1. Check the PR:
   gh pr view {pr_number} --repo unpinned/example
EOF

OUT1="$(bash "$GUARD" --root "$FIX" 2>&1)"
RC1=$?
echo "--- check 1: unpinned gh pr statement (real run against --root fixture) — exit $RC1"
printf '%s\n' "$OUT1" | sed 's/^/    /'

if [[ "$RC1" -eq 0 ]]; then
  echo "$NAME: FAIL — check 1: guard passed on a fixture with an unpinned gh pr statement" >&2
  FAILED=1
fi
if ! printf '%s\n' "$OUT1" | grep -qF "fixture-role.md:10:"; then
  echo "$NAME: FAIL — check 1: guard did not name fixture-role.md:10 in its failure output" >&2
  FAILED=1
fi

# ---------------------------------------------------------------------------
# Check 2 — pinning the same statement (resolve in the same statement) makes
# the guard pass. Same fixture file, corrected in place.
# ---------------------------------------------------------------------------

cat > "$CARD" <<'EOF'
---
name: fixture-role
---

# Fixture Role

## Workflow

1. Check the PR:
   CODE_REPO="$(source scripts/lib/repo-resolve.sh && _resolve_code_repo)"; gh pr view {pr_number} --repo "${CODE_REPO:?code plane unresolved}"
EOF

OUT2="$(bash "$GUARD" --root "$FIX" 2>&1)"
RC2=$?
echo "--- check 2: same statement, now pinned (real run) — exit $RC2"
printf '%s\n' "$OUT2" | sed 's/^/    /'

if [[ "$RC2" -ne 0 ]]; then
  echo "$NAME: FAIL — check 2: guard still fails after the statement was correctly pinned" >&2
  FAILED=1
fi

# ---------------------------------------------------------------------------
# Check 3 — a gh api repos/ statement correctly pinned to the Discussion
# plane must not fire (item 5's first non-firing case).
# ---------------------------------------------------------------------------

cat > "$CARD" <<'EOF'
---
name: fixture-role
---

# Fixture Role

## Workflow

1. Read a Discussion-plane label:
   gh api repos/autonomous-agent-7/fulcrumaxe/labels
EOF

OUT3="$(bash "$GUARD" --root "$FIX" 2>&1)"
RC3=$?
echo "--- check 3: gh api repos/ pinned to the Discussion plane (real run) — exit $RC3"
printf '%s\n' "$OUT3" | sed 's/^/    /'

if [[ "$RC3" -ne 0 ]]; then
  echo "$NAME: FAIL — check 3: guard fired on a gh api repos/ statement correctly pinned to the Discussion-plane literal" >&2
  FAILED=1
fi

# ---------------------------------------------------------------------------
# Check 4 — prose that merely mentions gh pr must not fire (item 5's second
# non-firing case): a markdown bullet describing a call another module makes,
# and an inline aside naming a forbidden verb.
# ---------------------------------------------------------------------------

cat > "$CARD" <<'EOF'
---
name: fixture-role
---

# Fixture Role

## Data Sources (read-only)

- gh pr list --state merged -- lead time computation (via release_manager)

## Tool Whitelist

You MUST NOT use `gh pr review` or `gh pr merge` — those are for Team Lead only.
EOF

OUT4="$(bash "$GUARD" --root "$FIX" 2>&1)"
RC4=$?
echo "--- check 4: prose mentioning gh pr, not a real statement (real run) — exit $RC4"
printf '%s\n' "$OUT4" | sed 's/^/    /'

if [[ "$RC4" -ne 0 ]]; then
  echo "$NAME: FAIL — check 4: guard fired on prose that merely mentions gh pr (a bullet and an inline aside), never an executed statement" >&2
  FAILED=1
fi

if [[ "$FAILED" -ne 0 ]]; then
  exit 1
fi

echo "$NAME: OK — unpinned gh pr statement fails and names file:line; pinning it passes; a correctly pinned gh api repos/ Discussion-plane call and gh-pr-mentioning prose never fire"
