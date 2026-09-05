#!/usr/bin/env bash
# tests/test_repo_target_gate.sh — Unit tests for
# open-source/checks/repo-target-gate.sh (D#1870).
#
# Each test builds a small synthetic target-dir fixture (not a full
# produced export — that's exercised separately by verify-export.sh) and
# asserts the check's exit code / output on it. One test per assertion the
# check makes, so a mutation of any single assertion breaks exactly one
# test here, not a sibling (D#1870 Spec item 7 / PR #1865 precedent) — with
# one documented exception: forbidden-shape family 7 ("default=" / "default:"
# keyword, PATTERNS' last entry). Its own regex only accepts an "=" or ":"
# connector — the exact same two connectors families 1 (assignment) and 5
# (JSON/TS colon field) already match unconditionally, regardless of what
# precedes them. So *no* fixture can trip family 7 without also tripping
# family 1 or family 5 — a content-based mutation test of family 7 alone is
# not just missing, it's provably impossible (D#1879 — an earlier version of
# this file used a fixture for "default= keyword detected" that didn't even
# contain the literal token "default", passing only via family 1; deleting
# family 7 left the suite green because nothing here actually depended on
# it). Family 7's positive-detection test below now uses an honest fixture
# that genuinely contains "default", and its mutation-isolation is a
# structural assertion (the PATTERNS array length surfaced in the summary
# line) instead of a content-based one, since content-based isolation isn't
# available.
#
# The check's allowlist is unconditional (mirrors identifier-gate.sh: an
# allowlist entry for a path that isn't in the scanned tree is itself
# "stale" / unreachable, by design — see IDENTIFIER-RULES.txt's wiki/
# carve-out precedent). So every fixture here is built on top of a base
# dir that stubs all six current allowlisted path:anchor pairs with
# matching content, keeping each test's failure signal isolated to the
# one file it actually adds.
#
# D#2192: the allowlist moved from path:line:reason to path:anchor:reason
# (content anchors, ported from identifier-gate.sh / D#2186), so the base
# fixture below deliberately puts each stub's matching line at a line
# number that does NOT match the real source file it stands in for — the
# whole point of an anchor is that its resolved line number is irrelevant
# to whether the entry is valid.
#
# Run: bash tests/test_repo_target_gate.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$REPO_ROOT/open-source/checks/repo-target-gate.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

run_check() {
  local dir="$1"
  OUT="$(bash "$CHECK" "$dir" 2>&1)"
  RC=$?
}

# Build a base fixture dir that satisfies every current allowlist entry
# (stub files with a matching line containing each entry's content anchor),
# so a test that adds one more file only ever sees FAILs tied to that file.
#
# Line numbers below are deliberately NOT the real source files' line numbers
# and deliberately NOT each other's — proving the gate resolves purely from
# anchor content, never a hand-written position, is the point of this fixture
# (see D#2192 note above).
#
# A file may need more than one anchored line: loop-bootstrap/bootstrap.sh
# carries two entries (SOURCE_REPO and ENGINE_CANONICAL_REPO), which is why
# stub() takes (lineno, content) pairs rather than a single pair of args.
make_base_dir() {
  local dir
  dir="$(mktemp -d)"
  python3 - "$dir" <<'PYEOF'
import sys, os
base = sys.argv[1]

def stub(relpath, *entries):
    path = os.path.join(base, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["" for _ in range(max(n for n, _ in entries))]
    for lineno, content in entries:
        lines[lineno - 1] = content
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

stub("backend/fleet/runtime.py", (20, '    #     "repo": "fulcrumaxe/fulcrumaxe",'))
stub("ts-backend/src/config/repo.ts", (40, 'export const DEFAULT_REPO = "fulcrumaxe/fulcrumaxe";'))
stub("tui/src/backend.ts", (10, "        GH_REPO: 'fulcrumaxe/fulcrumaxe',"))
stub("tui/src/index.tsx", (30, '      `gh api graphql --repo fulcrumaxe/fulcrumaxe`,'))
stub("loop-bootstrap/bootstrap.sh",
     (50, 'SOURCE_REPO="${LOOP_BOOTSTRAP_SOURCE_REPO:-fulcrumaxe/fulcrumaxe}"'),
     (70, '  ENGINE_CANONICAL_REPO="${LOOP_BOOTSTRAP_ENGINE_REPO:-fulcrumaxe/fulcrumaxe}"'))
stub("scripts/update-check.sh",
     (15, 'DEFAULT_ENGINE_REPO="${LOOP_BOOTSTRAP_ENGINE_REPO:-fulcrumaxe/fulcrumaxe}"'))
PYEOF
  echo "$dir"
}

BASE_DIR="$(make_base_dir)"
run_check "$BASE_DIR"
if [[ "$RC" -eq 0 ]]; then pass "base fixture (every allowlist entry satisfied, nothing else) is clean"; else fail "base fixture should be clean ($OUT)"; fi
if echo "$OUT" | grep -q "patterns=9"; then pass "all 9 forbidden-shape patterns present in summary"; else fail "expected patterns=9 in summary ($OUT)"; fi

# add_case <relpath> <content> — copy BASE_DIR, add one file, return its path.
add_case() {
  local rel="$1" content="$2" dir
  dir="$(mktemp -d)"
  cp -r "$BASE_DIR"/. "$dir"/
  mkdir -p "$dir/$(dirname "$rel")"
  printf '%s\n' "$content" > "$dir/$rel"
  echo "$dir"
}

echo "=== Shape-detection: each forbidden pattern fires on its own fixture ==="

d=$(add_case "app.py" 'DEFAULT_REPO = "fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1"; then pass "double-quote assignment detected"; else fail "double-quote assignment detected ($OUT)"; fi

d=$(add_case "app.py" "DEFAULT_REPO = 'fulcrumaxe/fulcrumaxe'")
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1"; then pass "single-quote assignment detected"; else fail "single-quote assignment detected ($OUT)"; fi

d=$(add_case "app.py" $'def f():\n    return "fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:2"; then pass "bare return detected"; else fail "bare return detected ($OUT)"; fi

d=$(add_case "app.py" 'repo = os.environ.get("AUTONOMOUS_TEAM_REPO", "fulcrumaxe/fulcrumaxe")')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1"; then pass "os.environ.get default detected"; else fail "os.environ.get default detected ($OUT)"; fi

d=$(add_case "app.py" 'repo = os.getenv("AUTONOMOUS_TEAM_REPO", "fulcrumaxe/fulcrumaxe")')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app.py:1"; then pass "getenv default detected"; else fail "getenv default detected ($OUT)"; fi

d=$(add_case "run.sh" 'REPO="${AUTONOMOUS_TEAM_REPO:-fulcrumaxe/fulcrumaxe}"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "run.sh:1"; then pass "shell param-expansion default detected"; else fail "shell param-expansion default detected ($OUT)"; fi

d=$(add_case "config.json" '{"repo": "fulcrumaxe/fulcrumaxe"}')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "config.json:1"; then pass "JSON field detected"; else fail "JSON field detected ($OUT)"; fi

d=$(add_case "widget.ts" "const cfg = { GH_REPO: 'fulcrumaxe/fulcrumaxe' }")
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "widget.ts:1"; then pass "TS object field detected"; else fail "TS object field detected ($OUT)"; fi

d=$(add_case "run2.sh" 'gh pr view 1 --repo fulcrumaxe/fulcrumaxe')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "run2.sh:1"; then pass "--repo CLI flag detected"; else fail "--repo CLI flag detected ($OUT)"; fi

d=$(add_case "app2.py" 'parser.add_argument("--repo", default="fulcrumaxe/fulcrumaxe")')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "app2.py:1"; then pass "default= keyword detected"; else fail "default= keyword detected ($OUT)"; fi
# NOTE: this fixture genuinely contains the literal "default" token pattern
# 7 requires — unlike an earlier version of this test (D#1879), which used
# `def f(repo_slug: str = "fulcrumaxe/fulcrumaxe"):` and never contained the
# word "default" at all, passing only because pattern 1 (assignment) also
# fires on `= "SLUG"`. It still does here too: pattern 7's own shape
# (`default` + `=`/`:` + quoted SLUG) is a proper subset of pattern 1's shape
# (anything + `=` + quoted SLUG) whenever the connector is `=`, and of
# pattern 5's shape (anything + `:` + quoted SLUG) whenever it's `:` — so no
# fixture can trip pattern 7 without also tripping one of those two. See the
# structural mutation test below for how pattern 7's presence is actually
# verified.

echo ""
echo "=== Negative cases: things that must NOT be flagged ==="

d=$(add_case "eq.py" 'if repo == "fulcrumaxe/fulcrumaxe":')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "== comparison not mistaken for assignment"; else fail "== comparison not mistaken for assignment ($OUT)"; fi

d=$(add_case "url.py" 'URL = "https://github.com/fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "URL-valued assignment not flagged"; else fail "URL-valued assignment not flagged ($OUT)"; fi

d=$(add_case "README.md" 'gh CLI calls must use --repo fulcrumaxe/fulcrumaxe')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "markdown files not scanned"; else fail "markdown files not scanned ($OUT)"; fi

# The specific leave-case that motivates the markdown blind spot at
# repo-target-gate.sh:30-34: README.md's CI badge, which must resolve to the
# real upstream repo and must never be flagged by this check.
d=$(add_case "README.md" '![CI](https://github.com/fulcrumaxe/fulcrumaxe/actions/workflows/ci.yml/badge.svg)')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "README CI badge not flagged"; else fail "README CI badge not flagged ($OUT)"; fi

d=$(add_case "tests/test_foo.py" 'REPO = "fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "tests/ directory not scanned"; else fail "tests/ directory not scanned ($OUT)"; fi

d=$(add_case "test_foo.py" 'REPO = "fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "test_*.py basename not scanned"; else fail "test_*.py basename not scanned ($OUT)"; fi

d=$(add_case "foo.test.ts" 'REPO = "fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "*.test.ts basename not scanned"; else fail "*.test.ts basename not scanned ($OUT)"; fi

d=$(add_case "app.txt" 'REPO = "fulcrumaxe/fulcrumaxe"')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "unlisted extension (.txt) not scanned"; else fail "unlisted extension (.txt) not scanned ($OUT)"; fi

echo ""
echo "=== Allowlist mechanics ==="

run_check "$BASE_DIR"
if [[ "$RC" -eq 0 ]]; then pass "allowlisted hits (every entry, path:anchor match) do not fail"; else fail "allowlisted hits do not fail ($OUT)"; fi

# Drifted allowlist entry: same path, anchor still resolves to a line
# (content unchanged around it), but the allowlisted line itself no longer
# matches any forbidden shape (its value changed). This is D#2192 Spec item
# 5: must produce exactly ONE stale-entry message, not the doubled
# hit+stale pair the old line-keyed version produced on a pure line move.
d="$(mktemp -d)"
cp -r "$BASE_DIR"/. "$d"/
python3 - "$d" <<'PYEOF'
import sys
path = sys.argv[1] + "/tui/src/backend.ts"
lines = open(path).read().split("\n")
lines[9] = "        GH_REPO: 'somewhere/else',"
open(path, "w").write("\n".join(lines))
PYEOF
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "stale allowlist entry 'tui/src/backend.ts:10'" && ! echo "$OUT" | grep -q "unallowlisted"; then
  pass "drifted allowlist entry detected (stale, single message)"
else
  fail "drifted allowlist entry detected ($OUT)"
fi

# D#2192 Spec item 4 — the regression this whole change exists to prevent:
# inserting a line ABOVE an anchored line must NOT fail the gate, because
# the anchor re-resolves to the new (shifted) line number on every run.
d="$(mktemp -d)"
cp -r "$BASE_DIR"/. "$d"/
python3 - "$d" <<'PYEOF'
import sys
path = sys.argv[1] + "/tui/src/backend.ts"
content = open(path).read()
open(path, "w").write("// an inserted line that shifts everything below it\n" + content)
PYEOF
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then
  pass "inserting a line above an anchored line does not fail the gate"
else
  fail "inserting a line above an anchored line should not fail the gate ($OUT)"
fi

# D#2192 Spec item 6 — an anchor matching two or more lines is ambiguous
# and must hard-fail, naming the match count.
d="$(mktemp -d)"
cp -r "$BASE_DIR"/. "$d"/
python3 - "$d" <<'PYEOF'
import sys
path = sys.argv[1] + "/tui/src/backend.ts"
with open(path, "a") as f:
    f.write("        GH_REPO: 'duplicate/anchor',\n")
PYEOF
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "matches 2 lines in tui/src/backend.ts (ambiguous)"; then
  pass "ambiguous anchor (matches 2+ lines) hard-fails, naming the count"
else
  fail "ambiguous anchor should hard-fail naming the count ($OUT)"
fi

# D#2192 Spec item 7 — a purely numeric anchor is a re-armed line-pin
# wearing the new field name, not real content, and must hard-fail. Swap
# the tui/src/backend.ts entry's anchor for its own (numeric) base
# fixture line number -- exactly the regression this rule exists to catch.
mutant="$(mktemp)"
python3 - "$CHECK" "$mutant" <<'PYEOF'
import re, sys
src = open(sys.argv[1]).read()
new_src, n = re.subn(
    r'tui/src/backend\.ts:GH_REPO:',
    'tui/src/backend.ts:10:',
    src, count=1,
)
assert n == 1, "tui/src/backend.ts allowlist entry not found for mutation"
open(sys.argv[2], "w").write(new_src)
PYEOF
OUT="$(bash "$mutant" "$BASE_DIR" 2>&1)"
RC=$?
rm -f "$mutant"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "purely numeric"; then
  pass "purely numeric anchor hard-fails as a re-armed line-pin"
else
  fail "purely numeric anchor should hard-fail ($OUT)"
fi

echo ""
echo "=== Mutation testing: break one assertion at a time ==="
# Each block copies the real check to a temp file, applies exactly ONE
# targeted source mutation, and confirms exactly the assertion tied to
# that mutation flips — proving it's load-bearing, not vacuous.

mutate_and_run() {
  # mutate_and_run <sed-expr> <fixture-dir>
  local sed_expr="$1" dir="$2" mutant
  mutant="$(mktemp)"
  sed -E "$sed_expr" "$CHECK" > "$mutant"
  OUT="$(bash "$mutant" "$dir" 2>&1)"
  RC=$?
  rm -f "$mutant"
}

# Mutation 1: disable the allowlist-suppression branch. The base fixture
# (clean on the real check) must now FAIL on every allowlisted hit.
mutate_and_run 's/if \[\[ -n "\$\{ALLOWLIST_SEEN\[\$key\]\+x\}" \]\]; then/if false; then/' "$BASE_DIR"
if [[ "$RC" -eq 1 ]]; then pass "mutant: disabling allowlist-suppression now fails the base fixture"; else fail "mutant: disabling allowlist-suppression should have failed ($OUT)"; fi

# Mutation 2: disable the stale-allowlist check. The drifted fixture
# (FAILs on the real check) must now PASS on the mutant.
d="$(mktemp -d)"
cp -r "$BASE_DIR"/. "$d"/
python3 - "$d" <<'PYEOF'
import sys
path = sys.argv[1] + "/tui/src/backend.ts"
lines = open(path).read().split("\n")
lines[9] = "        GH_REPO: 'somewhere/else',"
open(path, "w").write("\n".join(lines))
PYEOF
mutate_and_run 's/if \[\[ "\$\{ALLOWLIST_SEEN\[\$key\]:-0\}" -eq 0 \]\]; then/if false; then/' "$d"
rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "mutant: disabling stale-allowlist check now passes the drifted fixture"; else fail "mutant: disabling stale-allowlist check should have passed ($OUT)"; fi

# Mutation 3: empty out the PATTERNS array. Must still hard-fail with the
# "zero forbidden patterns" message on ANY fixture (even the clean base one).
mutant="$(mktemp)"
python3 - "$CHECK" "$mutant" <<'PYEOF'
import re, sys
src = open(sys.argv[1]).read()
src = re.sub(r"PATTERNS=\(.*?\n\)\n", "PATTERNS=()\n", src, count=1, flags=re.S)
open(sys.argv[2], "w").write(src)
PYEOF
OUT="$(bash "$mutant" "$BASE_DIR" 2>&1)"
RC=$?
rm -f "$mutant"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "zero forbidden patterns"; then pass "mutant: empty PATTERNS array still hard-fails with the right message"; else fail "mutant: empty PATTERNS array ($OUT)"; fi

# Mutation 4: disable is_test_path exclusion. A tests/ fixture (clean on
# the real check) must now FAIL on the mutant.
d=$(add_case "tests/test_foo.py" 'REPO = "fulcrumaxe/fulcrumaxe"')
mutate_and_run 's/is_test_path "\$rel" && continue/false \&\& continue/' "$d"
rm -rf "$d"
if [[ "$RC" -eq 1 ]]; then pass "mutant: disabling is_test_path now flags the tests/ fixture"; else fail "mutant: disabling is_test_path should have flagged it ($OUT)"; fi

# Mutation 5: widen SCAN_EXTENSIONS to include md. A markdown fixture
# (clean on the real check) must now FAIL on the mutant.
d=$(add_case "README.md" '--repo fulcrumaxe/fulcrumaxe')
mutate_and_run 's/SCAN_EXTENSIONS=\(py sh ts tsx js jsx json yaml yml\)/SCAN_EXTENSIONS=(py sh ts tsx js jsx json yaml yml md)/' "$d"
rm -rf "$d"
if [[ "$RC" -eq 1 ]]; then pass "mutant: adding md to SCAN_EXTENSIONS now flags the markdown fixture"; else fail "mutant: adding md to SCAN_EXTENSIONS should have flagged it ($OUT)"; fi

# Mutation 6: loosen the assignment pattern's closing-quote anchor. A
# URL-valued assignment (clean on the real check) must now FAIL.
d=$(add_case "url2.py" 'URL = "https://github.com/fulcrumaxe/fulcrumaxe"')
mutate_and_run "s/\\[\\^=\\!<>\\]=\\[\\[:space:\\]\\]\\*\"'\"\\\$SLUG\"'\"/[^=!<>]=.*fulcrumaxe\\/fulcrumaxe/" "$d"
rm -rf "$d"
if [[ "$RC" -eq 1 ]]; then pass "mutant: loosening the assignment anchor now flags the URL-valued fixture"; else fail "mutant: loosening the assignment anchor should have flagged it ($OUT)"; fi

# Mutation 7: remove forbidden-shape family 7 ("default=" / "default:"
# keyword) from PATTERNS entirely. This is the one pattern whose own shape
# (a "default" token followed by `=`/`:` then a quoted SLUG) is a proper
# subset of family 1's shape (anything + `=` + quoted SLUG) or family 5's
# shape (anything + `:` + quoted SLUG) — so no content fixture can isolate
# it (see the header comment and the "default= keyword detected" test
# above). Its removal is instead verified structurally: PATTERNS' length is
# surfaced in every run's summary line, so deleting family 7 must turn
# "patterns=9" into "patterns=8" on the clean base fixture. D#1879: an
# earlier version of this suite had no assertion at all tied to family 7's
# presence — this is that missing assertion.
mutant="$(mktemp)"
python3 - "$CHECK" "$mutant" <<'PYEOF'
import re, sys
src = open(sys.argv[1]).read()
new_src, n = re.subn(r"\n  # 7\. Keyword default:.*?\n\)", "\n)", src, count=1, flags=re.S)
assert n == 1, "family-7 pattern block not found for mutation"
open(sys.argv[2], "w").write(new_src)
PYEOF
OUT="$(bash "$mutant" "$BASE_DIR" 2>&1)"
RC=$?
rm -f "$mutant"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "patterns=8"; then
  pass "mutant: removing family 7 from PATTERNS drops the pattern count to 8"
else
  fail "mutant: removing family 7 from PATTERNS should have reported patterns=8 ($OUT)"
fi

rm -rf "$BASE_DIR"

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
