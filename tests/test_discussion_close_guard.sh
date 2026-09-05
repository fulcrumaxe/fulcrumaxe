#!/usr/bin/env bash
# tests/test_discussion_close_guard.sh — decision-table tests for
# scripts/lib/discussion-close-guard.sh (D#2021).
#
# Covers Spec (Acceptance) items 1-5:
#   1. Unknown-count Discussion (D#1997's shape) is never closed.
#   2. Declared multi-PR Discussion holds until the last merge, closes on it.
#   3. Declared single-PR Discussion still closes.
#   4. Empty body never closes.
#   5. Prose vocabulary (Batch/Slice/heading/marker) can hold, never close.
#
# Tests 6-12 (D#2064) cover the 4th positional argument, spec_comments_text:
# planned_prs posted in a Spec comment (not the body) must reach the same
# decision, resolution is the MAXIMUM across body and comments (never
# "most recent"), the empty-body fail-closed check stays keyed on body
# alone, and an anchored quote/prose mention in a comment can't masquerade
# as a declaration.
#
# Run: bash tests/test_discussion_close_guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=scripts/lib/discussion-close-guard.sh
source "$REPO_ROOT/scripts/lib/discussion-close-guard.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$label"
  else
    fail "$label — expected '$expected', got '$actual'"
  fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass "$label"
  else
    fail "$label — expected to find '$needle' in: $haystack"
  fi
}

# ── Test 1: Unknown-count Discussion (D#1997's shape) is not closed ─────────
# No planned_prs field, no umbrella vocabulary. Under main today the
# equivalent path (IS_UMBRELLA == false) closes; this must hold instead.
echo "Test 1: D#1997's shape — no planned_prs, no vocabulary"
BODY1="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

## Intent
Just a normal single-PR-shaped Discussion body with no frontmatter at all."

discussion_close_decision "$BODY1"
assert_eq "test1: CLOSE_DECISION is unknown" "unknown" "$CLOSE_DECISION"
assert_contains "test1: CLOSE_REASON mentions planned_prs" "$CLOSE_REASON" "planned_prs"

# ── Test 2: Declared multi-PR Discussion holds until the last merge ─────────
echo "Test 2: planned_prs: 6 — holds at 2, closes at 6"
BODY2="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

---
planned_prs: 6
---

## Spec
Six planned PRs."

discussion_close_decision "$BODY2" 2
assert_eq "test2: holds at 2/6 merges" "hold" "$CLOSE_DECISION"

discussion_close_decision "$BODY2" 6
assert_eq "test2: closes at 6/6 merges" "close" "$CLOSE_DECISION"

discussion_close_decision "$BODY2" 5
assert_eq "test2: still holds at 5/6 merges" "hold" "$CLOSE_DECISION"

# ── Test 3: Declared single-PR Discussion still closes ───────────────────────
echo "Test 3: planned_prs: 1 — closes (proves this isn't just 'never close')"
BODY3="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

---
planned_prs: 1
---

## Spec
One planned PR."

discussion_close_decision "$BODY3" 0
assert_eq "test3: closes with planned_prs: 1" "close" "$CLOSE_DECISION"

discussion_close_decision "$BODY3" 99
assert_eq "test3: closes with planned_prs: 1 regardless of merge count" "close" "$CLOSE_DECISION"

# ── Test 4: Empty body never closes ──────────────────────────────────────────
echo "Test 4: empty body"
discussion_close_decision ""
assert_eq "test4: CLOSE_DECISION is unknown for empty body" "unknown" "$CLOSE_DECISION"

# ── Test 5: Prose vocabulary can hold but can never close ───────────────────
# Body with **Batch letter mentions (matches detect_umbrella's batch signal)
# and no planned_prs field. is_umbrella=true is passed as the caller would
# after running detect_umbrella. Must hold for every merge count 0..99 —
# there must be NO count at which this closes.
echo "Test 5: Batch-letter vocabulary, no planned_prs — hold for merge counts 0..99"
BODY5="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

**Batch A** — do the first thing.
**Batch B** — do the second thing."

ANY_CLOSED=false
for N in $(seq 0 99); do
  discussion_close_decision "$BODY5" "$N" "true"
  if [[ "$CLOSE_DECISION" == "close" ]]; then
    ANY_CLOSED=true
    fail "test5: closed at merge count $N (must never close on vocabulary alone)"
    break
  fi
done
if [[ "$ANY_CLOSED" == "false" ]]; then
  pass "test5: held for all merge counts 0..99, never closed"
fi

# Same body, but caller correctly reports is_umbrella=false (vocabulary check
# is the CALLER's job, not this file's) — falls through to the unknown branch.
discussion_close_decision "$BODY5" 0 "false"
assert_eq "test5b: without is_umbrella=true, falls through to unknown" "unknown" "$CLOSE_DECISION"

# ── Test 6: Comment-only planned_prs: 2, 1 merge -> hold, reason names comment
echo "Test 6: planned_prs: 2 in a Spec comment only, 1 merge — hold"
NOFIELD_BODY="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

## Intent
No frontmatter in the body at all — the Spec lives in a comment."

COMMENT_PRS2="## Spec

---
planned_prs: 2
---

Two planned PRs."

discussion_close_decision "$NOFIELD_BODY" 1 "false" "$COMMENT_PRS2"
assert_eq "test6: holds at 1/2 merges (comment-only)" "hold" "$CLOSE_DECISION"
assert_contains "test6: CLOSE_REASON names comment as the source" "$CLOSE_REASON" "comment"

# ── Test 7: Comment-only planned_prs: 2, 2 merges -> close ──────────────────
# Proves the fix is not just "always hold" — the comment-only path can also
# close once its declared count is reached.
echo "Test 7: planned_prs: 2 in a Spec comment only, 2 merges — close"
discussion_close_decision "$NOFIELD_BODY" 2 "false" "$COMMENT_PRS2"
assert_eq "test7: closes at 2/2 merges (comment-only)" "close" "$CLOSE_DECISION"

# ── Test 8: Comment-only planned_prs: 1, 0 merges -> close ──────────────────
echo "Test 8: planned_prs: 1 in a Spec comment only, 0 merges — close"
COMMENT_PRS1="## Spec

---
planned_prs: 1
---

One planned PR."

discussion_close_decision "$NOFIELD_BODY" 0 "false" "$COMMENT_PRS1"
assert_eq "test8: closes with comment-only planned_prs: 1" "close" "$CLOSE_DECISION"

# ── Test 9: Body says 1, comment says 2, 1 merge -> hold (maximum wins) ─────
# Pins the precedence decision: maximum across sources, never most-recent.
echo "Test 9: body planned_prs: 1, comment planned_prs: 2, 1 merge — hold (max wins)"
BODY_PRS1="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

---
planned_prs: 1
---

## Spec
Body (wrongly) says one."

discussion_close_decision "$BODY_PRS1" 1 "false" "$COMMENT_PRS2"
assert_eq "test9: holds — max(1, 2)=2, only 1 merge recorded" "hold" "$CLOSE_DECISION"
assert_contains "test9: CLOSE_REASON names comment as the winning source" "$CLOSE_REASON" "comment"

# ── Test 9b: Body says 5, comment says 1, 1 merge -> hold (maximum wins) ───
# Code review catch: test 9 alone (body=1, comment=2) can't distinguish
# "maximum" from "comment always wins when present" — both pick 2. This is
# the case where the body value is HIGHER than the comment's, so the two
# resolution rules disagree: maximum picks 5 (hold, since 1 < 5 merges),
# "prefer comment" would pick 1 (close, since 1 >= 1 merge). Without this
# case a regression to "comment always wins" ships invisibly — exactly the
# D#2021-shaped bug the maximum rule exists to prevent, just triggered from
# the body side instead of the comment side.
echo "Test 9b: body planned_prs: 5, comment planned_prs: 1, 1 merge — hold (max wins, body side)"
BODY_PRS5="<!-- STATUS:SPEC_READY SINCE:2026-08-20T00:00:00Z -->

---
planned_prs: 5
---

## Spec
Five planned PRs, declared correctly in the body."

discussion_close_decision "$BODY_PRS5" 1 "false" "$COMMENT_PRS1"
assert_eq "test9b: holds — max(5, 1)=5, only 1 merge recorded" "hold" "$CLOSE_DECISION"
assert_contains "test9b: CLOSE_REASON names body as the winning source" "$CLOSE_REASON" "source: body"

# ── Test 10: No planned_prs anywhere, 1 merge -> unknown, holds open ────────
# The fail-closed guarantee — the most important case here.
echo "Test 10: no planned_prs in body or comments, 1 merge — unknown"
discussion_close_decision "$NOFIELD_BODY" 1 "false" "no planned_prs field here either."
assert_eq "test10: unknown with no planned_prs anywhere" "unknown" "$CLOSE_DECISION"

# ── Test 11: Empty body, comment carries planned_prs: 1 -> unknown ─────────
# The empty-body fail-closed check stays keyed on $body alone; comments must
# never satisfy it.
echo "Test 11: empty body, comment carries planned_prs: 1 — still unknown"
discussion_close_decision "" 0 "false" "$COMMENT_PRS1"
assert_eq "test11: empty body holds regardless of comment content" "unknown" "$CLOSE_DECISION"

# ── Test 12: A quoted/prose mention in a comment can't masquerade ───────────
# as a declaration — anchored to line start, same as the body check.
echo "Test 12: quoted '> planned_prs: 5' and prose mention in a comment — unknown"
QUOTED_COMMENT="> planned_prs: 5

Someone quoted the planned_prs field above while discussing the Spec."

discussion_close_decision "$NOFIELD_BODY" 0 "false" "$QUOTED_COMMENT"
assert_eq "test12: quoted/prose mention does not count as a declaration" "unknown" "$CLOSE_DECISION"

# ── Test 13: planned_prs: 0 is a deliberate hold-open, not close-on-first-merge
# (D#2272). Before this fix, 0 fell through to the merged_count >= planned_prs
# check, which is trivially true at merged_count=0 — the most aggressive value
# in the field's range, not the safest. Must hold at merged counts 0, 1 AND 3:
# there is no merge count at which a bare "0" should ever close.
echo "Test 13: planned_prs: 0 — deliberate hold-open, holds at merged 0, 1 and 3"
BODY_PRS0="<!-- STATUS:SPEC_READY SINCE:2026-09-03T00:00:00Z -->

---
planned_prs: 0
---

## Spec
Operational completion, not a merged PR."

discussion_close_decision "$BODY_PRS0" 0
assert_eq "test13: planned_prs:0 holds at merged=0 (not close-on-zero-merges)" "hold" "$CLOSE_DECISION"
assert_contains "test13: CLOSE_REASON names the deliberate hold-open declaration" "$CLOSE_REASON" "deliberate hold-open"

discussion_close_decision "$BODY_PRS0" 1
assert_eq "test13: planned_prs:0 holds at merged=1" "hold" "$CLOSE_DECISION"

discussion_close_decision "$BODY_PRS0" 3
assert_eq "test13: planned_prs:0 holds at merged=3 (not close-on-first-merge)" "hold" "$CLOSE_DECISION"

# ── Test 13b: planned_prs: 0 in a comment only — same hold-open semantics ───
echo "Test 13b: planned_prs: 0 declared in a Spec comment only — holds"
COMMENT_PRS0="## Spec

---
planned_prs: 0
---

Operational completion, declared in a comment."

discussion_close_decision "$NOFIELD_BODY" 5 "false" "$COMMENT_PRS0"
assert_eq "test13b: comment-only planned_prs:0 holds regardless of merge count" "hold" "$CLOSE_DECISION"
assert_contains "test13b: CLOSE_REASON names comment as the source" "$CLOSE_REASON" "source: comment"

# ── Test 13c: planned_prs: 0 can never win a max against a real declared count
# — a 0 is a floor, not a ceiling, so a stray 0 quoted elsewhere cannot hold a
# real Spec's non-zero count open (nor can it undercut a real close).
echo "Test 13c: body planned_prs: 1, comment planned_prs: 0 — max wins, closes"
discussion_close_decision "$BODY_PRS1" 0 "false" "$COMMENT_PRS0"
assert_eq "test13c: max(1, 0)=1 wins — closes despite the stray 0" "close" "$CLOSE_DECISION"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
