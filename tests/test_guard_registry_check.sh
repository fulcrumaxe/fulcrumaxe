#!/usr/bin/env bash
# tests/test_guard_registry_check.sh — exercises scripts/ci/guard-registry-check.py
# against fixture repo trees (D#2339 PR-a).
#
# The checker resolves its repo root from __file__, so each case builds a
# throwaway tree (scripts/ci/ + .github/workflows/ci.yml) and runs the real
# checker source with __file__ pointed into that tree. Running it that way
# rather than copying the file in keeps the checker out of its own subject
# set, which is what lets case 6 present a genuinely empty scripts/ci/.
# No state dir, no network, no stubs. The last case runs the checker on the
# real tree the way .github/workflows/ci.yml runs it.
#
# Usage: bash tests/test_guard_registry_check.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECKER="$REPO_ROOT/scripts/ci/guard-registry-check.py"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

if [ ! -f "$CHECKER" ]; then
  echo "FAIL: checker not found: $CHECKER" >&2
  exit 1
fi

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

SHIM="$TMPROOT/shim.py"
cat > "$SHIM" <<'PY'
"""Run guard-registry-check.py as though it lived under a fixture repo root."""
import sys

src, fake_path = sys.argv[1], sys.argv[2]
sys.argv = ["guard-registry-check.py"] + sys.argv[3:]
with open(src) as fh:
    code = compile(fh.read(), src, "exec")
exec(code, {"__name__": "__main__", "__file__": fake_path})
PY

# make_tree <name> — build $TMPROOT/<name> with scripts/ci/ and .github/workflows/.
make_tree() {
  local root="$TMPROOT/$1"
  mkdir -p "$root/scripts/ci" "$root/.github/workflows"
  echo "$root"
}

# workflow <root> <guard-file...> — a minimal workflow whose run: lines
# reference the given scripts/ci files.
workflow() {
  local root="$1"; shift
  {
    echo "jobs:"
    echo "  backend:"
    echo "    name: backend (import-smoke)"
    echo "    steps:"
    local p
    for p in "$@"; do
      echo "      - name: step for $p"
      echo "        run: python3 scripts/ci/$p"
    done
  } > "$root/.github/workflows/ci.yml"
}

# ledger <root> <json>
ledger() {
  printf '%s\n' "$2" > "$1/scripts/ci/guard-ledger.json"
}

# run_checker <root> [args...] — echo "<exit>|<stdout+stderr on one line>"
run_checker() {
  local root="$1"; shift
  local out rc
  out="$(python3 "$SHIM" "$CHECKER" "$root/scripts/ci/guard-registry-check.py" "$@" 2>&1)"
  rc=$?
  printf '%s|%s' "$rc" "$(printf '%s' "$out" | tr '\n' ' ')"
}

expect() {
  local label="$1" want_rc="$2" want_sub="$3" got="$4"
  local rc="${got%%|*}" out="${got#*|}"
  if [ "$rc" != "$want_rc" ]; then
    fail "$label" "expected exit $want_rc, got $rc — output: $out"
    return
  fi
  if [ -n "$want_sub" ] && [[ "$out" != *"$want_sub"* ]]; then
    fail "$label" "exit $rc as expected but output never mentioned '$want_sub' — output: $out"
    return
  fi
  pass "$label"
}

echo "== guard-registry-check =="

# 1. Happy path: one wired guard, one ledgered non-guard.
R="$(make_tree happy)"
touch "$R/scripts/ci/alpha-guard.py" "$R/scripts/ci/local-tool.sh"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {"local-tool.sh": "run by hand on a dev host, never in CI"}}'
expect "wired + ledgered reconciles clean" 0 "PASS  alpha-guard.py" "$(run_checker "$R")"

# 2. A file in neither the workflow nor the ledger — the dropped-guard case
#    this check exists for.
R="$(make_tree unwired)"
touch "$R/scripts/ci/alpha-guard.py" "$R/scripts/ci/orphan-guard.py"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {}}'
expect "unwired, unledgered file fails and is named" 1 "orphan-guard.py" "$(run_checker "$R")"

# 3. A blank reason is not a decision.
R="$(make_tree blank_reason)"
touch "$R/scripts/ci/local-tool.sh"
workflow "$R"
ledger "$R" '{"exempt": {"local-tool.sh": "   "}}'
expect "blank ledger reason fails" 1 "empty or non-string reason" "$(run_checker "$R")"

# 4. A ledger entry naming a file that no longer exists is stale.
R="$(make_tree stale)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {"deleted-tool.sh": "a real-looking reason"}}'
expect "stale ledger entry fails and is named" 1 "deleted-tool.sh" "$(run_checker "$R")"

# 5. Wired AND ledgered means one of the two is stale.
R="$(make_tree both)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {"alpha-guard.py": "supposedly not a CI step"}}'
expect "wired and ledgered at once fails" 1 "one of the two is stale" "$(run_checker "$R")"

# 6. Discovering nothing is a failure, not a pass — the item that keeps this
#    check from becoming the thing it guards against.
R="$(make_tree empty)"
workflow "$R"
ledger "$R" '{"exempt": {}}'
expect "empty scripts/ci/ fails rather than reporting all-clear" 1 "discovered zero files" "$(run_checker "$R")"

# 7. A guard named only in a YAML comment is not wired.
R="$(make_tree comment_only)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
printf '      # see scripts/ci/alpha-guard.py for why\n' >> "$R/.github/workflows/ci.yml"
ledger "$R" '{"exempt": {}}'
expect "a comment mention does not count as wired" 1 "alpha-guard.py" "$(run_checker "$R")"

# 8. Discovery is a directory listing, not a mode-bit filter.
R="$(make_tree modebit)"
touch "$R/scripts/ci/no-x-bit-guard.py"
chmod 644 "$R/scripts/ci/no-x-bit-guard.py"
workflow "$R" no-x-bit-guard.py
ledger "$R" '{"exempt": {}}'
expect "a non-executable file is still discovered" 0 "no-x-bit-guard.py" "$(run_checker "$R")"
expect "--list includes the non-executable file" 0 "no-x-bit-guard.py" "$(run_checker "$R" --list)"
expect "--list reports a count" 0 "count: 1" "$(run_checker "$R" --list)"

# 9. A missing or malformed ledger is a hard failure, not an empty exemption set.
R="$(make_tree no_ledger)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R" alpha-guard.py
expect "missing ledger fails" 1 "guard-ledger.json is missing" "$(run_checker "$R")"
ledger "$R" '{"exempt": {}, "typo_key": 1}'
expect "unknown top-level ledger key fails" 1 "unknown top-level key" "$(run_checker "$R")"
ledger "$R" '{"note": "no exempt object here"}'
expect "ledger without an exempt object fails" 1 "missing its required 'exempt' object" "$(run_checker "$R")"

# 10. The real tree, run the way ci.yml runs it.
expect "the real repo reconciles clean" 0 "guard-registry-check: OK" "$(run_checker "$REPO_ROOT")"

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
