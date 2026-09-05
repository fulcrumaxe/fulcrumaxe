#!/usr/bin/env bash
# tests/test_pre_spawn_check_syntax.sh
# Regression test: pre-spawn-check.sh must parse cleanly (bash -n).
# Guards against heredoc-inside-$() syntax errors like the one fixed in PR #840.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

echo "=== test_pre_spawn_check_syntax ==="
echo ""

SYNTAX_ERR="$(mktemp /tmp/test_pre_spawn_check_syntax.XXXXXX)"
trap 'rm -f "$SYNTAX_ERR"' EXIT

bash -n "$REPO_ROOT/scripts/pre-spawn-check.sh" 2>"$SYNTAX_ERR"
EXIT=$?

if [[ "$EXIT" -eq 0 ]]; then
  ok "pre-spawn-check.sh passes bash -n (no syntax errors)"
else
  fail "pre-spawn-check.sh has syntax errors: $(cat "$SYNTAX_ERR")"
fi

echo ""
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
