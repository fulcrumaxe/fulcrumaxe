#!/usr/bin/env bash
# tests/test_worktree_claims.sh — Unit tests for scripts/lib/worktree-claims.sh (D#1819)
#
# Run: bash tests/test_worktree_claims.sh
# Expects: all assertions pass, exit 0
#
# Follows this repo's existing plain-bash test-script convention (see
# tests/test_ci_status_check.sh, tests/test_two_gate_check.sh) — no bats
# runner is wired into the actual day-to-day test flow.
#
# Builds a real fixture git repo (bare "origin" + a main checkout + linked
# worktrees) under a temp dir, rather than depending on the live worktree
# population in this repo — that population changes as other executors run
# (D#1819 Spec item 6 explicitly calls this out). Uses
# WTC_MERGED_HEADS_OVERRIDE (mirrors scripts/lib/ci-status-check.sh's mock-
# var convention) so no test makes a real `gh` call.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WTC_LIB="$REAL_REPO_ROOT/scripts/lib/worktree-claims.sh"

PASS=0
FAIL=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label"; echo "        expected: $expected"; echo "        actual:   $actual"
    FAIL=$((FAIL + 1))
  fi
}

assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  if printf '%s' "$actual" | grep -qF "$expected_substr"; then
    echo "  PASS: $label"; PASS=$((PASS + 1));
  else
    echo "  FAIL: $label"; echo "        expected to contain: $expected_substr"; echo "        actual: $actual"
    FAIL=$((FAIL + 1))
  fi
}

assert_empty() {
  local label="$1" actual="$2"
  if [ -z "$actual" ]; then
    echo "  PASS: $label (empty)"; PASS=$((PASS + 1));
  else
    echo "  FAIL: $label — expected empty, got: $actual"; FAIL=$((FAIL + 1))
  fi
}

# -----------------------------------------------------------------------
# Fixture repo setup — bare origin + main checkout + linked worktrees.
# -----------------------------------------------------------------------
FIXTURE_ROOT="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

ORIGIN="$FIXTURE_ROOT/origin.git"
MAIN="$FIXTURE_ROOT/main"

git init --quiet --bare "$ORIGIN"
git clone --quiet "$ORIGIN" "$MAIN"

_commit() {
  # _commit <repo-path> <message> [date]
  local repo="$1" msg="$2" date="${3:-}"
  if [ -n "$date" ]; then
    GIT_AUTHOR_DATE="$date" GIT_COMMITTER_DATE="$date" git -C "$repo" commit --quiet -m "$msg"
  else
    git -C "$repo" commit --quiet -m "$msg"
  fi
}

echo "seed" > "$MAIN/README.md"
git -C "$MAIN" add README.md
_commit "$MAIN" "seed"
git -C "$MAIN" branch -M main
git -C "$MAIN" push --quiet -u origin main

echo ""
echo "=== WC-1: squash-merged branch under threshold does not contribute a claim (AC6) ==="
# Branch off main, make a real commit (this is the "PR" content).
git -C "$MAIN" worktree add --quiet -b feature-merged "$FIXTURE_ROOT/wt-merged" main
echo "feature content" > "$FIXTURE_ROOT/wt-merged/feature.txt"
git -C "$FIXTURE_ROOT/wt-merged" add feature.txt
_commit "$FIXTURE_ROOT/wt-merged" "add feature"

# Simulate squash-merge: main gets a NEW commit with the squashed result —
# different commit object than the branch's own commit, exactly the shape
# that defeats git branch --merged / cherry equivalence for a multi-commit
# branch, and precisely why D#1819 requires GitHub as the merge authority.
echo "feature content" > "$MAIN/feature.txt"
git -C "$MAIN" add feature.txt
_commit "$MAIN" "squash: add feature (#1)"
echo "unrelated main change" > "$MAIN/other.txt"
git -C "$MAIN" add other.txt
_commit "$MAIN" "unrelated main change"
git -C "$MAIN" push --quiet origin main

BEHIND=$(git -C "$FIXTURE_ROOT/wt-merged" rev-list --count HEAD..origin/main)
if [ "$BEHIND" -gt 0 ] && [ "$BEHIND" -lt 20 ]; then
  echo "  (fixture behind=$BEHIND, under threshold — as required by AC6)"
else
  echo "  FAIL: fixture setup — expected 0 < behind < 20, got $BEHIND"; FAIL=$((FAIL + 1))
fi

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="feature-merged"
  wtc_classify "$FIXTURE_ROOT/wt-merged" "feature-merged"
  echo "CLASS=$WTC_CLASS"
  echo "CLAIMS=$(wtc_claimed_files "$FIXTURE_ROOT/wt-merged" "feature-merged" "wt-merged" 2>/dev/null)"
) > "$FIXTURE_ROOT/wc1.out"
WC1_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wc1.out" | cut -d= -f2)
WC1_CLAIMS=$(grep '^CLAIMS=' "$FIXTURE_ROOT/wc1.out" | cut -d= -f2-)
assert_eq "WC-1: classified MERGED despite being under the commits-behind threshold" "MERGED" "$WC1_CLASS"
assert_empty "WC-1: MERGED worktree with no dirty files contributes zero claims" "$WC1_CLAIMS"

echo ""
echo "=== WC-2: MERGED worktree with a dirty tracked file still claims it + WARNs (AC7) ==="
echo "uncommitted edit" >> "$FIXTURE_ROOT/wt-merged/feature.txt"
(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="feature-merged"
  wtc_claimed_files "$FIXTURE_ROOT/wt-merged" "feature-merged" "wt-merged"
) > "$FIXTURE_ROOT/wc2.out" 2> "$FIXTURE_ROOT/wc2.err"
assert_contains "WC-2: dirty file still claimed on stdout" "feature.txt WT:wt-merged" "$(cat "$FIXTURE_ROOT/wc2.out")"
assert_contains "WC-2: WARN naming the worktree on stderr" "WARN: worktree wt-merged is classified MERGED" "$(cat "$FIXTURE_ROOT/wc2.err")"
assert_contains "WC-2: WARN names the specific dirty file" "feature.txt" "$(cat "$FIXTURE_ROOT/wc2.err")"

echo ""
echo "=== WC-3: STALE by wall-clock but 0 commits behind is skipped (AC7) ==="
# Worktree branched right at the current origin/main tip -> behind=0.
git -C "$MAIN" worktree add --quiet -b feature-wallclock-stale "$FIXTURE_ROOT/wt-wallclock" main
BEHIND_WC3=$(git -C "$FIXTURE_ROOT/wt-wallclock" rev-list --count HEAD..origin/main)
assert_eq "WC-3 fixture: 0 commits behind" "0" "$BEHIND_WC3"

# Both signals wtc_age_days maxes over must look old: the tip commit's
# committer date AND the worktree directory's own mtime (git worktree add
# always stamps the directory as freshly touched "now", which is exactly
# what the "max" conservative rule is designed to catch and not be fooled
# by — so we age both). This is what a multi-week-abandoned real worktree
# looks like: both the last commit AND the directory are old.
#
# Add a fresh commit dated old rather than amending the shared tip commit —
# amending would create a sibling of origin/main's tip (same parent,
# different SHA) and desync "behind" away from 0. A new commit ON TOP of
# origin/main's tip keeps it an ancestor, so behind stays 0.
OLD_EPOCH=$(( $(date +%s) - (20 * 86400) ))
echo "old work" > "$FIXTURE_ROOT/wt-wallclock/aged.txt"
git -C "$FIXTURE_ROOT/wt-wallclock" add aged.txt
_commit "$FIXTURE_ROOT/wt-wallclock" "aged commit" "@$OLD_EPOCH"
touch -d "@$OLD_EPOCH" "$FIXTURE_ROOT/wt-wallclock"

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-wallclock" "feature-wallclock-stale"
  echo "CLASS=$WTC_CLASS"
  echo "REASON=$WTC_REASON"
  echo "BEHIND=$WTC_BEHIND"
  echo "AGE=$WTC_AGE_DAYS"
) > "$FIXTURE_ROOT/wc3b.out"
WC3_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wc3b.out" | cut -d= -f2)
WC3_BEHIND=$(grep '^BEHIND=' "$FIXTURE_ROOT/wc3b.out" | cut -d= -f2)
WC3_AGE=$(grep '^AGE=' "$FIXTURE_ROOT/wc3b.out" | cut -d= -f2)
assert_eq "WC-3: still 0 commits behind" "0" "$WC3_BEHIND"
assert_eq "WC-3: classified STALE purely by wall-clock age" "STALE" "$WC3_CLASS"
if [ "${WC3_AGE:-0}" -ge 14 ]; then
  echo "  PASS: WC-3: age_days ($WC3_AGE) exceeds the 14-day default threshold"; PASS=$((PASS + 1))
else
  echo "  FAIL: WC-3: age_days ($WC3_AGE) does not exceed threshold"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-4: gh-unavailable degrade — no worktree is classified MERGED (constraint) ==="
(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_is_merged_branch "feature-merged"
  echo "RC=$?"
) > "$FIXTURE_ROOT/wc4.out"
assert_contains "WC-4: wtc_is_merged_branch returns non-zero (false) when gh is unavailable" "RC=1" "$(cat "$FIXTURE_ROOT/wc4.out")"

echo ""
echo "=== WC-5: genuinely active worktree claims its own three-dot diff (sanity) ==="
git -C "$MAIN" worktree add --quiet -b feature-active "$FIXTURE_ROOT/wt-active" main
echo "active work" > "$FIXTURE_ROOT/wt-active/active.txt"
git -C "$FIXTURE_ROOT/wt-active" add active.txt
_commit "$FIXTURE_ROOT/wt-active" "active work in progress"
(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-active" "feature-active"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-active" "feature-active" "wt-active"
) > "$FIXTURE_ROOT/wc5.out" 2>/dev/null
assert_contains "WC-5: classified ACTIVE" "CLASS=ACTIVE" "$(cat "$FIXTURE_ROOT/wc5.out")"
assert_contains "WC-5: claims its own new file" "active.txt WT:wt-active" "$(cat "$FIXTURE_ROOT/wc5.out")"

echo ""
echo "=== WC-6: CLI subcommands are runnable standalone (contract) ==="
if [ -x "$WTC_LIB" ] || [ -f "$WTC_LIB" ]; then
  echo "  PASS: lib file exists"; PASS=$((PASS + 1))
else
  echo "  FAIL: lib file missing"; FAIL=$((FAIL + 1))
fi
for fn in wtc_classify wtc_claimed_files wtc_cmd_list wtc_cmd_census wtc_cmd_explain; do
  if grep -q "^${fn}()" "$WTC_LIB"; then
    echo "  PASS: defines $fn"; PASS=$((PASS + 1))
  else
    echo "  FAIL: does not define $fn"; FAIL=$((FAIL + 1))
  fi
done
USAGE_RC=0
bash "$WTC_LIB" bogus-subcommand >/dev/null 2>&1 || USAGE_RC=$?
assert_eq "WC-6: unknown subcommand exits non-zero" "1" "$USAGE_RC"

echo ""
echo "=== WC-7: spawn-agent.sh and sweep-stale-worktrees.sh consume the shared lib ==="
if grep -qE 'source.*worktree-claims\.sh|\. .*worktree-claims\.sh' "$REAL_REPO_ROOT/scripts/spawn-agent.sh"; then
  echo "  PASS: spawn-agent.sh sources worktree-claims.sh"; PASS=$((PASS + 1))
else
  echo "  FAIL: spawn-agent.sh does not source worktree-claims.sh"; FAIL=$((FAIL + 1))
fi
if grep -qE 'source.*worktree-claims\.sh|\. .*worktree-claims\.sh' "$REAL_REPO_ROOT/scripts/sweep-stale-worktrees.sh"; then
  echo "  PASS: sweep-stale-worktrees.sh sources worktree-claims.sh"; PASS=$((PASS + 1))
else
  echo "  FAIL: sweep-stale-worktrees.sh does not source worktree-claims.sh"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-8: wtc_match_claim anchors on the exact path field (D#1914) ==="
(
  source "$WTC_LIB"
  echo "archive/snap/scripts/lib/working-principles.sh WT:x" | wtc_match_claim "scripts/lib/working-principles.sh"
) > "$FIXTURE_ROOT/wc8a.out"
assert_empty "WC-8: archive-suffix claim line does not match the real path" "$(cat "$FIXTURE_ROOT/wc8a.out")"

(
  source "$WTC_LIB"
  echo "scripts/lib/working-principles.sh WT:x" | wtc_match_claim "scripts/lib/working-principles.sh"
) > "$FIXTURE_ROOT/wc8b.out"
assert_eq "WC-8: exact path match returns the claim line" "scripts/lib/working-principles.sh WT:x" "$(cat "$FIXTURE_ROOT/wc8b.out")"

(
  source "$WTC_LIB"
  echo "archive/snap/scripts/lib/working-principles.sh PR#12" | wtc_match_claim "scripts/lib/working-principles.sh"
) > "$FIXTURE_ROOT/wc8c.out"
assert_empty "WC-8: archive-suffix claim line does not match under a PR# ref either" "$(cat "$FIXTURE_ROOT/wc8c.out")"

(
  source "$WTC_LIB"
  echo "scripts/lib/working-principles.sh PR#12" | wtc_match_claim "scripts/lib/working-principles.sh"
) > "$FIXTURE_ROOT/wc8d.out"
assert_eq "WC-8: exact path match under a PR# ref returns the claim line" "scripts/lib/working-principles.sh PR#12" "$(cat "$FIXTURE_ROOT/wc8d.out")"

echo ""
echo "=== WC-9: a rename claims BOTH paths as separate lines, no ' -> ' (D#1914) ==="
git -C "$MAIN" worktree add --quiet -b feature-rename "$FIXTURE_ROOT/wt-rename" main
mkdir -p "$FIXTURE_ROOT/wt-rename/a"
echo "content" > "$FIXTURE_ROOT/wt-rename/a/b.sh"
git -C "$FIXTURE_ROOT/wt-rename" add a/b.sh
_commit "$FIXTURE_ROOT/wt-rename" "add a/b.sh"
mkdir -p "$FIXTURE_ROOT/wt-rename/c"
git -C "$FIXTURE_ROOT/wt-rename" mv a/b.sh c/d.sh

(
  source "$WTC_LIB"
  wtc_dirty_tracked_files "$FIXTURE_ROOT/wt-rename"
) > "$FIXTURE_ROOT/wc9-dirty.out"
assert_contains "WC-9: dirty-tracked output contains bare new path" "c/d.sh" "$(cat "$FIXTURE_ROOT/wc9-dirty.out")"
assert_contains "WC-9: dirty-tracked output contains bare old path" "a/b.sh" "$(cat "$FIXTURE_ROOT/wc9-dirty.out")"
if grep -qF ' -> ' "$FIXTURE_ROOT/wc9-dirty.out"; then
  echo "  FAIL: WC-9: dirty-tracked output still contains ' -> ' arrow form"; FAIL=$((FAIL + 1))
else
  echo "  PASS: WC-9: dirty-tracked output has no ' -> ' arrow form"; PASS=$((PASS + 1))
fi

# wtc_claimed_files wraps dirty-tracked lines with " WT:<id>" — confirm both
# bare paths surface there too (the ACTIVE-classification claim path a real
# worktree with uncommitted changes takes), and that WTC_CLAIMED_FILES
# (D#1914's motivating input shape) never contains the arrow form either.
(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-rename" "feature-rename"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-rename" "feature-rename" "wt-rename"
) > "$FIXTURE_ROOT/wc9-claimed.out" 2>/dev/null
assert_contains "WC-9: claimed-files contains bare new path with WT ref" "c/d.sh WT:wt-rename" "$(cat "$FIXTURE_ROOT/wc9-claimed.out")"
assert_contains "WC-9: claimed-files contains bare old path with WT ref" "a/b.sh WT:wt-rename" "$(cat "$FIXTURE_ROOT/wc9-claimed.out")"
if grep -qF ' -> ' "$FIXTURE_ROOT/wc9-claimed.out"; then
  echo "  FAIL: WC-9: claimed-files output still contains ' -> ' arrow form"; FAIL=$((FAIL + 1))
else
  echo "  PASS: WC-9: claimed-files output has no ' -> ' arrow form"; PASS=$((PASS + 1))
fi

echo ""
echo "=== WC-10: wtc_match_claim finds the rename's new path in claimed-files output (D#1914) ==="
(
  source "$WTC_LIB"
  wtc_match_claim "c/d.sh" < "$FIXTURE_ROOT/wc9-claimed.out"
) > "$FIXTURE_ROOT/wc10.out"
assert_contains "WC-10: wtc_match_claim finds the renamed-to path" "c/d.sh WT:wt-rename" "$(cat "$FIXTURE_ROOT/wc10.out")"

echo ""
echo "=== WC-11: a claimed path containing a space is matched exactly, not truncated at the first space (D#1914) ==="
(
  source "$WTC_LIB"
  printf '%s\n' "archive/legacy-lane-2026-08-17/.lane/design/Meta Aesthetics/README.md WT:x" | \
    wtc_match_claim "archive/legacy-lane-2026-08-17/.lane/design/Meta Aesthetics/README.md"
) > "$FIXTURE_ROOT/wc11.out"
assert_eq "WC-11: space-containing path matches exactly" \
  "archive/legacy-lane-2026-08-17/.lane/design/Meta Aesthetics/README.md WT:x" "$(cat "$FIXTURE_ROOT/wc11.out")"

(
  source "$WTC_LIB"
  printf '%s\n' "archive/legacy-lane-2026-08-17/.lane/design/Meta Aesthetics/README.md WT:x" | \
    wtc_match_claim "archive/legacy-lane-2026-08-17/.lane/design/Meta"
) > "$FIXTURE_ROOT/wc11b.out"
assert_empty "WC-11: a touchpoint truncated at the first space does not falsely match" "$(cat "$FIXTURE_ROOT/wc11b.out")"

echo ""
echo "=== WC-12: only the first matching claim line is printed (preserves old head -1 semantics) ==="
(
  source "$WTC_LIB"
  printf '%s\n' "shared/file.sh WT:wt-a" "shared/file.sh WT:wt-b" | wtc_match_claim "shared/file.sh"
) > "$FIXTURE_ROOT/wc12.out"
WC12_LINES=$(grep -c . "$FIXTURE_ROOT/wc12.out" || true)
assert_eq "WC-12: exactly one line printed" "1" "$WC12_LINES"
assert_contains "WC-12: the first claim wins" "shared/file.sh WT:wt-a" "$(cat "$FIXTURE_ROOT/wc12.out")"

echo ""
echo "=== WC-13: wtc_match_claim prints nothing for an unclaimed path (sanity) ==="
(
  source "$WTC_LIB"
  printf '%s\n' "some/other/file.sh WT:wt-a" | wtc_match_claim "this/path/does/not/exist.sh"
) > "$FIXTURE_ROOT/wc13.out"
assert_empty "WC-13: no match on an unclaimed path" "$(cat "$FIXTURE_ROOT/wc13.out")"

echo ""
echo "=== WC-14: spawn-agent.sh's 0c gate calls wtc_match_claim, not unanchored grep -F (D#1914) ==="
if grep -qF 'grep -F "$_tp "' "$REAL_REPO_ROOT/scripts/spawn-agent.sh"; then
  echo "  FAIL: unanchored grep -F \"\$_tp \" still present in spawn-agent.sh"; FAIL=$((FAIL + 1))
else
  echo "  PASS: unanchored grep -F \"\$_tp \" removed from spawn-agent.sh"; PASS=$((PASS + 1))
fi
if grep -qE 'wtc_match_claim "\$_tp"' "$REAL_REPO_ROOT/scripts/spawn-agent.sh"; then
  echo "  PASS: spawn-agent.sh's 0c gate calls wtc_match_claim"; PASS=$((PASS + 1))
else
  echo "  FAIL: spawn-agent.sh's 0c gate does not call wtc_match_claim"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-15: a MODIFIED (non-rename) file whose own path contains ' -> ' is not split (D#1914 review fix) ==="
# Regression pin for the reviewer's finding: the arrow split in
# wtc_dirty_tracked_files must be gated on R/C status codes. Before the fix,
# a plain M-status line whose path happened to contain the literal
# substring " -> " would get sliced into two junk paths and the real path
# would be lost.
git -C "$MAIN" worktree add --quiet -b feature-arrow "$FIXTURE_ROOT/wt-arrow" main
mkdir -p "$FIXTURE_ROOT/wt-arrow/a"
echo "x" > "$FIXTURE_ROOT/wt-arrow/a/arrow -> weird.sh"
git -C "$FIXTURE_ROOT/wt-arrow" add "a/arrow -> weird.sh"
_commit "$FIXTURE_ROOT/wt-arrow" "add arrow-named file"
echo "y" >> "$FIXTURE_ROOT/wt-arrow/a/arrow -> weird.sh"

RAW_PORCELAIN=$(git -C "$FIXTURE_ROOT/wt-arrow" status --porcelain)
assert_contains "WC-15 fixture: porcelain reports the arrow-named file as Modified" "arrow -> weird.sh" "$RAW_PORCELAIN"

(
  source "$WTC_LIB"
  wtc_dirty_tracked_files "$FIXTURE_ROOT/wt-arrow"
) > "$FIXTURE_ROOT/wc15.out"
WC15_LINES=$(grep -c . "$FIXTURE_ROOT/wc15.out" || true)
assert_eq "WC-15: exactly one claim line emitted (not split into two junk paths)" "1" "$WC15_LINES"
assert_contains "WC-15: the emitted line still names the real file" "weird.sh" "$(cat "$FIXTURE_ROOT/wc15.out")"

echo ""
echo "=== D#1951 quoting fixtures: seed space / non-ASCII / rename-source paths on main ==="
# Seeded on MAIN (not on the worktree branch) on purpose. wtc_claimed_files
# on an ACTIVE worktree emits the three-dot diff half AND the dirty half; a
# file committed on the worktree's own branch would legitimately appear in
# both, and the "exactly one claim line" assertions below are specifically
# about the dirty half not double-spelling a path. Seeding on main keeps the
# three-dot diff empty so each count isolates the thing under test.
CAFE="$(printf 'caf\303\251.txt')"   # café.txt, built via printf so this
                                     # test file itself stays pure ASCII
mkdir -p "$MAIN/a/Meta Aesthetics" "$MAIN/Meta Aesthetics"
echo "seed" > "$MAIN/a/Meta Aesthetics/README.md"
echo "seed" > "$MAIN/$CAFE"
echo "seed" > "$MAIN/plain.txt"
# Root-level, deliberately: WC-20 renames FROM this path, and the mangling it
# pins only fires when the bare old-path record's first two bytes match
# /[MADRC]/. "Meta Aesthetics/DESIGN.md" starts "Me" (matches, via M); the
# nested "a/Meta..." above starts "a/" (does not match, so it would be
# silently dropped rather than mangled — a weaker signal).
echo "seed" > "$MAIN/Meta Aesthetics/DESIGN.md"
git -C "$MAIN" add "a/Meta Aesthetics/README.md" "Meta Aesthetics/DESIGN.md" "$CAFE" plain.txt
_commit "$MAIN" "seed quoting fixtures"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-quoting "$FIXTURE_ROOT/wt-quoting" main
echo "dirty" >> "$FIXTURE_ROOT/wt-quoting/a/Meta Aesthetics/README.md"
echo "dirty" >> "$FIXTURE_ROOT/wt-quoting/$CAFE"
echo "noise" > "$FIXTURE_ROOT/wt-quoting/untracked-noise.txt"

# Pin that the defect this change fixes is real on this host: the DEFAULT
# (non -z) porcelain and diff forms both C-quote at least one of these paths.
# If a future git stops quoting, these two assertions fail loudly rather than
# letting the regression tests below silently stop testing anything.
RAW_PORCELAIN_Q=$(git -C "$FIXTURE_ROOT/wt-quoting" status --porcelain)
assert_contains "D#1951 fixture: default porcelain C-quotes the space path" \
  '"a/Meta Aesthetics/README.md"' "$RAW_PORCELAIN_Q"
RAW_DIFF_Q=$(git -C "$FIXTURE_ROOT/wt-quoting" diff --name-only HEAD)
assert_contains "D#1951 fixture: default diff --name-only C-quotes the non-ASCII path" \
  'caf\303\251.txt' "$RAW_DIFF_Q"

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_claimed_files "$FIXTURE_ROOT/wt-quoting" "feature-quoting" "wt-quoting"
) > "$FIXTURE_ROOT/wc16.out" 2>/dev/null

echo ""
echo "=== WC-16: a dirty path containing a space claims once, bare, and matches (D#1951 AC3) ==="
WC16_COUNT=$(grep -cF 'a/Meta Aesthetics/README.md WT:wt-quoting' "$FIXTURE_ROOT/wc16.out" || true)
assert_eq "WC-16: exactly one claim line names the space-containing path" "1" "$WC16_COUNT"
WC16_QUOTED=$(grep -c '"' "$FIXTURE_ROOT/wc16.out" || true)
assert_eq "WC-16: zero C-quoted claim lines in the output" "0" "$WC16_QUOTED"
(
  source "$WTC_LIB"
  wtc_match_claim "a/Meta Aesthetics/README.md" < "$FIXTURE_ROOT/wc16.out"
) > "$FIXTURE_ROOT/wc16-match.out"
assert_eq "WC-16: wtc_match_claim hits the space-containing path" \
  "a/Meta Aesthetics/README.md WT:wt-quoting" "$(cat "$FIXTURE_ROOT/wc16-match.out")"

echo ""
echo "=== WC-17: a dirty non-ASCII path claims once, bare, and matches (D#1951 AC4) ==="
# The load-bearing new case. Before this change BOTH producers C-quoted a
# non-ASCII path, so no spelling in the list was ever matchable and a
# conflict on such a path never blocked a spawn — not "sometimes".
WC17_COUNT=$(grep -cF "$CAFE WT:wt-quoting" "$FIXTURE_ROOT/wc16.out" || true)
assert_eq "WC-17: exactly one claim line names the non-ASCII path" "1" "$WC17_COUNT"
WC17_ESCAPED=$(grep -cF 'caf\303\251' "$FIXTURE_ROOT/wc16.out" || true)
assert_eq "WC-17: no octal-escaped spelling of the non-ASCII path survives" "0" "$WC17_ESCAPED"
(
  source "$WTC_LIB"
  wtc_match_claim "$CAFE" < "$FIXTURE_ROOT/wc16.out"
) > "$FIXTURE_ROOT/wc17-match.out"
assert_eq "WC-17: wtc_match_claim hits the non-ASCII path" \
  "$CAFE WT:wt-quoting" "$(cat "$FIXTURE_ROOT/wc17-match.out")"

echo ""
echo "=== WC-18: untracked files never leak into the dirty-tracked set (D#1951 AC7 / D#1911 trap) ==="
# `git status --porcelain -z` still emits "?? untracked-noise.txt" records —
# the [MADRC] status-code filter is the only thing dropping them. Assert it
# directly: adopting -z must not widen the gate to untracked files.
(
  source "$WTC_LIB"
  wtc_dirty_tracked_files "$FIXTURE_ROOT/wt-quoting"
) > "$FIXTURE_ROOT/wc18.out" 2>/dev/null
WC18_UNTRACKED=$(grep -cF 'untracked-noise.txt' "$FIXTURE_ROOT/wc18.out" || true)
assert_eq "WC-18: zero lines emitted for the untracked file" "0" "$WC18_UNTRACKED"
WC18_RAW=$(git -C "$FIXTURE_ROOT/wt-quoting" status --porcelain -z | tr '\0' '\n' | grep -cF '?? untracked-noise.txt' || true)
assert_eq "WC-18 fixture: the raw -z stream really does contain the '??' record" "1" "$WC18_RAW"

echo ""
echo "=== WC-19: a rename whose NEW path contains a space claims both halves, bare (D#1951 AC5) ==="
# This is where the -z record reordering and the quoting fix interact. Under
# -z the record shape is "R  <new>\0<old>\0" — the new path shares the status
# record and the old path is the FOLLOWING record with no status prefix. If
# the [RC] branch does not consume that paired record, it falls through and
# gets parsed as though its first two bytes were a status code.
git -C "$MAIN" worktree add --quiet -b feature-rename-space "$FIXTURE_ROOT/wt-rename-space" main
git -C "$FIXTURE_ROOT/wt-rename-space" mv plain.txt "renamed plain.txt"

(
  source "$WTC_LIB"
  wtc_dirty_tracked_files "$FIXTURE_ROOT/wt-rename-space"
) > "$FIXTURE_ROOT/wc19-dirty.out" 2>/dev/null
WC19_LINES=$(grep -c . "$FIXTURE_ROOT/wc19-dirty.out" || true)
assert_eq "WC-19: a rename emits exactly two claim lines" "2" "$WC19_LINES"
WC19_QUOTED=$(grep -c '"' "$FIXTURE_ROOT/wc19-dirty.out" || true)
assert_eq "WC-19: neither rename line is C-quoted" "0" "$WC19_QUOTED"
if grep -qF ' -> ' "$FIXTURE_ROOT/wc19-dirty.out"; then
  echo "  FAIL: WC-19: rename output contains the ' -> ' arrow form"; FAIL=$((FAIL + 1))
else
  echo "  PASS: WC-19: rename output has no ' -> ' arrow form"; PASS=$((PASS + 1))
fi
assert_contains "WC-19: bare NEW path (with its space) is present" "renamed plain.txt" "$(cat "$FIXTURE_ROOT/wc19-dirty.out")"
assert_contains "WC-19: bare OLD path is present" "plain.txt" "$(cat "$FIXTURE_ROOT/wc19-dirty.out")"

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_claimed_files "$FIXTURE_ROOT/wt-rename-space" "feature-rename-space" "wt-rename-space"
) > "$FIXTURE_ROOT/wc19-claimed.out" 2>/dev/null
(
  source "$WTC_LIB"
  wtc_match_claim "renamed plain.txt" < "$FIXTURE_ROOT/wc19-claimed.out"
) > "$FIXTURE_ROOT/wc19-new.out"
assert_eq "WC-19: wtc_match_claim hits the rename's NEW path" \
  "renamed plain.txt WT:wt-rename-space" "$(cat "$FIXTURE_ROOT/wc19-new.out")"
(
  source "$WTC_LIB"
  wtc_match_claim "plain.txt" < "$FIXTURE_ROOT/wc19-claimed.out"
) > "$FIXTURE_ROOT/wc19-old.out"
assert_eq "WC-19: wtc_match_claim hits the rename's OLD path (and not the new one)" \
  "plain.txt WT:wt-rename-space" "$(cat "$FIXTURE_ROOT/wc19-old.out")"

echo ""
echo "=== WC-20: the paired old-path record is consumed, not re-parsed as a status code (D#1951) ==="
# Direct pin for the highest-risk step in the -z conversion. The old path of
# a rename arrives as its OWN record with no status prefix. "Meta
# Aesthetics/DESIGN.md" starts "Me", whose first two bytes MATCH /[MADRC]/ via
# the M — so if the [RC] branch fails to consume it with getline, it falls
# through to the next iteration, passes the status filter, and gets emitted as
# substr($0,4) = "a Aesthetics/DESIGN.md": a junk path that claims a file
# nobody touched, while the real old path is lost. Assert the junk spelling is
# absent by ANCHORING at line start — an unanchored 'ta Aesthetics' substring
# search false-positives on the intact "Meta Aesthetics" path itself.
git -C "$MAIN" worktree add --quiet -b feature-rename-junk "$FIXTURE_ROOT/wt-rename-junk" main
git -C "$FIXTURE_ROOT/wt-rename-junk" mv "Meta Aesthetics/DESIGN.md" "Renamed.md"
(
  source "$WTC_LIB"
  wtc_dirty_tracked_files "$FIXTURE_ROOT/wt-rename-junk"
) > "$FIXTURE_ROOT/wc20.out" 2>/dev/null
WC20_LINES=$(grep -c . "$FIXTURE_ROOT/wc20.out" || true)
assert_eq "WC-20: exactly two claim lines (new + old), no stray third" "2" "$WC20_LINES"
WC20_JUNK=$(grep -c '^a Aesthetics/' "$FIXTURE_ROOT/wc20.out" || true)
assert_eq "WC-20: no mangled 'a Aesthetics/...' junk path emitted" "0" "$WC20_JUNK"
assert_contains "WC-20: the intact old path is emitted" "Meta Aesthetics/DESIGN.md" "$(cat "$FIXTURE_ROOT/wc20.out")"
assert_contains "WC-20: the new path is emitted" "Renamed.md" "$(cat "$FIXTURE_ROOT/wc20.out")"

echo ""
echo "=== WC-A: MERGED worktree, dirty file byte-identical to origin/main drops the claim (D#2090) ==="
# HEAD gets v1, origin/main is advanced past it to v2, and the working tree
# is hand-edited to v2 — dirty relative to this worktree's own (stale) HEAD,
# but byte-identical to CURRENT origin/main. This is the exact defect shape:
# the file never diverged from anything live.
echo "v1" > "$MAIN/wca.txt"
git -C "$MAIN" add wca.txt
_commit "$MAIN" "add wca.txt v1"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-wca "$FIXTURE_ROOT/wt-a" main

echo "v2" > "$MAIN/wca.txt"
git -C "$MAIN" add wca.txt
_commit "$MAIN" "advance wca.txt to v2"
git -C "$MAIN" push --quiet origin main

echo "v2" > "$FIXTURE_ROOT/wt-a/wca.txt"

RAW_PORCELAIN_WCA=$(git -C "$FIXTURE_ROOT/wt-a" status --porcelain)
assert_contains "WC-A fixture: wca.txt is dirty relative to the worktree's own HEAD" "wca.txt" "$RAW_PORCELAIN_WCA"
assert_eq "WC-A fixture: working-tree content equals current origin/main content" "v2" "$(cat "$FIXTURE_ROOT/wt-a/wca.txt")"

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="feature-wca"
  wtc_classify "$FIXTURE_ROOT/wt-a" "feature-wca"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-a" "feature-wca" "wt-a"
) > "$FIXTURE_ROOT/wca.out" 2>/dev/null
WCA_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wca.out" | cut -d= -f2)
assert_eq "WC-A fixture: classified MERGED" "MERGED" "$WCA_CLASS"
WCA_CLAIM_COUNT=$(grep -cF 'wca.txt WT:wt-a' "$FIXTURE_ROOT/wca.out" || true)
assert_eq "WC-A: no claim line for a dirty file byte-identical to origin/main" "0" "$WCA_CLAIM_COUNT"

echo ""
echo "=== WC-B: MERGED worktree, dirty file divergent from origin/main keeps the claim + WARNs (D#2090) ==="
echo "base" > "$MAIN/wcb.txt"
git -C "$MAIN" add wcb.txt
_commit "$MAIN" "add wcb.txt"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-wcb "$FIXTURE_ROOT/wt-b" main
echo "diverged content" > "$FIXTURE_ROOT/wt-b/wcb.txt"

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="feature-wcb"
  wtc_classify "$FIXTURE_ROOT/wt-b" "feature-wcb"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-b" "feature-wcb" "wt-b"
) > "$FIXTURE_ROOT/wcb.out" 2> "$FIXTURE_ROOT/wcb.err"
WCB_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wcb.out" | cut -d= -f2)
assert_eq "WC-B fixture: classified MERGED" "MERGED" "$WCB_CLASS"
assert_contains "WC-B: dirty file divergent from origin/main is still claimed on stdout" "wcb.txt WT:wt-b" "$(cat "$FIXTURE_ROOT/wcb.out")"
assert_contains "WC-B: stderr WARN names the worktree" "worktree wt-b" "$(cat "$FIXTURE_ROOT/wcb.err")"
assert_contains "WC-B: stderr WARN names the specific dirty file" "wcb.txt" "$(cat "$FIXTURE_ROOT/wcb.err")"

echo ""
echo "=== WC-C: a tracked-file deletion counts as divergence from origin/main (D#2090) ==="
echo "present" > "$MAIN/wcc.txt"
git -C "$MAIN" add wcc.txt
_commit "$MAIN" "add wcc.txt"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-wcc "$FIXTURE_ROOT/wt-c" main
OLD_EPOCH_WCC=$(( $(date +%s) - (20 * 86400) ))
echo "old work" > "$FIXTURE_ROOT/wt-c/aged-wcc.txt"
git -C "$FIXTURE_ROOT/wt-c" add aged-wcc.txt
_commit "$FIXTURE_ROOT/wt-c" "aged commit" "@$OLD_EPOCH_WCC"

rm "$FIXTURE_ROOT/wt-c/wcc.txt"
# touch AFTER the rm — removing a directory entry bumps the parent
# directory's own mtime back to "now", which would silently undo the aging
# below if done in the other order.
touch -d "@$OLD_EPOCH_WCC" "$FIXTURE_ROOT/wt-c"

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-c" "feature-wcc"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-c" "feature-wcc" "wt-c"
) > "$FIXTURE_ROOT/wcc.out" 2>/dev/null
WCC_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wcc.out" | cut -d= -f2)
assert_eq "WC-C fixture: classified STALE by wall-clock age" "STALE" "$WCC_CLASS"
assert_contains "WC-C: a file deleted from the working tree but present in origin/main is still claimed" "wcc.txt WT:wt-c" "$(cat "$FIXTURE_ROOT/wcc.out")"

echo ""
echo "=== WC-D: ACTIVE worktree keeps claiming even when dirty content matches origin/main (D#2090 constraint) ==="
echo "v1" > "$MAIN/wcd.txt"
git -C "$MAIN" add wcd.txt
_commit "$MAIN" "add wcd.txt v1"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-wcd "$FIXTURE_ROOT/wt-d" main

echo "v2" > "$MAIN/wcd.txt"
git -C "$MAIN" add wcd.txt
_commit "$MAIN" "advance wcd.txt to v2"
git -C "$MAIN" push --quiet origin main

echo "v2" > "$FIXTURE_ROOT/wt-d/wcd.txt"

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-d" "feature-wcd"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-d" "feature-wcd" "wt-d"
) > "$FIXTURE_ROOT/wcd.out" 2>/dev/null
WCD_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wcd.out" | cut -d= -f2)
assert_eq "WC-D fixture: classified ACTIVE" "ACTIVE" "$WCD_CLASS"
assert_contains "WC-D: ACTIVE worktree still claims a dirty file that happens to match origin/main" "wcd.txt WT:wt-d" "$(cat "$FIXTURE_ROOT/wcd.out")"

echo ""
echo "=== WC-E: unresolvable origin/main fails closed — every dirty tracked file stays claimed (D#2090) ==="
# A second, fully independent fixture repo — deleting the "origin" remote is
# a repo-wide config change shared by every worktree of THAT repo, so it must
# not be the same $ORIGIN/$MAIN every other test in this file depends on.
ORIGIN2="$FIXTURE_ROOT/origin2.git"
MAIN2="$FIXTURE_ROOT/main2"
git init --quiet --bare "$ORIGIN2"
git clone --quiet "$ORIGIN2" "$MAIN2"
echo "seed2" > "$MAIN2/README.md"
git -C "$MAIN2" add README.md
_commit "$MAIN2" "seed2"
git -C "$MAIN2" branch -M main
git -C "$MAIN2" push --quiet -u origin main

echo "v1" > "$MAIN2/wce.txt"
git -C "$MAIN2" add wce.txt
_commit "$MAIN2" "add wce.txt"
git -C "$MAIN2" push --quiet origin main

git -C "$MAIN2" worktree add --quiet -b feature-wce "$FIXTURE_ROOT/wt-e" main

OLD_EPOCH_WCE=$(( $(date +%s) - (20 * 86400) ))
echo "old work" > "$FIXTURE_ROOT/wt-e/aged-wce.txt"
git -C "$FIXTURE_ROOT/wt-e" add aged-wce.txt
_commit "$FIXTURE_ROOT/wt-e" "aged commit" "@$OLD_EPOCH_WCE"
touch -d "@$OLD_EPOCH_WCE" "$FIXTURE_ROOT/wt-e"

echo "dirty edit" >> "$FIXTURE_ROOT/wt-e/wce.txt"

git -C "$FIXTURE_ROOT/wt-e" remote remove origin
RESOLVE_RC=0
git -C "$FIXTURE_ROOT/wt-e" rev-parse --verify --quiet origin/main >/dev/null 2>&1 || RESOLVE_RC=$?
if [ "$RESOLVE_RC" -ne 0 ]; then
  echo "  (fixture confirmed: origin/main does not resolve in wt-e, rc=$RESOLVE_RC)"
else
  echo "  FAIL: fixture setup — origin/main still resolves in wt-e"; FAIL=$((FAIL + 1))
fi

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-e" "feature-wce"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-e" "feature-wce" "wt-e"
) > "$FIXTURE_ROOT/wce.out" 2>/dev/null
WCE_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wce.out" | cut -d= -f2)
assert_eq "WC-E fixture: classified STALE (wall-clock) despite origin removed" "STALE" "$WCE_CLASS"
assert_contains "WC-E: dirty file remains claimed when origin/main cannot be resolved (fail closed)" "wce.txt WT:wt-e" "$(cat "$FIXTURE_ROOT/wce.out")"

echo ""
echo "=== WC-F: the surviving WARN header states class, reason, and age in days (D#2090) ==="
assert_contains "WC-F: WARN header contains the classification" "MERGED" "$(cat "$FIXTURE_ROOT/wcb.err")"
assert_contains "WC-F: WARN header contains the WTC_REASON text" "head ref of a merged PR" "$(cat "$FIXTURE_ROOT/wcb.err")"
if grep -qE '[0-9]+d ago' "$FIXTURE_ROOT/wcb.err"; then
  echo "  PASS: WC-F: WARN header states an age in days"; PASS=$((PASS + 1))
else
  echo "  FAIL: WC-F: WARN header missing an age-in-days marker"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-G: a staged rename in a MERGED/STALE worktree claims BOTH paths (D#2090 fix-cycle 1) ==="
# Fix-cycle 1 finding: default rename detection in the origin/main diff
# collapses a staged rename to "new path only", silently dropping the old
# path even though it still differs from origin/main (which holds the file
# there). Renames into archive/ are the house style (Archive Protocol
# mandates `git mv` over `git rm`), so this is routine, not exotic — the
# real worktree that motivated D#2090 has exactly this shape in its dirty
# set.
echo "present" > "$MAIN/wcg-old.txt"
git -C "$MAIN" add wcg-old.txt
_commit "$MAIN" "add wcg-old.txt"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-wcg "$FIXTURE_ROOT/wt-g" main
mkdir -p "$FIXTURE_ROOT/wt-g/archive"
git -C "$FIXTURE_ROOT/wt-g" mv wcg-old.txt archive/wcg-new.txt

RAW_PORCELAIN_WCG=$(git -C "$FIXTURE_ROOT/wt-g" status --porcelain)
assert_contains "WC-G fixture: porcelain reports the staged rename" "wcg-old.txt" "$RAW_PORCELAIN_WCG"

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="feature-wcg"
  wtc_classify "$FIXTURE_ROOT/wt-g" "feature-wcg"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-g" "feature-wcg" "wt-g"
) > "$FIXTURE_ROOT/wcg.out" 2> "$FIXTURE_ROOT/wcg.err"
WCG_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wcg.out" | cut -d= -f2)
assert_eq "WC-G fixture: classified MERGED" "MERGED" "$WCG_CLASS"
# Exact-line match (not substring): "archive/wcg-new.txt WT:wt-g" does not
# contain "wcg-old.txt WT:wt-g" as a substring, so these two checks cannot
# pass vacuously off of each other's line the way overlapping filenames
# would (D#1914-style anchoring discipline, applied to fixture naming here).
WCG_OLD_COUNT=$(grep -cFx 'wcg-old.txt WT:wt-g' "$FIXTURE_ROOT/wcg.out" || true)
assert_eq "WC-G: the rename's OLD path (still differs from origin/main) is claimed" "1" "$WCG_OLD_COUNT"
WCG_NEW_COUNT=$(grep -cFx 'archive/wcg-new.txt WT:wt-g' "$FIXTURE_ROOT/wcg.out" || true)
assert_eq "WC-G: the rename's NEW path (still differs from origin/main) is claimed" "1" "$WCG_NEW_COUNT"

echo ""
echo "=== WC-H: no PR ever + past abandoned-hours threshold classifies ABANDONED (D#2155 PR-a) ==="
# 30 hours old: past the 24h default claim_gate_abandoned_hours threshold,
# but far under the 14-day default STALE threshold, and 0 commits behind
# (freshly branched off the current origin/main tip) — exactly the shape
# MERGED and STALE both miss.
git -C "$MAIN" worktree add --quiet -b feature-abandoned "$FIXTURE_ROOT/wt-abandoned" main
ABANDONED_EPOCH=$(( $(date +%s) - (30 * 3600) ))
echo "abandoned work" > "$FIXTURE_ROOT/wt-abandoned/abandoned.txt"
git -C "$FIXTURE_ROOT/wt-abandoned" add abandoned.txt
_commit "$FIXTURE_ROOT/wt-abandoned" "abandoned commit" "@$ABANDONED_EPOCH"
touch -d "@$ABANDONED_EPOCH" "$FIXTURE_ROOT/wt-abandoned"

BEHIND_WCH=$(git -C "$FIXTURE_ROOT/wt-abandoned" rev-list --count HEAD..origin/main)
assert_eq "WC-H fixture: 0 commits behind (would not be caught by STALE's commits-behind arm)" "0" "$BEHIND_WCH"

(
  source "$WTC_LIB"
  # Neither cache contains this branch — "not merged" and "never had a PR".
  export WTC_MERGED_HEADS_OVERRIDE="unrelated-branch"
  export WTC_ALL_HEADS_OVERRIDE="unrelated-branch"
  wtc_classify "$FIXTURE_ROOT/wt-abandoned" "feature-abandoned"
  echo "CLASS=$WTC_CLASS"
  echo "REASON=$WTC_REASON"
) > "$FIXTURE_ROOT/wch.out"
WCH_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wch.out" | cut -d= -f2)
assert_eq "WC-H: classified ABANDONED (no PR ever, past hour threshold, under STALE thresholds)" "ABANDONED" "$WCH_CLASS"
assert_contains "WC-H: reason names the never-had-a-PR signal" "never had a PR opened" "$(cat "$FIXTURE_ROOT/wch.out")"
assert_contains "WC-H: reason names the hour threshold, not a day threshold" "threshold 24h" "$(cat "$FIXTURE_ROOT/wch.out")"

echo ""
echo "=== WC-I: a branch with ANY existing PR (even closed/unmerged) is never ABANDONED (D#2155 PR-a) ==="
git -C "$MAIN" worktree add --quiet -b feature-closed-pr "$FIXTURE_ROOT/wt-closedpr" main
CLOSEDPR_EPOCH=$(( $(date +%s) - (30 * 3600) ))
echo "work" > "$FIXTURE_ROOT/wt-closedpr/closed.txt"
git -C "$FIXTURE_ROOT/wt-closedpr" add closed.txt
_commit "$FIXTURE_ROOT/wt-closedpr" "closed pr commit" "@$CLOSEDPR_EPOCH"
touch -d "@$CLOSEDPR_EPOCH" "$FIXTURE_ROOT/wt-closedpr"

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="unrelated-branch"
  # This branch DOES appear in the "ever had a PR" set — e.g. an open or
  # closed-without-merge PR — so ABANDONED must not fire even though it is
  # old enough by the hour threshold.
  export WTC_ALL_HEADS_OVERRIDE="feature-closed-pr"
  wtc_classify "$FIXTURE_ROOT/wt-closedpr" "feature-closed-pr"
  echo "CLASS=$WTC_CLASS"
) > "$FIXTURE_ROOT/wci.out"
WCI_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wci.out" | cut -d= -f2)
assert_eq "WC-I: classified ACTIVE, not ABANDONED, once the branch has any PR on record" "ACTIVE" "$WCI_CLASS"

echo ""
echo "=== WC-J: gh-unavailable degrade — wtc_branch_ever_had_pr fails closed, never triggers ABANDONED (D#2155 PR-a) ==="
(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_branch_ever_had_pr "feature-abandoned"
  echo "RC=$?"
) > "$FIXTURE_ROOT/wcj.out"
assert_contains "WC-J: wtc_branch_ever_had_pr returns 0 (assumes a PR exists) when gh is unavailable" "RC=0" "$(cat "$FIXTURE_ROOT/wcj.out")"

(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-abandoned" "feature-abandoned"
  echo "CLASS=$WTC_CLASS"
) > "$FIXTURE_ROOT/wcj2.out"
WCJ2_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wcj2.out" | cut -d= -f2)
if [ "$WCJ2_CLASS" != "ABANDONED" ]; then
  echo "  PASS: WC-J: an otherwise-ABANDONED-shaped worktree is never classified ABANDONED when gh is unavailable"; PASS=$((PASS + 1))
else
  echo "  FAIL: WC-J: classified ABANDONED despite gh being unavailable — should fail closed"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-K: ABANDONED worktree, dirty file divergent from origin/main keeps the claim + WARNs (D#2155 PR-a) ==="
# Same dirty-file safety valve MERGED/STALE already use (D#2090) — pins that
# PR-a did not move or bypass it; ABANDONED joins the same shared filter.
echo "base" > "$MAIN/wck.txt"
git -C "$MAIN" add wck.txt
_commit "$MAIN" "add wck.txt"
git -C "$MAIN" push --quiet origin main

git -C "$MAIN" worktree add --quiet -b feature-wck "$FIXTURE_ROOT/wt-k" main
WCK_EPOCH=$(( $(date +%s) - (30 * 3600) ))
echo "old" > "$FIXTURE_ROOT/wt-k/wck-aged.txt"
git -C "$FIXTURE_ROOT/wt-k" add wck-aged.txt
_commit "$FIXTURE_ROOT/wt-k" "aged commit" "@$WCK_EPOCH"
touch -d "@$WCK_EPOCH" "$FIXTURE_ROOT/wt-k"
echo "diverged content" > "$FIXTURE_ROOT/wt-k/wck.txt"

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="unrelated-branch"
  export WTC_ALL_HEADS_OVERRIDE="unrelated-branch"
  wtc_classify "$FIXTURE_ROOT/wt-k" "feature-wck"
  echo "CLASS=$WTC_CLASS"
  wtc_claimed_files "$FIXTURE_ROOT/wt-k" "feature-wck" "wt-k"
) > "$FIXTURE_ROOT/wck.out" 2> "$FIXTURE_ROOT/wck.err"
WCK_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wck.out" | cut -d= -f2)
assert_eq "WC-K fixture: classified ABANDONED" "ABANDONED" "$WCK_CLASS"
assert_contains "WC-K: dirty file divergent from origin/main is still claimed on stdout" "wck.txt WT:wt-k" "$(cat "$FIXTURE_ROOT/wck.out")"
assert_contains "WC-K: stderr WARN names the worktree and the ABANDONED class" "worktree wt-k is classified ABANDONED" "$(cat "$FIXTURE_ROOT/wck.err")"

echo ""
echo "=== WC-L: gh subprocess count stays O(1), not O(N), across N wtc_classify calls (D#2155 PR-a acceptance) ==="
# Acceptance requires this be a SUBPROCESS COUNT assertion under a PATH shim,
# not a wall-clock threshold — CI timing variance would make a clock-based
# assertion flaky. A fake `gh` on PATH records every invocation; classifying
# 5 different worktree/branch pairs in ONE process must still make exactly
# ONE --state merged call and ONE --state all call, both cached after their
# first use (wtc_load_merged_heads / wtc_load_all_pr_heads), never a call
# per worktree.
GH_SHIM_DIR="$FIXTURE_ROOT/gh-shim"
mkdir -p "$GH_SHIM_DIR"
GH_CALL_LOG="$FIXTURE_ROOT/gh-calls.log"
: > "$GH_CALL_LOG"
cat > "$GH_SHIM_DIR/gh" <<SHIMEOF
#!/usr/bin/env bash
echo "\$*" >> "$GH_CALL_LOG"
echo ""
exit 0
SHIMEOF
chmod +x "$GH_SHIM_DIR/gh"

(
  source "$WTC_LIB"
  export PATH="$GH_SHIM_DIR:$PATH"
  unset WTC_SKIP_GH WTC_MERGED_HEADS_OVERRIDE WTC_ALL_HEADS_OVERRIDE
  # Each pair must actually be old enough to reach the ABANDONED check (the
  # age_hours > threshold guard short-circuits before wtc_branch_ever_had_pr
  # is ever called) — reuse the already-aged fixtures from earlier tests
  # rather than fresh ones, so this exercises BOTH gh call sites.
  for pair in \
    "$FIXTURE_ROOT/wt-abandoned:feature-abandoned" \
    "$FIXTURE_ROOT/wt-wallclock:feature-wallclock-stale" \
    "$FIXTURE_ROOT/wt-c:feature-wcc" \
    "$FIXTURE_ROOT/wt-k:feature-wck" \
    "$FIXTURE_ROOT/wt-abandoned:feature-abandoned"
  do
    wtc_classify "${pair%%:*}" "${pair##*:}" >/dev/null 2>&1 || true
  done
)
GH_CALL_COUNT=$(grep -c . "$GH_CALL_LOG" || true)
assert_eq "WC-L: exactly 2 gh subprocess calls total across 5 wtc_classify calls (cached, not O(N))" "2" "$GH_CALL_COUNT"
GH_MERGED_CALLS=$(grep -c -- '--state merged' "$GH_CALL_LOG" || true)
assert_eq "WC-L: exactly 1 gh call used --state merged" "1" "$GH_MERGED_CALLS"
GH_ALL_CALLS=$(grep -c -- '--state all' "$GH_CALL_LOG" || true)
assert_eq "WC-L: exactly 1 gh call used --state all (separate call/cache from merged)" "1" "$GH_ALL_CALLS"

echo ""
echo "=== WC-M: sweep-stale-worktrees.sh's census grep counts ABANDONED, not just MERGED|STALE (D#2155 PR-a) ==="
if grep -qE "grep -cE ' \(MERGED\|ABANDONED\|STALE\) '" "$REAL_REPO_ROOT/scripts/sweep-stale-worktrees.sh"; then
  echo "  PASS: WC-M: census grep includes ABANDONED"; PASS=$((PASS + 1))
else
  echo "  FAIL: WC-M: census grep does not include ABANDONED — would silently undercount"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-N: claim_gate_abandoned_hours has a shipped default of 24, no enable flag (D#2155 PR-a) ==="
DEFAULT_HOURS=$(python3 "$REAL_REPO_ROOT/backend/control_plane.py" get policies.team_lead.claim_gate_abandoned_hours 2>/dev/null | tr -d '"')
assert_eq "WC-N: control_plane default for claim_gate_abandoned_hours is 24" "24" "$DEFAULT_HOURS"
# Inspect only the actual guarding `if` for the ABANDONED arm (not the
# surrounding prose, which legitimately discusses-and-rejects an enable flag
# by name) — the functional condition itself must never reference a boolean
# gate/enable check.
ABANDONED_IF_LINE=$(grep -n 'WTC_CLASS="ABANDONED"' "$WTC_LIB" | head -1 | cut -d: -f1)
ABANDONED_GUARD=$(sed -n "$((ABANDONED_IF_LINE - 1))p" "$WTC_LIB")
assert_contains "WC-N fixture: located the ABANDONED guard condition" "age_hours" "$ABANDONED_GUARD"
if echo "$ABANDONED_GUARD" | grep -qiE 'enable'; then
  echo "  FAIL: WC-N: the ABANDONED guard condition itself references an enable flag — spec requires none"; FAIL=$((FAIL + 1))
else
  echo "  PASS: WC-N: the ABANDONED guard condition is a threshold check only, no enable flag"; PASS=$((PASS + 1))
fi

echo ""
echo "=== WC-O: a successful-but-EMPTY all-heads answer fails closed too, not just an unavailable one (code review fix, D#2155 PR-a) ==="
# WTC_ALL_HEADS_OVERRIDE cannot simulate this: an empty override string is
# falsy for the "-n" check in wtc_load_all_pr_heads, so it falls straight
# through to a REAL gh call rather than exercising the empty-cache path.
# The only way to reach this state is to force the cache variables directly,
# the way the reviewer reproduced it: a load that reported success
# (_LOADED=1, _AVAILABLE=1) but left the cache empty. Before the fix, this
# path returned 1 ("no PR ever"), inverting the fail-closed contract and
# firing ABANDONED on an unverifiable answer.
(
  source "$WTC_LIB"
  _WTC_ALL_PR_HEADS_LOADED=1
  _WTC_ALL_PR_HEADS_AVAILABLE=1
  _WTC_ALL_PR_HEADS_CACHE=""
  wtc_branch_ever_had_pr "feature-anything"
  echo "RC=$?"
) > "$FIXTURE_ROOT/wco.out"
assert_contains "WC-O: wtc_branch_ever_had_pr returns 0 (assumes a PR exists) on a successful-but-empty cache" "RC=0" "$(cat "$FIXTURE_ROOT/wco.out")"

# End-to-end pin at the wtc_classify level: an old, no-PR-ever-looking
# worktree must NOT be classified ABANDONED when the all-heads cache is
# forced into this successful-but-empty state.
git -C "$MAIN" worktree add --quiet -b feature-emptycache "$FIXTURE_ROOT/wt-emptycache" main
EMPTYCACHE_EPOCH=$(( $(date +%s) - (30 * 3600) ))
echo "old work" > "$FIXTURE_ROOT/wt-emptycache/emptycache.txt"
git -C "$FIXTURE_ROOT/wt-emptycache" add emptycache.txt
_commit "$FIXTURE_ROOT/wt-emptycache" "aged commit" "@$EMPTYCACHE_EPOCH"
touch -d "@$EMPTYCACHE_EPOCH" "$FIXTURE_ROOT/wt-emptycache"

(
  source "$WTC_LIB"
  export WTC_MERGED_HEADS_OVERRIDE="unrelated-branch"
  _WTC_ALL_PR_HEADS_LOADED=1
  _WTC_ALL_PR_HEADS_AVAILABLE=1
  _WTC_ALL_PR_HEADS_CACHE=""
  wtc_classify "$FIXTURE_ROOT/wt-emptycache" "feature-emptycache"
  echo "CLASS=$WTC_CLASS"
) > "$FIXTURE_ROOT/wco2.out"
WCO2_CLASS=$(grep '^CLASS=' "$FIXTURE_ROOT/wco2.out" | cut -d= -f2)
if [ "$WCO2_CLASS" != "ABANDONED" ]; then
  echo "  PASS: WC-O: wtc_classify does not fire ABANDONED off a successful-but-empty all-heads cache"; PASS=$((PASS + 1))
else
  echo "  FAIL: WC-O: wtc_classify fired ABANDONED off a successful-but-empty all-heads cache — fail-closed contract inverted"; FAIL=$((FAIL + 1))
fi

echo ""
echo "=== WC-P: single-slot dirty-files memo — no cross-path leak, no false staleness (D#2158) ==="
# Two worktrees, each with a DIFFERENT dirty tracked file, so a leaked or
# corrupted slot is directly observable in the output. Call path A, then B,
# then A again, all inside ONE process (one `(...)` subshell) — the memo's
# entire contract is that its lifetime is exactly one process/scan.
git -C "$MAIN" worktree add --quiet -b feature-memo-a "$FIXTURE_ROOT/wt-memo-a" main
echo "seed" > "$FIXTURE_ROOT/wt-memo-a/tracked-a.txt"
git -C "$FIXTURE_ROOT/wt-memo-a" add tracked-a.txt
_commit "$FIXTURE_ROOT/wt-memo-a" "seed tracked-a"
echo "dirty content A" > "$FIXTURE_ROOT/wt-memo-a/tracked-a.txt"

git -C "$MAIN" worktree add --quiet -b feature-memo-b "$FIXTURE_ROOT/wt-memo-b" main
echo "seed" > "$FIXTURE_ROOT/wt-memo-b/tracked-b.txt"
git -C "$FIXTURE_ROOT/wt-memo-b" add tracked-b.txt
_commit "$FIXTURE_ROOT/wt-memo-b" "seed tracked-b"
echo "dirty content B" > "$FIXTURE_ROOT/wt-memo-b/tracked-b.txt"

(
  source "$WTC_LIB"
  _wtc_dirty_memo "$FIXTURE_ROOT/wt-memo-a"
  echo "A1=[$WTC_DIRTY_MEMO_VALUE]"
  _wtc_dirty_memo "$FIXTURE_ROOT/wt-memo-b"
  echo "B1=[$WTC_DIRTY_MEMO_VALUE]"
  _wtc_dirty_memo "$FIXTURE_ROOT/wt-memo-a"
  echo "A2=[$WTC_DIRTY_MEMO_VALUE]"
) > "$FIXTURE_ROOT/wcp.out"

A1=$(grep '^A1=' "$FIXTURE_ROOT/wcp.out" | sed 's/^A1=//')
B1=$(grep '^B1=' "$FIXTURE_ROOT/wcp.out" | sed 's/^B1=//')
A2=$(grep '^A2=' "$FIXTURE_ROOT/wcp.out" | sed 's/^A2=//')

assert_contains "WC-P: A's first call returns its own dirty file" "tracked-a.txt" "$A1"
assert_contains "WC-P: B's call returns its own dirty file" "tracked-b.txt" "$B1"
assert_eq "WC-P: A's second call (after B) equals A's first call — no stale/corrupted carryover" "$A1" "$A2"
if [ "$B1" = "$A1" ]; then
  echo "  FAIL: WC-P: B's result equals A's — the slot leaked across paths"; FAIL=$((FAIL + 1))
else
  echo "  PASS: WC-P: B's result differs from A's — no cross-path leak"; PASS=$((PASS + 1))
fi

# Regression guard for the specific trap the Spec calls out: an empty memo
# must yield dirty_count=0, not 1 (printf '%s' vs printf '%s\n' into `grep -c .`).
git -C "$MAIN" worktree add --quiet -b feature-memo-clean "$FIXTURE_ROOT/wt-memo-clean" main
(
  source "$WTC_LIB"
  export WTC_SKIP_GH=1
  wtc_classify "$FIXTURE_ROOT/wt-memo-clean" "feature-memo-clean"
  echo "DIRTY_COUNT=$WTC_DIRTY_COUNT"
) > "$FIXTURE_ROOT/wcp-clean.out"
assert_contains "WC-P: a clean worktree's dirty count is 0, not 1 (empty-memo guard)" "DIRTY_COUNT=0" "$(cat "$FIXTURE_ROOT/wcp-clean.out")"

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
