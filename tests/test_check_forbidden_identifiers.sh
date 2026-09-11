#!/usr/bin/env bash
# tests/test_check_forbidden_identifiers.sh — D#2348 PR-g.
#
# Covers scripts/check-forbidden-identifiers.sh, the pre-push
# forbidden-identifier scan wired into run_always_gates().
#
# Two harnesses:
#
#   A. SYNTHETIC. A scratch repo with a scratch IDENTIFIER-RULES.txt, used
#      for the parser and fail-closed edge cases. The rules file's patterns
#      are invented tokens, so nothing in this file has to spell a real
#      forbidden identifier to test the machinery.
#
#   B. REAL RULES. A scratch repo carrying a COPY of this repo's own
#      open-source/IDENTIFIER-RULES.txt (D#2492 — a code-plane-scoped file,
#      not a copy of the private engine repo's fuller one; see that file's
#      own header for why), with the planted string derived at runtime from
#      the file's own FORBIDDEN_PATTERNS block rather than typed here. This
#      is what proves the gate refuses an identifier it actually exists to
#      refuse, and it does so without this test source ever spelling one out
#      — matters here the same way it mattered for the PR-b cleanup this
#      comment used to cite, even though this file's patterns are milder.
#
#   D/E/F. D#2492 additions: a tree with no rules file self-skips loudly
#      instead of passing quietly (item 1); an empty rules file is a
#      refusal, not a vacuous pass (item 5); and a positive control proving
#      the real file's exception list flags exactly what it should and
#      nothing else (item 4).
#
# Run: bash tests/test_check_forbidden_identifiers.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_SRC="$REPO_ROOT/scripts/check-forbidden-identifiers.sh"
REAL_RULES="$REPO_ROOT/open-source/IDENTIFIER-RULES.txt"

PASS=0
FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

assert_rc() {
  local label="$1" want="$2" got="$3" out="${4:-}"
  if [[ "$got" -eq "$want" ]]; then
    ok "$label"
  else
    bad "$label" "expected rc $want, got $got${out:+ | output: $out}"
  fi
}

assert_contains() {
  local label="$1" needle="$2" hay="$3"
  if [[ "$hay" == *"$needle"* ]]; then
    ok "$label"
  else
    bad "$label" "expected output to contain '$needle', got: $hay"
  fi
}

assert_not_contains() {
  local label="$1" needle="$2" hay="$3"
  if [[ "$hay" != *"$needle"* ]]; then
    ok "$label"
  else
    bad "$label" "expected output NOT to contain '$needle', got: $hay"
  fi
}

# Per-run scratch root. mktemp -d, never a fixed /tmp name — a fixed name
# races every concurrent invocation of this suite (D#2254).
SCRATCH="$(mktemp -d)"
if [[ -z "$SCRATCH" || ! -d "$SCRATCH" ]]; then
  echo "FATAL: could not create scratch dir"
  exit 1
fi
trap 'rm -rf "$SCRATCH"' EXIT

# make_repo <name> — a scratch tree shaped like this repo (scripts/ +
# open-source/), git-initialised with one baseline commit. Echoes the root.
# The scan script hardcodes its rules-file path relative to its own location,
# so it has to be copied into a real directory layout rather than pointed at
# one.
make_repo() {
  local root="$SCRATCH/$1"
  mkdir -p "$root/scripts" "$root/open-source" "$root/src"
  cp "$SCAN_SRC" "$root/scripts/check-forbidden-identifiers.sh"
  printf 'baseline\n' > "$root/src/app.txt"
  git -C "$root" init -q
  git -C "$root" config user.email "test@example.invalid"
  git -C "$root" config user.name "test"
  printf '%s\n' "$root"
}

commit_baseline() {
  local root="$1"
  git -C "$root" add -A
  git -C "$root" -c commit.gpgsign=false commit -q -m baseline
  git -C "$root" rev-parse HEAD
}

# write_rules <root> <forbidden-lines...> — synthetic rules file, harness A.
write_synthetic_rules() {
  local root="$1"; shift
  {
    echo "=== IDENTITIES_START ==="
    echo "SECRET_TOKEN=zzsecretname"
    echo "=== IDENTITIES_END ==="
    echo "=== FORBIDDEN_PATTERNS_START ==="
    printf '%s\n' "$@"
    echo "=== FORBIDDEN_PATTERNS_END ==="
    echo "=== ALLOWLIST_START ==="
    echo "=== ALLOWLIST_END ==="
  } > "$root/open-source/IDENTIFIER-RULES.txt"
}

run_scan() {
  local root="$1" base="$2"
  bash "$root/scripts/check-forbidden-identifiers.sh" --base "$base" 2>&1
}

echo "=== A. clean tree exits 0 (Spec item 4) ==="
R="$(make_repo clean)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "clean tree, nothing added -> rc 0" 0 "$RC" "$OUT"
assert_contains "clean tree reports PASS with a verdict line" "PASS (" "$OUT"
assert_contains "clean tree states added_lines=0" "added_lines=0" "$OUT"

echo "=== A. planted identifier fails and names file and line (Spec item 3) ==="
R="$(make_repo planted)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
BASE="$(commit_baseline "$R")"
printf 'line one\nline two\nzzsecretname here\n' > "$R/src/app.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "planted identifier -> rc 1" 1 "$RC" "$OUT"
assert_contains "names the file and the line number" "src/app.txt:3" "$OUT"
assert_contains "names the pattern that matched" "pattern: zzsecretname" "$OUT"

echo "=== A. uncommitted work is scanned, not just commits ==="
# The whole point of a pre-push gate: catch it before it is even a commit.
R="$(make_repo uncommitted)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
BASE="$(commit_baseline "$R")"
printf 'zzsecretname\n' > "$R/src/new-untracked-content.txt"
git -C "$R" add -A
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "staged-but-uncommitted addition -> rc 1" 1 "$RC" "$OUT"

echo "=== A. an unresolvable base ref is a hard failure on BOTH paths ==="
# Regression test. The rev-parse validation used to guard only the
# auto-resolve branch, so `--base <nonexistent>` produced an empty diff and
# the scan reported PASS with a real identifier in a tracked file — a gate
# reporting success having scanned nothing, which is the defect this gate
# exists to prevent. run_always_gates() passes no args and never hit it; a
# CI job passing an explicit base would have.
R="$(make_repo badbase)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
BASE="$(commit_baseline "$R")"
printf 'zzsecretname\n' >> "$R/src/app.txt"
OUT="$(run_scan "$R" "definitely-not-a-ref")"; RC=$?
assert_rc "explicit --base naming a nonexistent ref -> rc 1" 1 "$RC" "$OUT"
assert_contains "says the base does not resolve" "does not resolve to a commit" "$OUT"
assert_not_contains "does not report a PASS" "PASS (" "$OUT"
# ...and the valid-ref case still works, so the guard is not just refusing
# everything.
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "explicit --base naming a real commit still scans -> rc 1" 1 "$RC" "$OUT"
assert_contains "and finds the planted identifier" "src/app.txt:" "$OUT"

echo "=== A. a brand-new UNTRACKED file is scanned ==="
# git diff cannot see an untracked file at all, so without explicit
# handling a whole new file of identifiers reads as clean until `git add`
# — the worst moment for this gate to go quiet, since an executor runs
# preflight while still writing.
R="$(make_repo untracked)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
BASE="$(commit_baseline "$R")"
printf 'first\nzzsecretname\n' > "$R/src/brand-new.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "untracked file with a hit -> rc 1" 1 "$RC" "$OUT"
assert_contains "names the untracked file and line" "src/brand-new.txt:2" "$OUT"
# ...and .gitignore is honoured, so build output does not block a push.
printf 'ignored-dir/\n' > "$R/.gitignore"
mkdir -p "$R/ignored-dir"
printf 'zzsecretname\n' > "$R/ignored-dir/out.txt"
rm "$R/src/brand-new.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "gitignored untracked file -> rc 0" 0 "$RC" "$OUT"

echo "=== A. pre-existing hit in an untouched file does not block ==="
# A whole-tree scan would fail here. This gate is about what a push ADDS.
R="$(make_repo preexisting)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
printf 'zzsecretname was already here\n' > "$R/src/legacy.txt"
BASE="$(commit_baseline "$R")"
printf 'an unrelated edit\n' >> "$R/src/app.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "pre-existing hit, unrelated edit -> rc 0" 0 "$RC" "$OUT"

echo "=== A. excluded paths are not scanned ==="
R="$(make_repo excluded)"
write_synthetic_rules "$R" '{SECRET_TOKEN}'
BASE="$(commit_baseline "$R")"
mkdir -p "$R/archive/old-thing-2026-09-04" "$R/.autonomous-team"
printf 'zzsecretname\n' > "$R/archive/old-thing-2026-09-04/moved.txt"
printf 'zzsecretname\n' > "$R/.autonomous-team/state.txt"
git -C "$R" add -A
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "hits under archive/ and .autonomous-team/ -> rc 0" 0 "$RC" "$OUT"
assert_not_contains "does not name the archived path" "archive/old-thing" "$OUT"

echo "=== A. zero parsed patterns is a hard failure, never a vacuous PASS ==="
R="$(make_repo nopatterns)"
write_synthetic_rules "$R"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "empty FORBIDDEN_PATTERNS -> rc 1" 1 "$RC" "$OUT"
assert_contains "refuses a vacuous PASS by name" "vacuous PASS" "$OUT"

echo "=== A. an unresolved {TOKEN} is a hard failure ==="
R="$(make_repo badtoken)"
write_synthetic_rules "$R" '{NO_SUCH_TOKEN}'
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "unresolved token -> rc 1" 1 "$RC" "$OUT"
assert_contains "says it refuses to run it as a regex" "unresolved {TOKEN}" "$OUT"

echo "=== A. PREPUSH_EXEMPT fail-closed rules ==="
# An exemption is only valid if it names a pattern that still exists
# verbatim. This is the coupling that stops an exemption outliving its
# pattern.
exempt_repo() {
  local name="$1" exempt_block="$2"
  local root; root="$(make_repo "$name")"
  {
    echo "=== IDENTITIES_START ==="
    echo "SECRET_TOKEN=zzsecretname"
    echo "=== IDENTITIES_END ==="
    echo "=== FORBIDDEN_PATTERNS_START ==="
    echo "{SECRET_TOKEN}"
    echo "zzothername"
    echo "=== FORBIDDEN_PATTERNS_END ==="
    echo "=== PREPUSH_EXEMPT_START ==="
    printf '%b\n' "$exempt_block"
    echo "=== PREPUSH_EXEMPT_END ==="
    echo "=== ALLOWLIST_START ==="
    echo "=== ALLOWLIST_END ==="
  } > "$root/open-source/IDENTIFIER-RULES.txt"
  printf '%s\n' "$root"
}

R="$(exempt_repo exempt_ok '{SECRET_TOKEN}\tlegitimately present across the tree')"
BASE="$(commit_baseline "$R")"
printf 'zzsecretname\n' >> "$R/src/app.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "exempt pattern does not block -> rc 0" 0 "$RC" "$OUT"
assert_contains "summary reports the enforced/total split" "patterns=1/2" "$OUT"

R="$(exempt_repo exempt_ok2 '{SECRET_TOKEN}\tlegitimately present across the tree')"
BASE="$(commit_baseline "$R")"
printf 'zzothername\n' >> "$R/src/app.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "a NON-exempt pattern still blocks -> rc 1" 1 "$RC" "$OUT"

R="$(exempt_repo exempt_notab '{SECRET_TOKEN} no tab here')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "exemption with no tab -> rc 1" 1 "$RC" "$OUT"
assert_contains "rejects an exemption with no stated reason" "no stated reason" "$OUT"

R="$(exempt_repo exempt_stale 'zzpatternthatisnotlisted\tsome reason')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "exemption naming a non-existent pattern -> rc 1" 1 "$RC" "$OUT"
assert_contains "calls it a stale exemption" "stale exemption" "$OUT"

R="$(exempt_repo exempt_all '{SECRET_TOKEN}\treason one\nzzothername\treason two')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "every pattern exempt -> rc 1" 1 "$RC" "$OUT"
assert_contains "refuses to be a gate that checks nothing" "would check nothing" "$OUT"

echo "=== A. allowlist mechanics preserved (Spec item 2) ==="
allow_repo() {
  local name="$1" allow_line="$2"
  local root; root="$(make_repo "$name")"
  {
    echo "=== IDENTITIES_START ==="
    echo "SECRET_TOKEN=zzsecretname"
    echo "=== IDENTITIES_END ==="
    echo "=== FORBIDDEN_PATTERNS_START ==="
    echo "{SECRET_TOKEN}"
    echo "=== FORBIDDEN_PATTERNS_END ==="
    echo "=== ALLOWLIST_START ==="
    printf '%s\n' "$allow_line"
    echo "=== ALLOWLIST_END ==="
  } > "$root/open-source/IDENTIFIER-RULES.txt"
  printf '%s\n' "$root"
}

R="$(allow_repo allow_missing 'src/nope.txt:anchor text:a written reason')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "allowlist path not in the tree -> rc 1" 1 "$RC" "$OUT"
assert_contains "names the missing path" "path not found in the tree" "$OUT"

R="$(allow_repo allow_numeric 'src/app.txt:42:a written reason')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "purely numeric anchor -> rc 1" 1 "$RC" "$OUT"
assert_contains "calls it a re-armed line-pin" "re-armed line-pin" "$OUT"

R="$(allow_repo allow_colons 'src/app.txt:has:three:colons here')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "wrong colon count -> rc 1" 1 "$RC" "$OUT"
assert_contains "states the exactly-2-colons rule" "need exactly 2" "$OUT"

R="$(allow_repo allow_stale 'src/app.txt:baseline:the anchored line carries no forbidden identifier')"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "anchor resolves but line is not a hit -> rc 1" 1 "$RC" "$OUT"
assert_contains "calls it a stale allowlist entry" "stale allowlist entry" "$OUT"

R="$(allow_repo allow_ambiguous 'src/dup.txt:zzsecretname:reason')"
printf 'zzsecretname one\nzzsecretname two\n' > "$R/src/dup.txt"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "anchor matching two lines -> rc 1" 1 "$RC" "$OUT"
assert_contains "calls the anchor ambiguous" "(ambiguous)" "$OUT"

R="$(allow_repo allow_suppress 'src/known.txt:zzsecretname is documented here:a written reason')"
printf 'zzsecretname is documented here\n' > "$R/src/known.txt"
BASE="$(commit_baseline "$R")"
# Re-add the very line the allowlist anchors, plus an unallowlisted one.
printf 'zzsecretname is documented here\n' > "$R/src/known.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "valid allowlist entry, no new hits -> rc 0" 0 "$RC" "$OUT"
assert_contains "counts the allowlist entry" "allowlist=1" "$OUT"

echo "=== B. the REAL rules file refuses an identifier it exists to refuse ==="
# literal_for <pattern> — turns one FORBIDDEN_PATTERNS line into a literal
# string that matches it, by taking the first character of every bracket
# group and dropping the rest ([Jj] -> J, [p] -> p). Works for every pattern
# this file currently carries because none of them needs anything richer
# than a per-letter case class. This is how the planted string is derived
# from the live file at runtime instead of being typed here.
literal_for() { sed -E 's/\[(.)[^]]*\]/\1/g' <<<"$1"; }

mapfile -t REAL_FORBIDDEN < <(sed -n '/^=== FORBIDDEN_PATTERNS_START ===$/,/^=== FORBIDDEN_PATTERNS_END ===$/p' "$REAL_RULES" | grep -vE '^(===|[[:space:]]*#|[[:space:]]*$)')
if [[ "${#REAL_FORBIDDEN[@]}" -eq 0 ]]; then
  bad "real FORBIDDEN_PATTERNS is non-empty" "parsed zero lines from $REAL_RULES"
else
  ok "real FORBIDDEN_PATTERNS is non-empty (${#REAL_FORBIDDEN[@]} patterns)"
  R="$(make_repo realrules)"
  cp "$REAL_RULES" "$R/open-source/IDENTIFIER-RULES.txt"
  BASE="$(commit_baseline "$R")"

  OUT="$(run_scan "$R" "$BASE")"; RC=$?
  assert_rc "real rules, nothing added -> rc 0" 0 "$RC" "$OUT"

  for p in "${REAL_FORBIDDEN[@]}"; do
    PLANT="$(literal_for "$p")"
    printf 'planted: %s\n' "$PLANT" > "$R/src/app.txt"
    OUT="$(run_scan "$R" "$BASE")"; RC=$?
    assert_rc "real rules, real identifier planted -> rc 1" 1 "$RC" "$OUT"
    assert_contains "names src/app.txt and a line number" "src/app.txt:1" "$OUT"
    assert_contains "says the identifier was added" "forbidden identifier added" "$OUT"
    printf 'baseline\n' > "$R/src/app.txt"
  done
fi

echo "=== B. the enforced set is the whole real FORBIDDEN_PATTERNS set (Spec item 2/4) ==="
# This repo's own rules file (unlike the private engine's fuller one) ships
# with an empty PREPUSH_EXEMPT block by design — nothing here is legitimately
# present anywhere in the code-plane tree, so nothing needs an exemption.
# Pin that directly: enforced count == total count, and the exempt block
# itself parses to zero entries. If a future edit adds an exemption, this
# fails loudly and a reviewer has to look at why.
mapfile -t ENFORCED < <(bash "$SCAN_SRC" --list-patterns)
if [[ "${#ENFORCED[@]}" -eq "${#REAL_FORBIDDEN[@]}" && "${#ENFORCED[@]}" -gt 0 ]]; then
  ok "every real forbidden pattern is enforced pre-push, none exempt (${#ENFORCED[@]})"
else
  bad "every real forbidden pattern is enforced pre-push, none exempt" \
    "enforced=${#ENFORCED[@]} total=${#REAL_FORBIDDEN[@]}"
fi

mapfile -t REAL_EXEMPT < <(sed -n '/^=== PREPUSH_EXEMPT_START ===$/,/^=== PREPUSH_EXEMPT_END ===$/p' "$REAL_RULES" | grep -vE '^(===|[[:space:]]*#|[[:space:]]*$)')
if [[ "${#REAL_EXEMPT[@]}" -eq 0 ]]; then
  ok "PREPUSH_EXEMPT is empty, as designed"
else
  bad "PREPUSH_EXEMPT is empty, as designed" "found ${#REAL_EXEMPT[@]} entr(ies): ${REAL_EXEMPT[*]}"
fi

for p in "${ENFORCED[@]}"; do
  if grep -qxF -- "$p" <(printf '%s\n' "${REAL_FORBIDDEN[@]}"); then
    ok "enforced pattern is a verbatim FORBIDDEN_PATTERNS line"
  else
    bad "enforced pattern is a verbatim FORBIDDEN_PATTERNS line" "not found verbatim: $p"
  fi
done

echo "=== C. the gate is wired into run_always_gates and is not exempt from the self-skip assertion ==="
# Spec item 5. run_always_gates() asserts execution by counting, with no
# list of gate names to edit — so "covered by that assertion" means "called
# from run_always_gates", and "produces a verdict" means it reaches
# pass/fail or a positive tree-shape SKIP, never a silent return.
PFC="$REPO_ROOT/scripts/lib/preflight-common.sh"
if sed -n '/^run_always_gates() {/,/^}/p' "$PFC" | grep -q 'check_forbidden_identifiers'; then
  ok "check_forbidden_identifiers is called from run_always_gates"
else
  bad "check_forbidden_identifiers is called from run_always_gates" "not found in the function body"
fi
if sed -n '/^check_forbidden_identifiers() {/,/^}/p' "$PFC" | grep -q 'CHECKS_RUN++'; then
  ok "the gate increments CHECKS_RUN, so the ran-for-real count includes it"
else
  bad "the gate increments CHECKS_RUN" "no CHECKS_RUN++ in the function body"
fi
if sed -n '/^check_forbidden_identifiers() {/,/^}/p' "$PFC" | grep -q 'self_skip'; then
  ok "a missing script or rules file self_skips (hard-failed by run_always_gates) rather than passing quietly"
else
  bad "a missing script or rules file self_skips" "no self_skip call in the function body"
fi

echo "=== D. item 1, at runtime: no open-source/ dir self-skips loudly, not a silent [SKIP] ==="
# Regression test for the actual defect D#2492 filed: this used to be a bare
# `echo "[SKIP] ..."; return 0` with no self_skip call, which is exactly the
# branch that made the guard read green on the code plane having checked
# nothing. Exercise the real function, not just grep its source.
NODIR="$SCRATCH/no-open-source"
mkdir -p "$NODIR/scripts/lib"
cp "$PFC" "$NODIR/scripts/lib/preflight-common.sh"
(
  REPO_ROOT="$NODIR"
  START_TS=$SECONDS
  CHECKS_RUN=0
  CURRENT_SLUG=""
  FAILED=0
  # shellcheck source=/dev/null
  source "$NODIR/scripts/lib/preflight-common.sh"
  SELF_SKIPPED_COUNT=0
  SELF_SKIPPED_NAMES=()
  # A command substitution `$(...)` would run check_forbidden_identifiers in
  # a NESTED subshell, so self_skip's SELF_SKIPPED_COUNT increment would
  # never make it back out even though stdout would still be captured —
  # redirect to a file instead, in this same subshell, so the side effect
  # on the counter survives to be read below.
  check_forbidden_identifiers > "$NODIR/gate-stdout.txt" 2>&1
  GATE_RC=$?
  {
    echo "RC=$GATE_RC"
    echo "SELF_SKIPPED_COUNT=$SELF_SKIPPED_COUNT"
    cat "$NODIR/gate-stdout.txt"
  } > "$NODIR/result.txt"
)
RESULT="$(cat "$NODIR/result.txt")"
assert_contains "self_skip fired (count=1)" "SELF_SKIPPED_COUNT=1" "$RESULT"
assert_contains "output carries a WARN naming what was not checked" "[WARN]" "$RESULT"
assert_not_contains "no bare [SKIP] line for this branch" "[SKIP] Forbidden Identifiers" "$RESULT"

echo "=== E. item 5: an empty rules file is a refusal, not a vacuous pass ==="
R="$(make_repo emptyrules)"
: > "$R/open-source/IDENTIFIER-RULES.txt"
BASE="$(commit_baseline "$R")"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "empty rules file -> rc 1" 1 "$RC" "$OUT"
assert_not_contains "does not report PASS" "PASS (" "$OUT"
assert_contains "refuses a vacuous PASS by name" "vacuous PASS" "$OUT"

echo "=== F. item 4: positive control on the REAL rules file — clean text stays clean ==="
R="$(make_repo realrules_negative)"
cp "$REAL_RULES" "$R/open-source/IDENTIFIER-RULES.txt"
BASE="$(commit_baseline "$R")"
printf 'ordinary text: coldstart, serve_forever, forever young, claw machines\n' >> "$R/src/app.txt"
OUT="$(run_scan "$R" "$BASE")"; RC=$?
assert_rc "unrelated prose near the patterns does not false-positive -> rc 0" 0 "$RC" "$OUT"

echo
echo "passed=$PASS failed=$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
