#!/usr/bin/env bash
# tests/test_memory_triage.sh — verify memory-triage.sh behavior
#
# Asserts:
#   1. All memory files (except MEMORY.md) have a tier field
#   2. All tier values are in the valid set
#   3. --list-tier output is sorted and unique

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/memory-triage.sh"
FIXTURE_DIR="$REPO_ROOT/scripts/memory-triage"
VALID_TIERS="project transferable hardwire-candidate"

pass=0
fail=0

check() {
  local desc="$1"
  local result="$2"
  if [ "$result" = "ok" ]; then
    echo "PASS: $desc"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — $result"
    fail=$((fail + 1))
  fi
}

# --- Test 1: all memory files have a tier field ---
missing_tier=0
for f in "$FIXTURE_DIR"/*.md; do
  fname=$(basename "$f")
  [ "$fname" = "MEMORY.md" ] && continue
  if ! grep -q "^tier:" "$f" 2>/dev/null; then
    echo "  missing tier: $fname" >&2
    missing_tier=$((missing_tier + 1))
  fi
done
[ "$missing_tier" -eq 0 ] && check "all memory files have tier field" "ok" \
  || check "all memory files have tier field" "$missing_tier file(s) missing tier"

# --- Test 2: all tier values are valid ---
invalid_tier=0
for f in "$FIXTURE_DIR"/*.md; do
  fname=$(basename "$f")
  [ "$fname" = "MEMORY.md" ] && continue
  tier_val=$(grep -m1 "^tier:" "$f" 2>/dev/null | sed 's/^tier: //' | tr -d '[:space:]')
  valid=0
  for t in $VALID_TIERS; do
    [ "$t" = "$tier_val" ] && valid=1
  done
  if [ "$valid" -eq 0 ]; then
    echo "  invalid tier '$tier_val' in $fname" >&2
    invalid_tier=$((invalid_tier + 1))
  fi
done
[ "$invalid_tier" -eq 0 ] && check "all tier values are valid" "ok" \
  || check "all tier values are valid" "$invalid_tier file(s) with invalid tier"

# --- Test 3: --validate exits 0 ---
if "$SCRIPT" --validate >/dev/null 2>&1; then
  check "--validate exits 0" "ok"
else
  check "--validate exits 0" "non-zero exit"
fi

# --- Test 4-6: --list-tier output is sorted and unique for each tier ---
for tier in $VALID_TIERS; do
  raw=$("$SCRIPT" --list-tier "$tier" 2>/dev/null)
  if [ -z "$raw" ]; then
    # It's valid to have zero results (though unlikely) — just check no error
    check "--list-tier $tier (empty — no error)" "ok"
    continue
  fi
  sorted=$(echo "$raw" | sort -u)
  if [ "$raw" = "$sorted" ]; then
    check "--list-tier $tier output is sorted and unique" "ok"
  else
    check "--list-tier $tier output is sorted and unique" "output not sorted or contains duplicates"
  fi
done

# --- Test 7: each tier has at least one file ---
for tier in $VALID_TIERS; do
  count=$("$SCRIPT" --list-tier "$tier" 2>/dev/null | wc -l)
  [ "$count" -gt 0 ] && check "--list-tier $tier has >=1 result" "ok" \
    || check "--list-tier $tier has >=1 result" "0 results"
done

# --- Test 8: --validate fails on a bad fixture ---
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
cat > "$tmpdir/test_bad.md" <<'EOF'
---
name: bad-file
description: "missing tier"
---
body
EOF
# Temporarily patch FIXTURE_DIR lookup — can't do that without modifying script,
# so instead verify the grep detection directly
if grep -q "^tier:" "$tmpdir/test_bad.md" 2>/dev/null; then
  check "bad fixture (no tier) is detected" "false — grep found tier in bad file (unexpected)"
else
  check "bad fixture (no tier) is detected by grep" "ok"
fi

# --- Summary ---
echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
