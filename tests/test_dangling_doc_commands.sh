#!/usr/bin/env bash
# tests/test_dangling_doc_commands.sh — Unit tests for
# open-source/checks/dangling-doc-commands.sh (D#1831).
#
# Plants the eighteen dangling-reference shapes measured in D#1831's
# re-verification, one per doc so no fence-parity interaction between
# shapes can confound a result, and asserts the guard's real behavior
# against the declared expectation table below matches exactly. A
# CAUGHT/MISSED table lives here instead of only in the Discussion so this
# file is the thing that keeps the measurement honest as the guard changes
# — grep this file, not the Discussion, to see what's covered today.
#
# Three of the twelve pre-widening misses are declared, not bugs:
#   v12 (4-space indented block), v16 (HTML <pre>) — no shipped doc uses
#     either form today; detecting them is speculative, not a fix for a
#     live defect.
#   v13 (quoted path) — not a literal, checkable path by design (mirrors
#     shell-variable references like `bash "$SCRIPT"`).
#
# Run: bash tests/test_dangling_doc_commands.sh
# Expects: all assertions pass, exit 0, caught >= 15/18.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$REPO_ROOT/open-source/checks/dangling-doc-commands.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

run_check() {
  local dir="$1"
  OUT="$(bash "$CHECK" "$dir" 2>&1)"
  RC=$?
}

# one_doc_dir <content> — a fresh target dir containing exactly one
# README.md with the given content, so the guard's DOCS list is stable
# (README.md only) across every shape fixture.
one_doc_dir() {
  local content="$1" dir
  dir="$(mktemp -d)"
  printf '%s\n' "$content" > "$dir/README.md"
  echo "$dir"
}

echo "=== Item 1: scope line printed on success and failure ==="

d=$(one_doc_dir 'nothing runnable here')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned [0-9]+ doc' && echo "$OUT" | grep -q "inline"; then
  pass "scope line on a clean (success) run matches the required shape and mentions inline"
else
  fail "scope line on success ($OUT)"
fi

d=$(one_doc_dir '```bash
bash nonexistent.sh
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned [0-9]+ doc' && echo "$OUT" | grep -q "inline"; then
  pass "scope line on a failing run matches the required shape and mentions inline"
else
  fail "scope line on failure ($OUT)"
fi

echo ""
echo "=== Item 2: empty document list is a hard failure, not a pass ==="

d="$(mktemp -d)"
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 2 ]] && echo "$OUT" | grep -qi "no documents"; then
  pass "empty document list exits 2 with 'no documents' on stderr"
else
  fail "empty document list should exit 2 ($OUT)"
fi

echo ""
echo "=== Item 3: inline code spans are scanned ==="

d=$(one_doc_dir 'Run `bash nonexistent-thing.sh` to start.')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "nonexistent-thing.sh"; then
  pass "inline code span (v03) detected"
else
  fail "inline code span (v03) ($OUT)"
fi

echo ""
echo "=== Item 4: directory-component paths are scanned (root-level narrowing removed) ==="

d=$(one_doc_dir '```bash
bash scripts/nonexistent-thing.sh
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "scripts/nonexistent-thing.sh"; then
  pass "directory-component path, bash (v04) detected"
else
  fail "directory-component path, bash (v04) ($OUT)"
fi

d=$(one_doc_dir '```bash
python3 scripts/nonexistent-thing.py
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "scripts/nonexistent-thing.py"; then
  pass "directory-component path, python3 (v05) detected"
else
  fail "directory-component path, python3 (v05) ($OUT)"
fi

echo ""
echo "=== Item 5: interpreter set (sh, python, node, ./) ==="

d=$(one_doc_dir '```bash
sh nonexistent-sh-thing.sh
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "nonexistent-sh-thing.sh"; then
  pass "sh interpreter (v06) detected"
else
  fail "sh interpreter (v06) ($OUT)"
fi

d=$(one_doc_dir '```bash
python nonexistent-py-thing.py
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "nonexistent-py-thing.py"; then
  pass "python interpreter (v07) detected"
else
  fail "python interpreter (v07) ($OUT)"
fi

d=$(one_doc_dir '```bash
./direct-exec.sh
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "./direct-exec.sh"; then
  pass "./ direct execution (v08) detected"
else
  fail "./ direct execution (v08) ($OUT)"
fi

d=$(one_doc_dir '```bash
node nonexistent-thing.js
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "nonexistent-thing.js"; then
  pass "node interpreter (v09) detected"
else
  fail "node interpreter (v09) ($OUT)"
fi

echo ""
echo "=== Item 6: ~~~ fences are treated as fences ==="

d=$(one_doc_dir '~~~bash
bash nonexistent-tilde-thing.sh
~~~')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "nonexistent-tilde-thing.sh"; then
  pass "~~~ fence (v11) detected"
else
  fail "~~~ fence (v11) ($OUT)"
fi

echo ""
echo "=== Item 7: a leading flag does not defeat detection ==="

d=$(one_doc_dir '```bash
bash -x nonexistent-flagged-thing.sh
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "'nonexistent-flagged-thing.sh'" && ! echo "$OUT" | grep -q "'-x'"; then
  pass "flag-before-path (v14) names the path, not the flag"
else
  fail "flag-before-path (v14) ($OUT)"
fi

echo ""
echo "=== Item 8: full 18-shape coverage table (D#1831 re-verification) ==="
# Each entry: "vNN|expect|content|reason". expect is CAUGHT or MISSED.
# Content is what a real doc containing that shape looks like; every
# planted reference targets a file that does not exist. reason is required
# for every MISSED entry (declared-scope items get their design rationale;
# there are none left that are simply undeclared).

# content uses literal \n for line breaks (expanded below) so each entry
# stays on one line — a real embedded newline here would make `read` at
# the bottom of this loop stop mid-record (bash `read` always breaks on
# newline, regardless of IFS).
SHAPES=(
'v01|CAUGHT|```bash\nbash v01-thing.sh\n```|'
'v02|CAUGHT|```bash\npython3 v02-thing.py\n```|'
'v03|CAUGHT|Run `bash v03-thing.sh` inline.|'
'v04|CAUGHT|```bash\nbash scripts/v04-thing.sh\n```|'
'v05|CAUGHT|```bash\npython3 scripts/v05-thing.py\n```|'
'v06|CAUGHT|```bash\nsh v06-thing.sh\n```|'
'v07|CAUGHT|```bash\npython v07-thing.py\n```|'
'v08|CAUGHT|```bash\n./v08-thing.sh\n```|'
'v09|CAUGHT|```bash\nnode v09-thing.js\n```|'
'v10|CAUGHT|```bash\n$ bash v10-thing.sh\n```|'
'v11|CAUGHT|~~~bash\nbash v11-thing.sh\n~~~|'
'v12|MISSED|    bash v12-thing.sh|declared: no shipped doc uses a 4-space indented code block; detecting a form nothing ships is speculative, not a fix for a live defect.'
'v13|MISSED|```bash\nbash "v13-thing.sh"\n```|declared: quoted path is not a literal, checkable reference (mirrors the shell-variable exception, e.g. bash "$SCRIPT").'
'v14|CAUGHT|```bash\nbash -x v14-thing.sh\n```|'
'v15|CAUGHT|```bash\nbash v15-thing.sh\n```|'
'v16|MISSED|<pre>\nbash v16-thing.sh\n</pre>|declared: no shipped doc uses an HTML <pre> block; detecting a form nothing ships is speculative, not a fix for a live defect.'
'v17|CAUGHT|```bash\nFOO=1 bash v17-thing.sh\n```|'
'v18|CAUGHT|```bash\nsudo bash v18-thing.sh\n```|'
)

CAUGHT_COUNT=0
TOTAL=0
for entry in "${SHAPES[@]}"; do
  TOTAL=$((TOTAL + 1))
  IFS='|' read -r vid expect content reason <<< "$entry"
  content="${content//\\n/$'\n'}"
  if [[ "$expect" == "MISSED" && -z "$reason" ]]; then
    fail "$vid: declared MISSED with no reason recorded"
    continue
  fi
  d=$(one_doc_dir "$content")
  run_check "$d"; rm -rf "$d"
  thing="${vid}-thing"
  actually_caught=0
  if [[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "$thing"; then
    actually_caught=1
  fi
  if [[ "$expect" == "CAUGHT" ]]; then
    if [[ "$actually_caught" -eq 1 ]]; then
      pass "$vid: expected CAUGHT, was CAUGHT"
      CAUGHT_COUNT=$((CAUGHT_COUNT + 1))
    else
      fail "$vid: expected CAUGHT, was MISSED ($OUT)"
    fi
  else
    if [[ "$actually_caught" -eq 0 ]]; then
      pass "$vid: expected MISSED ($reason), was MISSED"
    else
      fail "$vid: expected MISSED, was unexpectedly CAUGHT ($OUT)"
    fi
  fi
done

echo ""
echo "caught ${CAUGHT_COUNT}/${TOTAL}"
if [[ "$CAUGHT_COUNT" -lt 15 ]]; then
  fail "caught count $CAUGHT_COUNT/$TOTAL is below the required floor of 15"
else
  pass "caught count $CAUGHT_COUNT/$TOTAL meets the required floor of 15"
fi

echo ""
echo "=== Negative cases: things that must NOT be flagged ==="

d=$(one_doc_dir '```bash
bash "$SCRIPT_VAR"
```')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "shell-variable reference not flagged"; else fail "shell-variable reference not flagged ($OUT)"; fi

d=$(one_doc_dir 'This is prose mentioning bash and a-file.sh, not a command.')
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "plain prose outside code spans not flagged"; else fail "plain prose outside code spans not flagged ($OUT)"; fi

d="$(mktemp -d)"
cp "$REPO_ROOT/open-source/export.sh" "$d/export.sh" 2>/dev/null || true
mkdir -p "$d/open-source"
cp "$REPO_ROOT/open-source/export.sh" "$d/open-source/export.sh"
printf '%s\n' '```bash
bash open-source/export.sh /tmp/x
```' > "$d/README.md"
run_check "$d"; rm -rf "$d"
if [[ "$RC" -eq 0 ]]; then pass "reference to a file that actually exists in the export is not flagged"; else fail "existing-file reference should not be flagged ($OUT)"; fi

echo ""
echo "=== Mutation testing: a mutation to any single detection rule breaks exactly one assertion ==="

mutate_and_run() {
  # mutate_and_run <sed-expr> <fixture-dir>
  local sed_expr="$1" dir="$2" mutant
  mutant="$(mktemp)"
  sed -E "$sed_expr" "$CHECK" > "$mutant"
  OUT="$(bash "$mutant" "$dir" 2>&1)"
  RC=$?
  rm -f "$mutant"
}

# Mutation 1: drop 'sh' from the interpreter set. Only the sh fixture (v06)
# flips; bash/python3/etc. fixtures are untouched since they're separate
# alternatives in the same regex group.
d=$(one_doc_dir '```bash
sh mutant1-thing.sh
```')
mutate_and_run 's/INTERPRETERS='"'"'bash\|sh\|python3\|python\|node'"'"'/INTERPRETERS='"'"'bash|python3|python|node'"'"'/' "$d"
rm -rf "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned [0-9]+ doc'; then
  pass "mutant: removing 'sh' from INTERPRETERS now misses the sh fixture"
else
  fail "mutant: removing 'sh' should have missed the sh fixture ($OUT)"
fi

# Mutation 2: re-introduce the root-level-only narrowing (item 4). A
# directory-component fixture (caught on the real check) must now be missed.
d=$(one_doc_dir '```bash
bash scripts/mutant2-thing.sh
```')
mutate_and_run 's/\[\[ -z "\$ref" \]\] && continue/[[ -z "$ref" ]] \&\& continue; case "$ref" in *\/*) continue ;; esac/' "$d"
rm -rf "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned [0-9]+ doc'; then
  pass "mutant: reintroducing root-level-only narrowing now misses the directory-component fixture"
else
  fail "mutant: reintroducing the narrowing should have missed it ($OUT)"
fi

# Mutation 3: disable the empty-document-list hard-failure (item 2). An
# empty target dir (exit 2 on the real check) must now exit 0.
d="$(mktemp -d)"
mutate_and_run 's/if \[\[ "\$\{#DOCS\[@\]\}" -eq 0 \]\]; then/if false; then/' "$d"
rm -rf "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned 0 doc'; then
  pass "mutant: disabling the empty-doc-list guard now passes on zero documents"
else
  fail "mutant: disabling the empty-doc-list guard should have passed ($OUT)"
fi

# mutate_via_python <marker> <old> <new> <fixture-dir> — like mutate_and_run
# but uses an exact Python string replacement instead of a regex, for
# mutations whose source text has special characters sed would need heavy
# escaping for (brace/glob-heavy bash syntax). Asserts the replacement was
# actually found exactly once, so a future refactor that moves this text
# fails loudly here instead of silently producing a no-op mutant (a no-op
# mutant that still exits 0 on a fixture reads as a false PASS).
mutate_via_python() {
  local old="$1" new="$2" dir="$3" mutant
  mutant="$(mktemp)"
  python3 - "$CHECK" "$mutant" "$old" "$new" <<'PYEOF'
import sys
check_path, mutant_path, old, new = sys.argv[1:5]
src = open(check_path).read()
assert src.count(old) == 1, f"expected exactly one occurrence of the target text, found {src.count(old)}"
open(mutant_path, "w").write(src.replace(old, new, 1))
PYEOF
  OUT="$(bash "$mutant" "$dir" 2>&1)"
  RC=$?
  rm -f "$mutant"
}

# Mutation 4: disable inline-span scanning (item 3) by feeding an empty
# string to scan_text instead of the backtick-stripped span. An
# inline-span fixture (caught on the real check) must now be missed.
d=$(one_doc_dir 'Run `bash mutant4-thing.sh` inline.')
mutate_via_python 'scan_text "$rel" "$lineno" "${span:1:-1}"' 'scan_text "$rel" "$lineno" ""' "$d"
rm -rf "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned [0-9]+ doc'; then
  pass "mutant: disabling inline-span scanning now misses the inline-span fixture"
else
  fail "mutant: disabling inline-span scanning should have missed it ($OUT)"
fi

# Mutation 5: only recognize ``` fences, not ~~~ (item 6). A ~~~ fixture
# (caught on the real check) must now be missed.
d=$(one_doc_dir '~~~bash
bash mutant5-thing.sh
~~~')
mutate_via_python \
  'if [[ "$line" == '"'"'```'"'"'* || "$line" == '"'"'~~~'"'"'* ]]; then' \
  'if [[ "$line" == '"'"'```'"'"'* ]]; then' \
  "$d"
rm -rf "$d"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qE 'dangling-doc-commands: scanned [0-9]+ doc'; then
  pass "mutant: dropping ~~~ fence recognition now misses the ~~~ fixture"
else
  fail "mutant: dropping ~~~ recognition should have missed it ($OUT)"
fi

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
