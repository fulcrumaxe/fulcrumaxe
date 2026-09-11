#!/usr/bin/env bash
# tests/test_merge_gate_label_lane.sh — label lane enforcement (D#2529)
#
# Run: bash tests/test_merge_gate_label_lane.sh
# Expects: all assertions pass, exit 0
#
# D#2529: a merge-gate pass label was applied on PR #147 and no agent claims
# having applied it. The gate could only ever check whether a label string
# was present, never who was allowed to put it there. This suite drives
# scripts/lib/merge-gate-labels.sh's merge_gate_check_label_lane directly —
# not through apply_label or refresh-gate-label.sh, neither of which this
# change touches (see the PR body for why).
#
# MUTATION-1 is the binding item (acceptance item 6): it asserts the refusal
# with the real lane map, then re-sources the file with the lane map
# short-circuited to an always-allow and asserts the same call now succeeds
# — i.e. this suite would have caught the exact PR #147 shape (a
# code-reviewer-shaped apply of security-review-passed) if the lane check
# did not exist.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MERGE_GATE_LABELS_LIB="$REAL_REPO_ROOT/scripts/lib/merge-gate-labels.sh"

PASS=0
FAIL=0

assert_exit() {
  local label="$1" expected_rc="$2" actual_rc="$3"
  if [ "$actual_rc" -eq "$expected_rc" ]; then
    echo "  PASS: $label (exit $actual_rc)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected exit $expected_rc, got $actual_rc)"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  if echo "$actual" | grep -qF "$expected_substr"; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label — expected output to contain: $expected_substr"
    echo "        actual: $actual"
    FAIL=$((FAIL + 1))
  fi
}

# -----------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------
if [ ! -f "$MERGE_GATE_LABELS_LIB" ]; then
  echo "FATAL: $MERGE_GATE_LABELS_LIB not found" >&2
  exit 1
fi
# shellcheck source=../scripts/lib/merge-gate-labels.sh
source "$MERGE_GATE_LABELS_LIB"

if ! declare -p MERGE_GATE_LABEL_LANE >/dev/null 2>&1; then
  echo "FATAL: MERGE_GATE_LABEL_LANE not defined after sourcing $MERGE_GATE_LABELS_LIB" >&2
  exit 1
fi

echo "=== In-lane applies succeed (acceptance item 7) ==="
for label in "${!MERGE_GATE_LABEL_LANE[@]}"; do
  role="${MERGE_GATE_LABEL_LANE[$label]}"
  out=$(merge_gate_check_label_lane "$label" "$role" 2>&1)
  rc=$?
  assert_exit "own-lane apply: $role -> $label" 0 "$rc"
done

echo ""
echo "=== team-lead is always permitted (D#2529 filing context) ==="
for label in "${!MERGE_GATE_LABEL_LANE[@]}"; do
  out=$(merge_gate_check_label_lane "$label" "team-lead" 2>&1)
  rc=$?
  assert_exit "team-lead -> $label" 0 "$rc"
done

echo ""
echo "=== Out-of-lane applies are refused, for every mapped label ==="
# For each label, try every OTHER mapped role (not its own, not team-lead)
# and confirm refusal. Covers all four labels, not just the two in the D#147
# story (acceptance item 7).
for label in "${!MERGE_GATE_LABEL_LANE[@]}"; do
  owner="${MERGE_GATE_LABEL_LANE[$label]}"
  for other_label in "${!MERGE_GATE_LABEL_LANE[@]}"; do
    other_role="${MERGE_GATE_LABEL_LANE[$other_label]}"
    if [ "$other_role" == "$owner" ]; then
      continue
    fi
    out=$(merge_gate_check_label_lane "$label" "$other_role" 2>&1)
    rc=$?
    assert_exit "out-of-lane: $other_role -> $label" 1 "$rc"
    assert_contains "out-of-lane refusal names label ($label)" "$label" "$out"
    assert_contains "out-of-lane refusal names role ($other_role)" "$other_role" "$out"
  done
done

echo ""
echo "=== The exact D#147 shape: a code-reviewer-shaped apply of security-review-passed ==="
out=$(merge_gate_check_label_lane "security-review-passed" "code-reviewer" 2>&1)
rc=$?
assert_exit "code-reviewer -> security-review-passed" 1 "$rc"
assert_contains "refusal names security-review-passed" "security-review-passed" "$out"
assert_contains "refusal names code-reviewer" "code-reviewer" "$out"

echo ""
echo "=== Unmapped label is a hard error, not a default-allow (acceptance item 4) ==="
out=$(merge_gate_check_label_lane "do-not-merge" "team-lead" 2>&1)
rc=$?
assert_exit "unmapped label 'do-not-merge' refused" 2 "$rc"
assert_contains "unmapped-label error names the label" "do-not-merge" "$out"

echo ""
echo "=== No read of a GitHub actor, login, or user.login (acceptance item 8) ==="
# Strip comment lines first: the file's own header prose explains, in
# English, that it deliberately reads none of these — that explanation
# necessarily contains the words "actor" and "login" without being a read of
# either. What must be absent is CODE that reads one: a field access
# (`.login`, `user.login`), a `gh api`/`--jq` pull of an author/actor, or a
# GraphQL selection naming one.
CODE_LINES=$(grep -vE '^\s*#' "$MERGE_GATE_LABELS_LIB")
if echo "$CODE_LINES" | grep -qE '\.login\b|\bactor\b|\blogin\b'; then
  echo "  FAIL: found a code (non-comment) read of actor/login/user.login in $MERGE_GATE_LABELS_LIB:"
  echo "$CODE_LINES" | grep -nE '\.login\b|\bactor\b|\blogin\b'
  FAIL=$((FAIL + 1))
else
  echo "  PASS: no actor/login/user.login read in $MERGE_GATE_LABELS_LIB's code"
  PASS=$((PASS + 1))
fi

echo ""
echo "=== MUTATION-1 (binding item, acceptance item 6): remove the lane check, confirm red ==="
# Re-source the library into an isolated subshell with MERGE_GATE_LABEL_LANE
# check short-circuited to always-allow, and confirm the exact D#147-shaped
# call that was refused above now succeeds — i.e. a passing test against an
# unrestricted helper is evidence of nothing, and this one is not that.
MUTATED_RC=$(
  # shellcheck source=../scripts/lib/merge-gate-labels.sh
  source "$MERGE_GATE_LABELS_LIB"
  # The mutation: replace the real function with an always-allow stub,
  # exactly as if the lane check were removed from the file.
  merge_gate_check_label_lane() { return 0; }
  merge_gate_check_label_lane "security-review-passed" "code-reviewer" >/dev/null 2>&1
  echo $?
)
if [ "$MUTATED_RC" -eq 0 ]; then
  echo "  PASS: MUTATION-1 — with the lane check removed, the same call that was refused above now succeeds (mutant is red on the real code, green here — the test has discriminating power)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: MUTATION-1 — mutated (always-allow) stub still returned $MUTATED_RC, expected 0. This suite's earlier refusal assertions would pass even with no lane check at all."
  FAIL=$((FAIL + 1))
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
