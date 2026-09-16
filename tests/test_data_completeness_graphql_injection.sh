#!/usr/bin/env bash
# tests/test_data_completeness_graphql_injection.sh — hermetic unit tests for
# scripts/data-completeness-check.sh's owner/name validation (D#2598 fix-round
# 2 item 3).
#
# scripts/data-completeness-check.sh's Discussion-count cross-check used to
# paste the resolved Discussion-plane owner/name directly into a GraphQL
# query STRING, the same injection shape as a hand-built SQL string, sourced
# from .autonomous-team/config.json (user-editable, not a constant). This
# suite extracts the real _dcc_is_valid_github_owner / _dcc_is_valid_github_name
# functions from the script (not a hand-duplicated copy, so this test cannot
# silently drift from the implementation) and drives them directly.
#
# Run: bash tests/test_data_completeness_graphql_injection.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/data-completeness-check.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "=== data-completeness-check.sh GraphQL injection guard tests ==="

# Extract just the two validator functions — never the whole script (which
# runs a live API sweep top-to-bottom the moment it's sourced). Both are
# written as single-line function bodies, so a plain grep for the whole
# definition line is enough; falls back to a multi-line range in case a
# future edit reformats them to a multi-line body.
OWNER_FN_SRC=$(grep '^_dcc_is_valid_github_owner() {' "$SCRIPT")
[[ -z "$OWNER_FN_SRC" ]] && OWNER_FN_SRC=$(sed -n '/^_dcc_is_valid_github_owner() {/,/^}/p' "$SCRIPT")
NAME_FN_SRC=$(grep '^_dcc_is_valid_github_name() ' "$SCRIPT")
[[ -z "$NAME_FN_SRC" ]] && NAME_FN_SRC=$(sed -n '/^_dcc_is_valid_github_name() {/,/^}/p' "$SCRIPT")

if [[ -z "$OWNER_FN_SRC" || -z "$NAME_FN_SRC" ]]; then
  fail "could not extract _dcc_is_valid_github_owner/_dcc_is_valid_github_name from $SCRIPT — have they been renamed?"
  echo ""
  echo "=== $PASS passed, $FAIL failed ==="
  exit 1
fi
eval "$OWNER_FN_SRC"
eval "$NAME_FN_SRC"

# ── Test 1: real, valid owner/name — accepted ──────────────────────────────
echo ""
echo "--- Test 1: valid owner/name accepted ---"
if _dcc_is_valid_github_owner "fulcrumaxe" && _dcc_is_valid_github_name "fulcrumaxe"; then
  pass "valid owner 'fulcrumaxe' and name 'fulcrumaxe' both accepted"
else
  fail "valid owner/name were rejected"
fi
if _dcc_is_valid_github_owner "autonomous-agent-7"; then
  pass "valid hyphenated owner 'autonomous-agent-7' accepted"
else
  fail "valid hyphenated owner was rejected"
fi

# ── Test 2: GraphQL/query-injection-shaped owner — rejected ────────────────
echo ""
echo "--- Test 2: query-injection-shaped owner rejected ---"
INJECT_OWNER='fulcrumaxe", extra: injected('
if _dcc_is_valid_github_owner "$INJECT_OWNER"; then
  fail "injection-shaped owner was accepted: $INJECT_OWNER"
else
  pass "injection-shaped owner correctly rejected"
fi

# ── Test 3: injection-shaped name (closing quote + brace) — rejected ───────
echo ""
echo "--- Test 3: injection-shaped name rejected ---"
INJECT_NAME='fulcrumaxe") { adminSecrets'
if _dcc_is_valid_github_name "$INJECT_NAME"; then
  fail "injection-shaped name was accepted: $INJECT_NAME"
else
  pass "injection-shaped name correctly rejected"
fi

# ── Test 4: empty value — rejected (not silently treated as valid) ─────────
echo ""
echo "--- Test 4: empty owner/name rejected ---"
if _dcc_is_valid_github_owner ""; then
  fail "empty owner was accepted"
else
  pass "empty owner correctly rejected"
fi
if _dcc_is_valid_github_name ""; then
  fail "empty name was accepted"
else
  pass "empty name correctly rejected"
fi

# ── Test 5: whitespace-containing value — rejected ──────────────────────────
echo ""
echo "--- Test 5: whitespace in owner/name rejected ---"
if _dcc_is_valid_github_owner "fulcrumaxe fulcrumaxe"; then
  fail "owner with a space was accepted"
else
  pass "owner with a space correctly rejected"
fi

# ── Test 6: valid repo name with dot/underscore — accepted ─────────────────
echo ""
echo "--- Test 6: valid repo name with dot and underscore accepted ---"
if _dcc_is_valid_github_name "my_repo.name-2"; then
  pass "valid repo name with '.', '_', '-' accepted"
else
  fail "valid repo name with '.', '_', '-' was rejected"
fi

# ── Test 7: script no longer string-builds the query with owner/name ───────
echo ""
echo "--- Test 7: query is parameterized, not string-built with the resolved values ---"
if grep -qE -- 'owner:"'\''"\$_DCC_DISC_OWNER' "$SCRIPT" 2>/dev/null; then
  fail "script still string-interpolates \$_DCC_DISC_OWNER directly into the query"
elif grep -qF -- '-F owner="$_DCC_DISC_OWNER" -F name="$_DCC_DISC_NAME"' "$SCRIPT"; then
  pass "script passes owner/name as GraphQL variables (-F), not string-built into the query"
else
  fail "expected '-F owner=\"\$_DCC_DISC_OWNER\" -F name=\"\$_DCC_DISC_NAME\"' in $SCRIPT, not found"
fi

echo ""
echo "=== $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
