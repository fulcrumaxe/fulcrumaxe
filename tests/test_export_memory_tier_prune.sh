#!/usr/bin/env bash
# tests/test_export_memory_tier_prune.sh — regression test for the
# tier-driven memory prune block in open-source/export.sh (D#1845 / PR
# #1871). Extracted three times by hand during review, proven correct, and
# then shipped with nothing in the repo to catch a regression (D#1873).
#
# Runs the REAL prune block, extracted out of export.sh by its heredoc
# markers rather than by line number — line numbers already drifted once
# (200 -> 126) between this Discussion being opened and triaged. There are
# TWO `PYEOF` heredocs in export.sh (this prune block, and a separate
# identifier-rewrite pass below it); the extraction disambiguates by
# matching the opening line `python3 - "$TARGET_DIR" <<` and exiting at the
# FIRST closing `PYEOF`, so it can't swallow the wrong block or both.
#
# Accepts an optional path to export.sh (default: the repo's own copy) so
# the exact same test can be pointed at a mutated /tmp copy for mutation
# testing without ever writing a mutant into the repo.
#
# Usage: bash tests/test_export_memory_tier_prune.sh [path-to-export.sh]

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPORT_SH="${1:-$REPO_ROOT/open-source/export.sh}"

pass=0
fail=0

check() {
  local desc="$1" result="$2"
  if [ "$result" = ok ]; then
    echo "PASS: $desc"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — $result"
    fail=$((fail + 1))
  fi
}

# --- extraction --------------------------------------------------------

extract_prune_block() {
  local export_sh="$1"
  awk '/^python3 - "\$TARGET_DIR" <</{f=1;next} /^PYEOF/{if(f)exit} f' "$export_sh"
}

# Anti-vacuity guard (D#1873 criterion 4): a naive `extract | python3 -`
# pipeline exits 0 on a missing export.sh — the extraction yields nothing,
# and `python3 -` happily runs an empty program and exits clean. That is
# the exact silent-pass shape this Discussion exists to prevent, so it is
# guarded explicitly here rather than left to `set -e` / pipefail, which do
# not catch an empty-but-successful extraction.
run_prune() {
  local export_sh="$1" target_dir="$2"
  if [[ ! -f "$export_sh" ]]; then
    echo "run_prune: export.sh not found at $export_sh" >&2
    return 1
  fi
  local block
  block="$(extract_prune_block "$export_sh")"
  if [[ -z "$block" ]]; then
    echo "run_prune: extracted an empty prune block from $export_sh — refusing to run it" >&2
    return 1
  fi
  printf '%s\n' "$block" | python3 - "$target_dir"
}

mk_fixture() {
  local dir
  dir="$(mktemp -d)"
  mkdir -p "$dir/scripts/memory-triage"
  echo "$dir"
}

cleanup_dirs=()
cleanup() {
  local d
  for d in "${cleanup_dirs[@]}"; do
    rm -rf "$d"
  done
}
trap cleanup EXIT

# --- Case 0: anti-vacuity guard -----------------------------------------

out=$(run_prune "/tmp/does-not-exist-export-$$.sh" /tmp 2>&1)
rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "does-not-exist-export-$$.sh"; then
  check "guard: missing export.sh fails loudly, names the file" "ok"
else
  check "guard: missing export.sh fails loudly, names the file" "rc=$rc out=$out"
fi

# --- Case 1: missing tier: (frontmatter present, no tier: anywhere) -----

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/missing_tier.md" <<'EOF'
---
name: missing-tier
description: "no tier field anywhere in this file"
---
body text, no tier mention anywhere
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -q "missing_tier.md"; then
  check "missing tier: exits 1, names the file" "ok"
else
  check "missing tier: exits 1, names the file" "rc=$rc out=$out"
fi

# --- Case 2: tier: nonsense ----------------------------------------------

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/bad_tier.md" <<'EOF'
---
tier: nonsense
---
body
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -q "bad_tier.md"; then
  check "tier: nonsense exits 1, names the file" "ok"
else
  check "tier: nonsense exits 1, names the file" "rc=$rc out=$out"
fi

# --- Case 3: no frontmatter at all (a bare "tier:" line in the body must --
# --- NOT be picked up once frontmatter is absent) -------------------------

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/no_frontmatter.md" <<'EOF'
This is a memory note without frontmatter.
tier: project
The rest of the note.
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -q "no_frontmatter.md"; then
  check "no frontmatter at all: exits 1, names the file" "ok"
else
  check "no frontmatter at all: exits 1, names the file" "rc=$rc out=$out"
fi

# --- Case 4: MEMORY.md line that is not a memory-index entry -------------

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/valid.md" <<'EOF'
---
tier: transferable
---
body
EOF
cat > "$fx/scripts/memory-triage/MEMORY.md" <<'EOF'
- [Valid Entry](valid.md)
this line has no markdown link and is not blank
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -q "does not parse as a memory-index entry"; then
  check "bad MEMORY.md line: exits 1, names the line" "ok"
else
  check "bad MEMORY.md line: exits 1, names the line" "rc=$rc out=$out"
fi

# --- Case 5: tier: in body prose is not read as frontmatter ---------------

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/prose.md" <<'EOF'
---
tier: transferable
---
This memory's frontmatter tier is transferable. The line below looks like a
tier declaration but is body prose after the closing frontmatter delimiter,
and must not be read as the tier.
tier: project
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && [ -f "$fx/scripts/memory-triage/prose.md" ]; then
  check "tier: in body prose is not read as frontmatter" "ok"
else
  check "tier: in body prose is not read as frontmatter" "rc=$rc out=$out"
fi

# --- Case 6: project <-> transferable flip pair ---------------------------
# Proves the field is actually read, rather than a disguised fallback to
# the deleted name-list: flipping the SAME file's tier must flip the
# outcome in both directions.

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/flip.md" <<'EOF'
---
tier: project
---
secret content
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && [ ! -f "$fx/scripts/memory-triage/flip.md" ]; then
  check "flip pair: tier: project prunes" "ok"
else
  check "flip pair: tier: project prunes" "rc=$rc out=$out"
fi

fx=$(mk_fixture); cleanup_dirs+=("$fx")
cat > "$fx/scripts/memory-triage/flip.md" <<'EOF'
---
tier: transferable
---
secret content
EOF
out=$(run_prune "$EXPORT_SH" "$fx" 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && [ -f "$fx/scripts/memory-triage/flip.md" ]; then
  check "flip pair: tier: transferable ships" "ok"
else
  check "flip pair: tier: transferable ships" "rc=$rc out=$out"
fi

# --- Summary ---

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
