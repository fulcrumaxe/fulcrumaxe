#!/usr/bin/env bash
# tests/test_no_hardcoded_checkout_paths_guard.sh — hermetic unit tests for
# scripts/check-no-hardcoded-checkout-paths.sh (D#1877).
#
# Modelled on tests/test_repo_target_gate.sh — the house pattern for this
# kind of guard test: every fixture is a small synthetic git repo built
# under mktemp, never the real repo, and each mutation test copies the real
# check to a mutant tmpfile, applies exactly ONE targeted source change,
# and confirms exactly the assertion tied to that mutation flips. A
# mutation that breaks two tests, or none, is not proof of anything (PR
# #1865 shipped a vacuous test that survived a file-level mutation
# precisely because the mutation broke a sibling instead of the intended
# assertion — this file's mutation-isolation discipline exists to catch
# that class of bug here too).
#
# This file itself is excluded from the real check by construction (see
# the check's own header/EXCLUDED-files comment) — its fixtures below
# deliberately write the pattern as synthetic test data, and while this
# file was untracked (added but not yet `git add`ed) it was invisible to
# the very gate it tests, which is exactly the false-green shape D#1877
# exists to close (review round 2: Gate 1 evidence collected pre-commit
# was true when collected, and wrong by the time the PR shipped, because
# the check scopes to `git ls-files`). Collect this file's own Gate 1
# evidence AFTER `git add`, same as any tracked-file-scoped guard.
#
# Because the real check computes its own repo root from
# `dirname "${BASH_SOURCE[0]}"` rather than taking a target-dir argument
# (see the check's own header comment on why it isn't sourced from /
# doesn't share a lib with open-source/checks/identifier-gate.sh), each
# fixture below places a COPY of the check inside its own synthetic git
# repo at the same relative path (scripts/check-no-hardcoded-checkout-paths.sh)
# so `git ls-files` and the allowlist path both resolve inside the
# fixture, never the real tree.
#
# Run: bash tests/test_no_hardcoded_checkout_paths_guard.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SRC="$REPO_ROOT/scripts/check-no-hardcoded-checkout-paths.sh"
CHECK_REL="scripts/check-no-hardcoded-checkout-paths.sh"
ALLOWLIST_REL="scripts/fixtures/allowed_checkout_path_literals.txt"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# content_hash <line> — same trim + sha256(first 12 hex) as the real
# check's hash_line(), so fixtures below can compute the exact key an
# allowlist entry needs without hardcoding a brittle literal hash.
trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}
content_hash() {
  trim "$1" | sha256sum | cut -c1-12
}

run_check() {
  # run_check <fixture-dir> [check-path]
  local dir="$1"
  local check="${2:-}"
  [ -z "$check" ] && check="$dir/$CHECK_REL"
  OUT="$(bash "$check" 2>&1)"
  RC=$?
}

# init_repo <dir> — git-init a fixture dir so `git ls-files` works inside it.
init_repo() {
  local dir="$1"
  git -C "$dir" init -q
  git -C "$dir" add -A
}

# new_fixture — empty synthetic repo with just the check installed.
#
# D#2018: the check now derives its PATTERN from the checkout it's actually
# running in (scripts/lib/repo-root-resolve.sh's _resolve_main_repo_root),
# not a hardcoded username, and that derivation's home-shaped component
# keys on the checkout's own basename. Nesting every fixture one level
# under mktemp -d, in a directory literally named "checkout", pins that
# basename so the suite's existing planted literals ("/home/agent/checkout",
# "/home/testuser/checkout", ...) keep matching without each fixture needing its
# own AUTONOMOUS_TEAM_REPO_ROOT override. Also copies the resolver lib
# itself into the fixture at the same relative path the real check sources
# it from — the fixture only ever contained a copy of the check script
# before this, and the check now `source`s a sibling file it did not need
# to before.
new_fixture() {
  local dir
  dir="$(mktemp -d)/checkout"
  mkdir -p "$dir/$(dirname "$CHECK_REL")/lib" "$dir/$(dirname "$ALLOWLIST_REL")"
  cp "$CHECK_SRC" "$dir/$CHECK_REL"
  cp "$REPO_ROOT/scripts/lib/repo-root-resolve.sh" "$dir/scripts/lib/repo-root-resolve.sh"
  echo "$dir"
}

write_allowlist() {
  # write_allowlist <dir> <content>
  printf '%s\n' "$2" > "$1/$ALLOWLIST_REL"
}

echo "=== Base cases ==="

# --- Not a git repo at all (no `init_repo` call) hard-fails with a named,
#     single message — not silently proceeding to report every allowlist
#     entry as dangling because `git ls-files` came back empty (D#1877
#     review round 3: this used to print 169 misleading failures instead
#     of one clear one). ---
d="$(new_fixture)"
write_allowlist "$d" "# empty allowlist"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "not a git repository" && [[ "$(echo "$OUT" | grep -c 'FAIL:')" -eq 1 ]]; then
  pass "not a git repository hard-fails with exactly one named message"
else
  fail "not-a-git-repo should hard-fail with exactly one message ($OUT)"
fi
rm -rf "$d"

# --- Clean tree, no matches anywhere: guard passes with zero matches. ---
d="$(new_fixture)"
write_allowlist "$d" "# empty allowlist, no matches in the tree"
echo "no path literals here" > "$d/app.txt"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 matches"; then pass "empty tree, empty allowlist: clean (0 matches)"; else fail "empty tree should be clean ($OUT)"; fi
rm -rf "$d"

# --- Missing allowlist file itself is a hard failure. ---
d="$(new_fixture)"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "allowlist file .* not found"; then pass "missing allowlist file hard-fails"; else fail "missing allowlist file should hard-fail ($OUT)"; fi
rm -rf "$d"

# --- Malformed allowlist entry (missing the reason field) hard-fails, named. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"'):"
printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "malformed allowlist entry"; then pass "malformed allowlist entry (missing reason field) hard-fails"; else fail "malformed entry should hard-fail ($OUT)"; fi
rm -rf "$d"

# --- Malformed allowlist entry (hash isn't 12 hex chars) hard-fails, named. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:not-hex:some reason"
printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "malformed allowlist entry"; then pass "malformed allowlist entry (non-hex hash) hard-fails"; else fail "non-hex hash entry should hard-fail ($OUT)"; fi
rm -rf "$d"

echo ""
echo "=== Unlisted match ==="

# --- A match with no allowlist entry at all fails, naming path AND line. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'one\ntwo\nthree\nX = "/home/agent/checkout"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:4 contains a hardcoded checkout path"; then
  pass "unlisted match fails naming path:line"
else
  fail "unlisted match should fail naming path:line ($OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Content-hash allowlisting ==="

# --- An allowlisted path:hash is clean on that exact content. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"'):test fixture — this exact line is allowlisted"
printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "1 matches"; then pass "allowlisted path:hash is clean"; else fail "allowlisted path:hash should be clean ($OUT)"; fi
rm -rf "$d"

# --- The same file, allowlisted only for one line's content, has a SECOND
#     match with DIFFERENT content that is NOT allowlisted — content
#     keying means this still fails, naming the new line specifically.
#     This is the exact defect D#1877 exists to close: the old whole-file
#     format would have exempted it for free just because one line in the
#     file was listed. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout/a"'):test fixture — only this content is allowlisted"
printf 'X = "/home/agent/checkout/a"\ntwo\nY = "/home/agent/checkout/b"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:3 contains a hardcoded checkout path"; then
  pass "content-keying: unlisted content in an already-listed file still fails"
else
  fail "content-keying: unlisted second line should still fail ($OUT)"
fi
rm -rf "$d"

# --- Line-shift resilience — the whole reason for switching from path:line
#     to path:hash (D#1877 review round 2): inserting an unrelated line
#     ABOVE an allowlisted match shifts its line number but not its
#     content. A path:line allowlist would spuriously fail here (the old
#     entry no longer matches the shifted line); path:hash stays clean. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"'):test fixture — content-keyed, should survive a line shift"
printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
before_rc="$RC"
# Now insert an unrelated line above it — same content, new line number.
printf '# an unrelated new import, added above\nX = "/home/agent/checkout"\n' > "$d/app.py"
run_check "$d"
if [[ "$before_rc" -eq 0 && "$RC" -eq 0 ]]; then
  pass "content-hash keying survives an unrelated line shift above the match"
else
  fail "content-hash keying should survive a line shift (before=$before_rc after=$RC: $OUT)"
fi
rm -rf "$d"

# --- Regression pin for the `IFS=: read` trailing-colon bug (D#1877
#     review round 2): a matched line that itself ends in a literal ':'
#     (common in Python — `for x in y:`) must hash on its FULL content,
#     colon included. Allowlisting the correct (colon-included) hash
#     passes; the hash of the same line with its trailing colon stripped
#     off is a different value entirely and does NOT satisfy the entry —
#     proving the two are genuinely different hashes, not the same one
#     under a different name. ---
d="$(new_fixture)"
line_with_colon='for path in [f"{_MAIN_REPO}/foo.txt", "/home/agent/checkout/file"]:'
correct_hash="$(content_hash "$line_with_colon")"
stripped_hash="$(content_hash "${line_with_colon%:}")"
if [[ "$correct_hash" == "$stripped_hash" ]]; then
  fail "regression-pin setup: colon-included and colon-stripped hashes should differ (both were $correct_hash)"
else
  write_allowlist "$d" "app.py:$correct_hash:test fixture — trailing colon must be part of the hashed content"
  printf '%s\n' "$line_with_colon" > "$d/app.py"
  init_repo "$d"
  run_check "$d"
  if [[ "$RC" -eq 0 ]]; then
    pass "trailing-colon regression pin: full line (colon included) hashes correctly and is allowlisted clean"
  else
    fail "trailing-colon regression pin: correct hash should have been clean ($OUT)"
  fi
  rm -rf "$d"
fi

echo ""
echo "=== Stale entries ==="

# --- An allowlist entry whose hash no longer matches any current line in
#     that (tracked) file hard-fails, naming the path:hash key. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'this content no longer exists anywhere'):test fixture — stale, no current line hashes to this"
printf 'no longer a checkout path here\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
stale_key="app.py:$(content_hash 'this content no longer exists anywhere')"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "stale allowlist entry '$stale_key'"; then
  pass "stale entry (no current line hashes to this) hard-fails, named"
else
  fail "stale entry should hard-fail, named ($OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Dangling entries ==="

# --- An allowlist entry naming a path that isn't tracked at all
#     (the whole PR #1894 class: 417 files deleted, 24 entries left
#     naming them) hard-fails, naming the path:hash key. ---
d="$(new_fixture)"
write_allowlist "$d" "gone.py:$(content_hash 'anything'):test fixture — dangling, gone.py was never created"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "dangling allowlist entry 'gone.py:$(content_hash 'anything')'"; then
  pass "dangling entry (path not in git ls-files) hard-fails, named"
else
  fail "dangling entry should hard-fail, named ($OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Exclusions (by construction, never via the allowlist) ==="

# --- archive/** is excluded regardless of allowlist contents. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/archive/old-thing"
printf 'X = "/home/agent/checkout"\n' > "$d/archive/old-thing/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then pass "archive/** excluded by construction"; else fail "archive/** should be excluded ($OUT)"; fi
rm -rf "$d"

# --- wiki/Changelog.md and wiki/Project-Status.md are excluded by
#     construction — a hit there (e.g. a PR title containing a checkout
#     path, written by sync-wiki.sh) never needs an allowlist entry. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/wiki"
printf '## PR: fix /home/agent/checkout thing\n' > "$d/wiki/Changelog.md"
printf 'status references /home/testuser/checkout\n' > "$d/wiki/Project-Status.md"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "wiki/Changelog.md and wiki/Project-Status.md excluded by construction"
else
  fail "generated wiki files should be excluded ($OUT)"
fi
rm -rf "$d"

# --- A DIFFERENT wiki file is not exempt — the exclusion is scoped to
#     exactly those two generated filenames, not all of wiki/. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/wiki"
printf 'X = "/home/agent/checkout"\n' > "$d/wiki/Some-Other-Page.md"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "wiki/Some-Other-Page.md:1"; then
  pass "wiki exclusion is scoped to Changelog.md/Project-Status.md only, not all of wiki/"
else
  fail "other wiki files should still be scanned ($OUT)"
fi
rm -rf "$d"

# --- This harness itself is excluded by construction (D#1877 review round
#     2, blocker 1): a synthetic copy at the harness's own tracked path
#     needs no allowlist entry to pass. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/tests"
printf '# synthetic mutation fixture: X = "/home/agent/checkout"\n' > "$d/tests/test_no_hardcoded_checkout_paths_guard.sh"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "tests/test_no_hardcoded_checkout_paths_guard.sh excluded by construction"
else
  fail "the harness's own tracked path should be excluded ($OUT)"
fi
rm -rf "$d"

# --- A near-miss filename is NOT exempt — proves the harness exclusion is
#     an exact-path match, not a prefix or a glob, and can't silently
#     widen to catch sibling files. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/tests"
printf 'X = "/home/agent/checkout"\n' > "$d/tests/test_no_hardcoded_checkout_paths_guard_extra.sh"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "tests/test_no_hardcoded_checkout_paths_guard_extra.sh:1"; then
  pass "harness exclusion is an exact path match — a near-miss filename is still scanned"
else
  fail "near-miss filename should still be scanned, not excluded ($OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Multi-line files ==="

# --- A file with several matching lines (distinct content) needs one
#     entry PER distinct hash; covering only some of them still fails on
#     the rest. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout/a"'):test fixture — only this one covered"
printf 'X = "/home/agent/checkout/a"\nY = "/home/agent/checkout/b"\nZ = "/home/agent/checkout/c"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:2" && echo "$OUT" | grep -q "app.py:3"; then
  pass "multi-line file: partial coverage still fails on the uncovered lines"
else
  fail "multi-line file should fail on every uncovered line ($OUT)"
fi
rm -rf "$d"

d="$(new_fixture)"
write_allowlist "$d" "$(printf 'app.py:%s:line a\napp.py:%s:line b\napp.py:%s:line c' \
  "$(content_hash 'X = "/home/agent/checkout/a"')" \
  "$(content_hash 'Y = "/home/agent/checkout/b"')" \
  "$(content_hash 'Z = "/home/agent/checkout/c"')")"
printf 'X = "/home/agent/checkout/a"\nY = "/home/agent/checkout/b"\nZ = "/home/agent/checkout/c"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "3 matches"; then
  pass "multi-line file: full per-content coverage is clean"
else
  fail "multi-line file with every distinct line covered should be clean ($OUT)"
fi
rm -rf "$d"

# --- Two IDENTICAL lines in one file share a single hash, so one entry
#     covers both occurrences — content-hash keying's other consequence
#     (fewer entries needed, not more), distinct from the line-shift test
#     above. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout/dup"'):test fixture — one entry, two identical occurrences"
printf 'X = "/home/agent/checkout/dup"\ntwo\nX = "/home/agent/checkout/dup"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "2 matches"; then
  pass "identical content on two lines shares one allowlist entry"
else
  fail "identical content on two lines should share one entry ($OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Mutation testing: break one assertion at a time ==="
# Each block copies the real check into a fresh fixture, applies exactly
# ONE targeted source mutation, and confirms exactly the assertion tied to
# that mutation flips — proving it's load-bearing, not vacuous.

mutate_check() {
  # mutate_check <fixture-dir> <sed-expr> — overwrite the check INSIDE the
  # fixture with a mutated copy of the real source.
  local dir="$1" sed_expr="$2"
  sed -E "$sed_expr" "$CHECK_SRC" > "$dir/$CHECK_REL"
}

mutate_check_py() {
  # mutate_check_py <fixture-dir> <python-script-path> — like mutate_check,
  # but the mutation is a python re.sub applied to the real source (for
  # mutations too structural for a single sed expression).
  local dir="$1" script="$2"
  python3 "$script" "$CHECK_SRC" "$dir/$CHECK_REL"
}

# --- Mutation 1: disable the allowlist-suppression branch. A clean fixture
#     (0 unlisted matches on the real check) must now FAIL on its one
#     allowlisted match. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"'):test fixture — this exact line is allowlisted"
printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
init_repo "$d"
mutate_check "$d" 's/if \[\[ -n "\$\{ALLOWLIST_SEEN\[\$key\]\+x\}" \]\]; then/if false; then/'
run_check "$d"
if [[ "$RC" -eq 1 ]]; then pass "mutant 1: disabling allowlist-suppression now fails a clean fixture"; else fail "mutant 1 should have failed ($OUT)"; fi
rm -rf "$d"

# --- Mutation 2: disable the stale-entry check. Checks the SPECIFIC
#     "stale" wording (not just RC), so this mutant is independently
#     load-bearing rather than incidentally satisfied by a broken guard —
#     the same gap D#1877 review round 2 flagged in this test originally
#     (it only checked RC==0, which a sufficiently-broken mutant could also
#     produce for unrelated reasons). ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'this content no longer exists anywhere'):test fixture — stale"
printf 'no longer a checkout path here\n' > "$d/app.py"
init_repo "$d"
mutate_check "$d" 's/if \[\[ "\$\{ALLOWLIST_SEEN\[\$key\]\}" -eq 0 \]\]; then/if false; then/'
run_check "$d"
if [[ "$RC" -eq 0 ]] && ! echo "$OUT" | grep -q "stale allowlist entry"; then
  pass "mutant 2: disabling stale-entry check now passes a stale fixture, no stale wording emitted"
else
  fail "mutant 2 should have passed with no 'stale' wording ($OUT)"
fi
rm -rf "$d"

# --- Mutation 3: disable the dangling-entry check. This one doesn't flip
#     the mutant's overall exit code — an untracked path's key is also
#     never marked SEEN, so the stale-entry check independently catches it
#     too (belt-and-suspenders: same FAIL either way). What DOES break is
#     the dangling-entry test's own assertion, which checks for the
#     specific "dangling allowlist entry" wording, not just any failure —
#     under the mutant it's reported as stale instead, so that assertion
#     goes red even though the guard as a whole still fails closed. ---
d="$(new_fixture)"
write_allowlist "$d" "gone.py:$(content_hash 'anything'):test fixture — dangling"
init_repo "$d"
mutate_check "$d" 's/if \[\[ -z "\$\{TRACKED\[\$path\]\+x\}" \]\]; then/if false; then/'
run_check "$d"
dangling_key="gone.py:$(content_hash 'anything')"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "dangling allowlist entry '$dangling_key'"; then
  fail "mutant 3 should have broken the dangling-named assertion ($OUT)"
else
  pass "mutant 3: disabling dangling-entry check breaks the dangling-named assertion (reported as stale instead)"
fi
rm -rf "$d"

# --- Mutation 4: remove the wiki-exclusion filter from the scan pipeline.
#     A fixture with a hit in wiki/Changelog.md (PASSes on the real check
#     — excluded by construction) must now FAIL on the mutant. Applied via
#     python re.sub (not sed) since it removes one whole line cleanly. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/wiki"
printf '## PR: fix /home/agent/checkout thing\n' > "$d/wiki/Changelog.md"
init_repo "$d"
cat > "$d/mutate4.py" <<'PYEOF'
import sys
src = open(sys.argv[1]).read()
old = "    | grep -vxF 'wiki/Changelog.md' \\\n"
assert old in src, "mutation 4 target line not found"
src = src.replace(old, "", 1)
open(sys.argv[2], "w").write(src)
PYEOF
mutate_check_py "$d" "$d/mutate4.py"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "wiki/Changelog.md:1"; then
  pass "mutant 4: removing the wiki exclusion now flags wiki/Changelog.md"
else
  fail "mutant 4 should have flagged wiki/Changelog.md ($OUT)"
fi
rm -rf "$d"

# --- Mutation 5: reintroduce whole-file exemption (the pre-D#1877 defect)
#     — an allowlist entry for ANY content in a file suppresses EVERY
#     match in that file, not just its own content. A fixture allowlisted
#     for one line's content, with a second, differently-worded match
#     (FAILs on the real check — that's exactly content-keying's job) must
#     now PASS on the mutant. ---
d="$(new_fixture)"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout/a"'):test fixture — only this content allowlisted"
printf 'X = "/home/agent/checkout/a"\ntwo\nY = "/home/agent/checkout/b"\n' > "$d/app.py"
init_repo "$d"
cat > "$d/mutate5.py" <<'PYEOF'
import sys
src = open(sys.argv[1]).read()
old = '''    hash="$(hash_line "$line")"
    key="$f:$hash"
    if [[ -n "${ALLOWLIST_SEEN[$key]+x}" ]]; then
      ALLOWLIST_SEEN["$key"]=1
      continue
    fi'''
new = '''    hash="$(hash_line "$line")"
    key="$f:$hash"
    file_allowed=0
    for k in "${ALLOWLIST_KEYS[@]}"; do
      if [[ "$k" == "$f:"* ]]; then
        ALLOWLIST_SEEN["$k"]=1
        file_allowed=1
      fi
    done
    if [[ "$file_allowed" -eq 1 ]]; then
      continue
    fi'''
assert old in src, "mutation 5 target block not found"
src = src.replace(old, new, 1)
open(sys.argv[2], "w").write(src)
PYEOF
mutate_check_py "$d" "$d/mutate5.py"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "mutant 5: reintroducing whole-file exemption now passes the content-keying fixture"
else
  fail "mutant 5 should have passed (whole-file exemption reintroduced) ($OUT)"
fi
rm -rf "$d"

# --- Mutation 6: remove the harness's own self-exclusion line (D#1877
#     review round 2, blocker 1). A synthetic copy of the harness's
#     tracked path (PASSes on the real check — excluded by construction)
#     must now FAIL on the mutant. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
mkdir -p "$d/tests"
printf '# synthetic mutation fixture: X = "/home/agent/checkout"\n' > "$d/tests/test_no_hardcoded_checkout_paths_guard.sh"
init_repo "$d"
cat > "$d/mutate6.py" <<'PYEOF'
import sys
src = open(sys.argv[1]).read()
old = "    | grep -vxF 'tests/test_no_hardcoded_checkout_paths_guard.sh' \\\n"
assert old in src, "mutation 6 target line not found"
src = src.replace(old, "", 1)
open(sys.argv[2], "w").write(src)
PYEOF
mutate_check_py "$d" "$d/mutate6.py"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "tests/test_no_hardcoded_checkout_paths_guard.sh:1"; then
  pass "mutant 6: removing the harness self-exclusion now flags its own tracked path"
else
  fail "mutant 6 should have flagged the harness's own path ($OUT)"
fi
rm -rf "$d"

# --- Mutation 7: remove the up-front "is this a git repo" check (D#1877
#     review round 3). A fixture that's deliberately NOT a git repo (no
#     `init_repo` call) hard-fails on the real check with one named
#     message; on the mutant, `git ls-files` fails silently, MATCH_FILES
#     and TRACKED both come back empty, there's nothing to flag as
#     dangling because nothing is allowlisted either — so it FALSE-PASSES
#     as "OK: 0 matches" instead of failing. ---
d="$(new_fixture)"
write_allowlist "$d" "# empty allowlist"
cat > "$d/mutate7.py" <<'PYEOF'
import sys
src = open(sys.argv[1]).read()
old = '''if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "FAIL: not a git repository — this guard requires \\`git ls-files\\` to determine the tracked file set, and cannot run outside one" >&2
  exit 1
fi

'''
assert old in src, "mutation 7 target block not found"
src = src.replace(old, "", 1)
open(sys.argv[2], "w").write(src)
PYEOF
mutate_check_py "$d" "$d/mutate7.py"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "OK:"; then
  pass "mutant 7: removing the git-repo check now false-passes on a non-git directory"
else
  fail "mutant 7 should have false-passed on a non-git directory ($OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Banned reason families (D#1997) ==="

# The allowlist went from 169 entries to zero, and the drain established that
# two families of reason were not honest descriptions of unfinished work but
# claims that had quietly stopped being true. The guard rejects both outright
# now. One case per family, each asserting exit 1 — a test that only checks the
# guard still passes on the real (empty) allowlist would prove nothing, since
# an empty list satisfies any reason rule at all.
#
# Every case below uses a synthetic repo with a genuinely-matching line and an
# otherwise-valid entry, so the ONLY thing under test is the reason text.

banned_reason_case() {
  # banned_reason_case <label> <reason-text>
  local label="$1" reason="$2"
  local d
  d="$(new_fixture)"
  printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
  write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"'):$reason"
  init_repo "$d"
  run_check "$d"
  if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "banned reason"; then
    pass "banned reason rejected: $label"
  else
    fail "banned reason should hard-fail ($label): rc=$RC $OUT"
  fi
  rm -rf "$d"
}

banned_reason_case "not a live hardcode" \
  "test fixture data asserting on the path shape, not a live hardcode"
banned_reason_case "flag for follow-up" \
  "documents the same old path as a live example — flag for follow-up"
banned_reason_case "owned by (another Discussion)" \
  "owned by the parallel D#1814 executor in this same cutover wave"
banned_reason_case "out of ... scope" \
  "ts-backend/ is out of this ticket's scope per the sequencing note"
banned_reason_case "parked" \
  "hooks/ is parked by owner direction, this Spec forbids any hooks/ change"
banned_reason_case "lives there, not here" \
  "D#1764, draft PR #1813 — MAIN_REPO_ROOT fix lives there, not here"

# Case-insensitivity: the ban is on the claim, not on how it was capitalised.
banned_reason_case "capitalised variant" \
  "Not A Live Hardcode, just a docstring example"

# Control: a reason that states a mechanical impossibility still passes, so the
# rule above is rejecting the family and not simply rejecting every entry.
d="$(new_fixture)"
printf 'X = "/home/agent/checkout"\n' > "$d/app.py"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"'):byte-compared against a vendor artifact this repo does not control"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "OK:"; then
  pass "a mechanical-impossibility reason is still accepted"
else
  fail "mechanical-impossibility reason should pass (rc=$RC $OUT)"
fi
rm -rf "$d"

# The reason vetting must look at the reason field only. A file whose *content*
# discusses these phrases — this repo has several, including the allowlist's own
# header — must not be affected by them.
d="$(new_fixture)"
printf 'X = "/home/agent/checkout"  # parked, owned by nobody, not a live hardcode\n' > "$d/app.py"
write_allowlist "$d" "app.py:$(content_hash 'X = "/home/agent/checkout"  # parked, owned by nobody, not a live hardcode'):byte-compared against a vendor artifact this repo does not control"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "OK:"; then
  pass "banned phrases in file CONTENT do not trigger the reason ban"
else
  fail "reason ban must key on the reason field only (rc=$RC $OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Arbitrary-username coverage (D#2018) ==="
# The guard's PATTERN used to be a literal '/home/(agent|jp)' — this
# operator's two usernames, hardcoded — so a checkout under any third
# username was structurally invisible to it. These three cases plant a
# checkout path under a username that is neither, and assert the guard
# actually fires. Every fixture is rooted at ".../checkout"
# (new_fixture's own basename pin, see its header comment above), so a
# planted line reading "/home/<user>/checkout/..." matches the derived
# pattern's home-shaped component regardless of <user>.

# --- A third username, neither agent nor jp. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'CHECKOUT = "/home/thirduser/checkout/backend"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1 contains a hardcoded checkout path"; then
  pass "third-username checkout path (neither agent nor jp) is caught"
else
  fail "third-username checkout path should have been caught (rc=$RC $OUT)"
fi
rm -rf "$d"

# --- A CI-runner-shaped home — the shape a path copied out of a CI log
#     would have (GitHub Actions' default runner user is literally
#     "runner"). ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'CHECKOUT = "/home/runner/checkout/backend"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1 contains a hardcoded checkout path"; then
  pass "CI-runner-shaped checkout path (/home/runner/...) is caught"
else
  fail "CI-runner-shaped checkout path should have been caught (rc=$RC $OUT)"
fi
rm -rf "$d"

# --- The macOS home shape. ---
d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'CHECKOUT = "/Users/thirduser/checkout/backend"\n' > "$d/app.py"
init_repo "$d"
run_check "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1 contains a hardcoded checkout path"; then
  pass "macOS-shaped checkout path (/Users/...) is caught"
else
  fail "macOS-shaped checkout path should have been caught (rc=$RC $OUT)"
fi
rm -rf "$d"

echo ""
echo "=== Mutation testing: pattern derivation reverted to a hardcoded username ==="
# --- Mutation 8: revert PATTERN to the old literal '/home/(agent|jp)'.
#     Each of the three arbitrary-username fixtures above — every one of
#     which FAILS (rc=1) on the real check — must now false-PASS (rc=0)
#     on the mutant, proving the new coverage is load-bearing rather than
#     a fixture that never reached the guard (D#1984's trap). This is
#     what AC-6 in D#2018 asks for directly. The mutation script is
#     written once, outside any fixture, and reused for all three cases —
#     mutate_check_py takes CHECK_SRC (the real, unmutated file) as input
#     regardless of which fixture invokes it, so one script suffices. ---
mutate8_script="$(mktemp)"
cat > "$mutate8_script" <<'PYEOF'
import sys
src = open(sys.argv[1]).read()
old = '''PATTERN="$_PATTERN_ROOT_ESC"
if [ -n "$_PATTERN_BASENAME_ESC" ]; then
  PATTERN="${PATTERN}|/home/[^/]+/${_PATTERN_BASENAME_ESC}|/Users/[^/]+/${_PATTERN_BASENAME_ESC}|/root/${_PATTERN_BASENAME_ESC}"
fi'''
new = "PATTERN='/home/(agent|jp)'"
assert old in src, "mutation 8 target block not found"
src = src.replace(old, new, 1)
open(sys.argv[2], "w").write(src)
PYEOF

d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'CHECKOUT = "/home/thirduser/checkout/backend"\n' > "$d/app.py"
init_repo "$d"
mutate_check_py "$d" "$mutate8_script"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "mutant 8: reverting to hardcoded username false-passes the third-username case"
else
  fail "mutant 8 should have false-passed the third-username case ($OUT)"
fi
rm -rf "$d"

d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'CHECKOUT = "/home/runner/checkout/backend"\n' > "$d/app.py"
init_repo "$d"
mutate_check_py "$d" "$mutate8_script"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "mutant 8: reverting to hardcoded username false-passes the CI-runner case"
else
  fail "mutant 8 should have false-passed the CI-runner case ($OUT)"
fi
rm -rf "$d"

d="$(new_fixture)"
write_allowlist "$d" "# nothing allowlisted"
printf 'CHECKOUT = "/Users/thirduser/checkout/backend"\n' > "$d/app.py"
init_repo "$d"
mutate_check_py "$d" "$mutate8_script"
run_check "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "mutant 8: reverting to hardcoded username false-passes the macOS case"
else
  fail "mutant 8 should have false-passed the macOS case ($OUT)"
fi
rm -rf "$d"
rm -f "$mutate8_script"

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
