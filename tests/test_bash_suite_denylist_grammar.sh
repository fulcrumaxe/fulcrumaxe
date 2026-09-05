#!/usr/bin/env bash
# tests/test_bash_suite_denylist_grammar.sh — enforces the five-class
# grammar D#2152 requires of every scripts/run-pr-tests.sh
# BASH_SUITE_DENYLIST entry: <path>:<class>(<argument>) — <one-line evidence>.
#
# Source nothing, no stubs, no network — just extract the array literal out
# of the real file with awk and validate each entry line with plain bash
# regexes and filesystem checks. Modeled on the pass/fail counter harness in
# tests/test_run_pr_tests_routing.sh.
#
# D#2152 Spec item 2. Before D#2152 landed, 21+ of 24 entries carried only
# the bare "measured fail on this host (PR body table)" reason with no
# class/argument/evidence shape — this suite fails against that state and
# passes once every entry is triaged into the grammar.
#
# Usage: bash tests/test_bash_suite_denylist_grammar.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${1:-$REPO_ROOT/scripts/run-pr-tests.sh}"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

if [ ! -f "$TARGET" ]; then
  echo "FAIL: target file not found: $TARGET" >&2
  exit 1
fi

# Extract just the quoted entry lines between "BASH_SUITE_DENYLIST=(" and
# the closing ")" — awk so comment lines above/below the array (which may
# themselves mention class names or parens) are never mistaken for entries.
mapfile -t RAW_LINES < <(awk '
  /^BASH_SUITE_DENYLIST=\(/ { inarr=1; next }
  inarr && /^\)/ { inarr=0; next }
  inarr { print }
' "$TARGET")

if [ "${#RAW_LINES[@]}" -eq 0 ]; then
  fail "array extraction" "found 0 entries in BASH_SUITE_DENYLIST — extraction regex or array shape changed?"
  echo ""
  echo "== $PASS passed, $FAIL failed =="
  exit 1
fi

echo "Found ${#RAW_LINES[@]} denylist entries."

# <path>:<class>(<argument>) — <one-line evidence>
# class is exactly one of: bug host-env slow flaky obsolete
GRAMMAR_RE='^[[:space:]]*"([^:"]+):(bug|host-env|slow|flaky|obsolete)\(([^)]+)\)[[:space:]]—[[:space:]](.+)"$'

BANNED_SUBSTRING='measured fail on this host (PR body table)'

for line in "${RAW_LINES[@]}"; do
  # Blank lines inside the array (none expected, but skip cleanly if present).
  [ -n "${line//[[:space:]]/}" ] || continue

  if [[ "$line" =~ $GRAMMAR_RE ]]; then
    entry_path="${BASH_REMATCH[1]}"
    entry_class="${BASH_REMATCH[2]}"
    entry_arg="${BASH_REMATCH[3]}"
    pass "grammar: $entry_path matches <path>:<class>(<argument>) — <evidence>"
  else
    fail "grammar: entry does not match the grammar" "$line"
    continue
  fi

  # Class token is in the closed five-value vocabulary. The regex alternation
  # above already restricts this, but assert it explicitly and by name so a
  # future edit to the regex alone cannot silently widen the vocabulary
  # without a visible check breaking.
  case "$entry_class" in
    bug|host-env|slow|flaky|obsolete)
      pass "class vocabulary: $entry_path class '$entry_class' is in {bug,host-env,slow,flaky,obsolete}" ;;
    *)
      fail "class vocabulary: $entry_path class '$entry_class' is in {bug,host-env,slow,flaky,obsolete}" "got '$entry_class'" ;;
  esac

  if [ "$entry_class" = "bug" ]; then
    if [[ "$entry_arg" =~ ^D#[0-9]+$ ]]; then
      pass "bug argument: $entry_path argument '$entry_arg' matches D#[0-9]+"
    else
      fail "bug argument: $entry_path argument matches D#[0-9]+" "got '$entry_arg'"
    fi
  fi

  if [ "$entry_class" = "obsolete" ]; then
    if [ -d "$REPO_ROOT/$entry_arg" ]; then
      pass "obsolete argument: $entry_path archive path '$entry_arg' exists on disk"
    else
      fail "obsolete argument: $entry_path archive path '$entry_arg' exists on disk" "not found: $REPO_ROOT/$entry_arg"
    fi
    if [ -e "$REPO_ROOT/$entry_path" ]; then
      fail "obsolete path removed: $entry_path no longer exists on disk" "still present at $REPO_ROOT/$entry_path"
    else
      pass "obsolete path removed: $entry_path no longer exists on disk"
    fi
  else
    if [ -e "$REPO_ROOT/$entry_path" ]; then
      pass "non-obsolete path exists: $entry_path is present on disk"
    else
      fail "non-obsolete path exists: $entry_path is present on disk" "not found: $REPO_ROOT/$entry_path"
    fi
  fi

  if [[ "$line" == *"$BANNED_SUBSTRING"* ]]; then
    fail "no bare 'measured fail' text: $entry_path" "reason text still contains '$BANNED_SUBSTRING'"
  else
    pass "no bare 'measured fail' text: $entry_path"
  fi
done

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
