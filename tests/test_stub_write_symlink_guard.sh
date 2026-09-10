#!/usr/bin/env bash
# tests/test_stub_write_symlink_guard.sh — proves tests/lib/stub-write.sh
# actually refuses to follow a symlink, and that a naive `cat >`/`>` write
# in the same situation does not.
#
# This is the general-mechanism proof for D#2499: a test fixture creates a
# symlink at a path, then a later step writes a stub "over" that path. If
# the write follows the symlink it lands on whatever the link points at —
# which, in the code-plane PR #103 incident this guards against, was the
# real scripts/lib/pr_intake_gate.py in the working tree. This suite never
# touches that file or any other real production file: it builds its own
# disposable sentinel and symlink under a mktemp'd scratch dir, so it can
# demonstrate the corruption without being able to cause it for real.
#
# Runs the identical containment check twice — once through stub_write,
# once through a naive `cat >` redirect — so both directions are visible in
# one run: the guarded path must leave the sentinel untouched, and the naive
# path must NOT (proving the check is discriminating, not a tautology that
# would pass either way).
#
# Usage:
#   bash tests/test_stub_write_symlink_guard.sh
#
# Exits 0 if all assertions pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tests/lib/stub-write.sh
source "$SCRIPT_DIR/lib/stub-write.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

RUN_TMP="$(mktemp -d /tmp/test_stub_write_symlink_guard.XXXXXX)"
trap 'rm -rf "$RUN_TMP"' EXIT

SENTINEL_CONTENT="sentinel — do not touch (test_stub_write_symlink_guard $$)"

_sha() { sha256sum "$1" | awk '{print $1}'; }

# A naive stub writer — the exact shape ("cat > $path <<EOF ... EOF") that
# corrupted the real file during PR #103, reproduced here only against a
# disposable sentinel.
_write_naive() {
  cat > "$1"
}

# Builds a fresh real-file + symlink-into-it pair under $RUN_TMP/<label>,
# writes a stub to the symlink path via $2, then reports (via return code)
# whether the real file is byte-identical to what it was before the write.
# 0 = untouched (contained), 1 = corrupted (write followed the symlink).
_run_containment_check() {
  local label="$1" write_fn="$2"
  local real="$RUN_TMP/$label/real-file.txt"
  local link="$RUN_TMP/$label/fixture/link.txt"
  mkdir -p "$(dirname "$real")" "$(dirname "$link")"
  printf '%s\n' "$SENTINEL_CONTENT" > "$real"
  local before after
  before="$(_sha "$real")"

  ln -s "$real" "$link"

  "$write_fn" "$link" <<'STUB'
stub content that must never reach the real file
STUB

  after="$(_sha "$real")"
  [[ "$before" == "$after" ]]
}

# ── Assertion 1: stub_write leaves the real file byte-identical ────────────
if _run_containment_check "guarded" stub_write; then
  pass "stub_write: real file is byte-identical (sha256) after a write to the symlinked path"
else
  fail "stub_write: real file is byte-identical (sha256) after a write to the symlinked path"
fi

# stub_write must still perform the write — at the destination path, as a
# plain file — not silently no-op the whole call.
GUARD_LINK="$RUN_TMP/guarded/fixture/link.txt"
if [[ -f "$GUARD_LINK" && ! -L "$GUARD_LINK" ]] && \
   grep -q "stub content that must never reach the real file" "$GUARD_LINK"; then
  pass "stub_write: still writes the stub at the destination path, as a plain (non-symlink) file"
else
  fail "stub_write: still writes the stub at the destination path, as a plain (non-symlink) file"
fi

# ── Assertion 2: the same check, run against a naive `cat >` write, fails ──
# This is the required "must fail" half of criterion 3 — proving the check
# above is actually discriminating rather than passing unconditionally.
if _run_containment_check "naive" _write_naive; then
  fail "naive cat> redirect: real file is byte-identical (sha256) after a write to the symlinked path (expected this to fail — a naive redirect follows the symlink and corrupts the real file, but it did not here)"
else
  pass "naive cat> redirect: real file is byte-identical (sha256) after a write to the symlinked path correctly FAILS — demonstrates the exact defect stub_write prevents"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ "$FAIL" -gt 0 ]]; then
  echo "FAILED — see PASS/FAIL lines above"
  exit 1
fi

echo "All tests passed."
exit 0
